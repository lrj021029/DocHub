"""中文 BM25 稀疏向量生成器（基于 jieba 分词 + 确定性 32-bit 词槽哈希）。

设计目标（TECHNICAL_SPEC §9.1）：
1. 零外部模型依赖、完全确定性可复现（同一文本跨进程、跨重启永远产出相同 indices/values）。
2. 中文采用 `jieba` 搜索引擎模式分词（并保留英文数字词项），过滤常见中英文停用词与纯标点。
3. 使用稳定哈希（BLAKE2b -> 31-bit 非负整型词 ID，避免跨 Python 进程 `PYTHONHASHSEED` 漂移）。
4. 计算 BM25 TF 饱和权重，直接产出 `qdrant_client.models.SparseVector(indices, values)`。
"""

from __future__ import annotations

import hashlib
import math
import re
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence

import jieba
from qdrant_client.models import SparseVector

# 常见中英文检索停用词（过滤高频无区分度虚词，保留关键业务与技术术语）
DEFAULT_STOPWORDS: frozenset[str] = frozenset(
    {
        # 中文停用词
        "的",
        "了",
        "和",
        "是",
        "就",
        "都",
        "而",
        "及",
        "与",
        "着",
        "或",
        "一个",
        "没有",
        "我们",
        "你们",
        "他们",
        "它们",
        "是否",
        "可以",
        "需要",
        "进行",
        "以及",
        "通过",
        "对于",
        "关于",
        "因为",
        "所以",
        "但是",
        "如果",
        "这个",
        "那个",
        "这些",
        "那些",
        "什么",
        "怎么",
        "如何",
        "请问",
        "一下",
        "一种",
        "已经",
        "正在",
        "并且",
        "或者",
        "不仅",
        "而且",
        "其中",
        "以下",
        "以上",
        "在",
        "中",
        "上",
        "下",
        "为",
        "以",
        "把",
        "被",
        "将",
        "对",
        "从",
        "到",
        "也",
        "很",
        "还",
        "再",
        "又",
        "并",
        "且",
        "之",
        "其",
        "等",
        # 英文常见停用词
        "a",
        "an",
        "the",
        "and",
        "or",
        "but",
        "if",
        "in",
        "on",
        "at",
        "to",
        "for",
        "of",
        "with",
        "by",
        "from",
        "as",
        "is",
        "are",
        "was",
        "were",
        "be",
        "been",
        "being",
        "this",
        "that",
        "these",
        "those",
        "it",
        "its",
        "into",
        "about",
    }
)

# 匹配至少包含一个中文字符、字母或数字的有效词项
_VALID_TOKEN_RE = re.compile(r"[\u4e00-\u9fffA-Za-z0-9]")


class ChineseSparseVectorizer:
    """确定性中文 BM25 稀疏向量编码器，直接兼容 Qdrant SparseVector。"""

    def __init__(
        self,
        *,
        k1: float = 1.5,
        b: float = 0.75,
        avgdl: float = 256.0,
        hash_space: int = 2_147_483_647,  # 2^31 - 1，兼容有符号与无符号 32-bit 词槽
        use_search_mode: bool = True,
        stopwords: Iterable[str] | None = None,
        idf_weights: Mapping[str, float] | None = None,
    ) -> None:
        if k1 <= 0:
            raise ValueError("BM25 参数 k1 必须大于 0")
        if not (0.0 <= b <= 1.0):
            raise ValueError("BM25 参数 b 必须位于 [0.0, 1.0]")
        if avgdl <= 0:
            raise ValueError("BM25 参数 avgdl 必须大于 0")

        self.k1 = k1
        self.b = b
        self.avgdl = avgdl
        self.hash_space = max(1024, min(hash_space, 0xFFFFFFFF))
        self.use_search_mode = use_search_mode
        self.stopwords = (
            frozenset(word.strip().lower() for word in stopwords)
            if stopwords is not None
            else DEFAULT_STOPWORDS
        )
        self._idf_weights = dict(idf_weights) if idf_weights else {}

        # 抑制 jieba 首次初始化时的冗余 stderr 提示
        jieba.setLogLevel(60)

    def tokenize(self, text: str) -> list[str]:
        """对输入文本执行中文分词、规范化小写与停用词/标点过滤。"""
        if not text or not text.strip():
            return []

        raw_tokens = jieba.cut_for_search(text) if self.use_search_mode else jieba.lcut(text)
        tokens: list[str] = []
        for raw in raw_tokens:
            token = raw.strip().lower()
            if not token:
                continue
            if not _VALID_TOKEN_RE.search(token):
                continue
            if token in self.stopwords:
                continue
            # 过滤单字符纯英文噪声（保留单字汉字与有意义数字/多字母词）
            if len(token) == 1 and token.isascii() and not token.isdigit():
                continue
            tokens.append(token)
        return tokens

    def token_to_index(self, token: str) -> int:
        """将词项稳定映射为确定性 32-bit 非负整型词槽 ID（不受 PYTHONHASHSEED 影响）。"""
        digest = hashlib.blake2b(token.encode("utf-8"), digest_size=4).digest()
        raw_int = int.from_bytes(digest, byteorder="big", signed=False)
        return raw_int % self.hash_space

    def _compute_doc_weight(self, tf: int, doc_len: int, token: str) -> float:
        """计算文档侧 BM25 词频饱和权重（可选叠加预统计 IDF）。"""
        denom = tf + self.k1 * (1.0 - self.b + self.b * (doc_len / self.avgdl))
        bm25_tf = (tf * (self.k1 + 1.0)) / denom if denom > 0 else 0.0
        idf = self._idf_weights.get(token, 1.0)
        return round(bm25_tf * idf, 6)

    def _compute_query_weight(self, tf: int, token: str) -> float:
        """计算查询侧稀疏词项权重（采用对数平滑 TF 权重并叠加可选 IDF）。"""
        q_tf = 1.0 + math.log(tf) if tf > 1 else 1.0
        idf = self._idf_weights.get(token, 1.0)
        return round(q_tf * idf, 6)

    def encode_document(self, text: str) -> SparseVector:
        """将单篇文档切片编码为 Qdrant `SparseVector(indices, values)`。"""
        tokens = self.tokenize(text)
        if not tokens:
            return SparseVector(indices=[], values=[])

        doc_len = len(tokens)
        counts = Counter(tokens)
        bucket_weights: dict[int, float] = {}
        for token, tf in counts.items():
            idx = self.token_to_index(token)
            weight = self._compute_doc_weight(tf, doc_len, token)
            bucket_weights[idx] = round(bucket_weights.get(idx, 0.0) + weight, 6)

        sorted_items = sorted(bucket_weights.items(), key=lambda pair: pair[0])
        return SparseVector(
            indices=[idx for idx, _ in sorted_items],
            values=[val for _, val in sorted_items],
        )

    def encode_documents(self, texts: Sequence[str]) -> list[SparseVector]:
        """批量编码文档切片为 Qdrant `SparseVector` 列表。"""
        return [self.encode_document(text) for text in texts]

    def encode_query(self, query: str) -> SparseVector:
        """将用户检索查询编码为 Qdrant `SparseVector(indices, values)`。"""
        tokens = self.tokenize(query)
        if not tokens:
            return SparseVector(indices=[], values=[])

        counts = Counter(tokens)
        bucket_weights: dict[int, float] = {}
        for token, tf in counts.items():
            idx = self.token_to_index(token)
            weight = self._compute_query_weight(tf, token)
            bucket_weights[idx] = round(bucket_weights.get(idx, 0.0) + weight, 6)

        sorted_items = sorted(bucket_weights.items(), key=lambda pair: pair[0])
        return SparseVector(
            indices=[idx for idx, _ in sorted_items],
            values=[val for _, val in sorted_items],
        )

    @staticmethod
    def dot_score(vec_a: SparseVector, vec_b: SparseVector) -> float:
        """计算两条 Qdrant 稀疏向量的内积检索相关性得分（与 Qdrant 稀疏点积一致）。"""
        if not vec_a.indices or not vec_b.indices:
            return 0.0
        map_a = dict(zip(vec_a.indices, vec_a.values, strict=True))
        score = 0.0
        for idx, val in zip(vec_b.indices, vec_b.values, strict=True):
            if idx in map_a:
                score += map_a[idx] * val
        return round(score, 6)
