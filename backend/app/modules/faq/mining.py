"""FAQ 脱敏挖掘与聚类引擎（基于 scikit-learn HDBSCAN + jieba，严格按 Space/权限域隔离）。

核心设计要点：
1. `QuestionRedactor`：在问题进入聚类或候选生成前执行强规则 PII 脱敏（手机号、身份证号、
   邮箱、银行卡号、工号）与空白归一化。
2. 增量指派 `assign_question_incremental` + 批量重聚类 `run_hdbscan_recluster`（严格使用
   `sklearn.cluster.HDBSCAN`，兼容 Windows Python 3.12 零 C++ 编译依赖）。
3. **跨权限隔离红线**：聚类严格按 `(organization_id, space_id)` 分片运行，绝不跨越不同
   Knowledge Space 或权限域混合生成可见候选答案；匿名跨群体统计若独立用户数低于最小群体
   隐私阈值（默认 2）则隐藏明细。
"""

from __future__ import annotations

import hashlib
import re
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import jieba
import numpy as np
from sklearn.cluster import HDBSCAN
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.ids import new_uuid7
from app.db.session import org_rls_session
from app.modules.documents.models import Chunk, Document, DocumentVersion
from app.modules.knowledge_gaps.models import (
    FAQCandidate,
    QuestionCluster,
    QuestionClusterMember,
    QuestionEvent,
)
from app.modules.knowledge_spaces.models import KnowledgeSpace

# --- PII 脱敏正则规则 ---
_PHONE_RE = re.compile(r"(?<!\d)(?:\+?86[- ]?)?1[3-9]\d{9}(?!\d)")
_ID_CARD_RE = re.compile(r"(?<!\d)\d{17}[\dXx](?!\d)")
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_BANK_CARD_RE = re.compile(r"(?<!\d)\d{16,19}(?!\d)")
_EMP_NO_RE = re.compile(r"(?i)\b(?:EMP|STAFF|GH|工号)[-:：#\s]*[A-Za-z0-9]{4,12}\b")
_MULTI_SPACE_RE = re.compile(r"\s+")
_PUNCT_STRIP_RE = re.compile(r"[？?！!。.,，;；:：\s]+$")


class QuestionRedactor:
    """用户提问 PII 脱敏与文本归一化器。"""

    @staticmethod
    def redact_pii(text: str) -> str:
        """替换手机号、身份证号、邮箱、银行卡号及工号为固定脱敏占位符。"""
        sanitized = _ID_CARD_RE.sub("[ID_CARD]", text)
        sanitized = _PHONE_RE.sub("[PHONE]", sanitized)
        sanitized = _EMAIL_RE.sub("[EMAIL]", sanitized)
        sanitized = _BANK_CARD_RE.sub("[CARD_NO]", sanitized)
        sanitized = _EMP_NO_RE.sub("[EMP_ID]", sanitized)
        return _MULTI_SPACE_RE.sub(" ", sanitized).strip()

    @classmethod
    def normalize(cls, text: str) -> str:
        """先脱敏 PII，再做空白与尾部标点归一化。"""
        redacted = cls.redact_pii(text)
        return _PUNCT_STRIP_RE.sub("", redacted).strip()


def _tokenize_chinese(text: str) -> str:
    """使用 jieba 分词构造供 TF-IDF / 向量相似度的空格分隔词串。"""
    tokens = [tok.strip().lower() for tok in jieba.cut(text) if tok.strip()]
    return " ".join(tokens)


def hash_identifier(raw_id: str | uuid.UUID, *, salt: str = "dochub-mining") -> str:
    """生成用于统计独立用户/会话的单向脱敏哈希。"""
    return hashlib.sha256(f"{salt}:{raw_id}".encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class MiningThresholds:
    """FAQ 聚类挖掘与防刷量阈值配置。"""

    total_frequency_threshold: int = 3
    unique_user_threshold: int = 2
    unique_session_threshold: int = 2
    min_cluster_confidence: float = 0.45
    anti_spam_per_user_cap: int = 3
    min_privacy_group_size: int = 2


@dataclass(slots=True)
class MiningExecutionStats:
    """单次挖掘执行统计摘要。"""

    run_id: uuid.UUID
    processed_events: int = 0
    clusters_created: int = 0
    candidates_created: int = 0
    cluster_ids: list[uuid.UUID] = field(default_factory=list)
    candidate_ids: list[uuid.UUID] = field(default_factory=list)


def _compute_anti_spam_metrics(events: list[QuestionEvent], per_user_cap: int) -> dict[str, Any]:
    """计算去刷量有效频次、独立用户数、独立会话数及部门覆盖数。"""
    user_counts: dict[str, int] = {}
    unique_sessions: set[str] = set()
    unique_depts: set[str] = set()

    for ev in events:
        user_counts[ev.user_hash] = user_counts.get(ev.user_hash, 0) + 1
        unique_sessions.add(ev.session_hash)
        if ev.department_id is not None:
            unique_depts.add(str(ev.department_id))

    # 单用户超出防刷上限的重复提问仅按 cap 计入有效频次
    effective_freq = sum(min(cnt, per_user_cap) for cnt in user_counts.values())
    return {
        "raw_frequency": len(events),
        "effective_frequency": effective_freq,
        "unique_user_count": len(user_counts),
        "unique_session_count": len(unique_sessions),
        "department_coverage": len(unique_depts),
    }


async def record_question_event(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    space_id: uuid.UUID | None,
    user_id: uuid.UUID,
    session_id: str | uuid.UUID,
    department_id: uuid.UUID | None,
    raw_question: str,
    sensitivity_level: str,
    outcome: str,
) -> QuestionEvent:
    """脱敏用户提问并记录 QuestionEvent（自动清除 PII，严禁原始敏感信息入聚类池）。"""
    normalized = QuestionRedactor.normalize(raw_question)
    event = QuestionEvent(
        organization_id=organization_id,
        space_id=space_id,
        user_hash=hash_identifier(user_id, salt="user"),
        session_hash=hash_identifier(session_id, salt="session"),
        department_id=department_id,
        normalized_text=normalized,
        sensitivity_level=sensitivity_level,
        outcome=outcome,
    )
    session.add(event)
    await session.flush()
    return event


async def assign_question_incremental(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    space_id: uuid.UUID,
    event: QuestionEvent,
    similarity_threshold: float = 0.65,
) -> uuid.UUID | None:
    """实时增量近似匹配：将新问题绑定到同 Space 内最相似的活跃 QuestionCluster。"""
    clusters = (
        (
            await session.execute(
                select(QuestionCluster).where(
                    QuestionCluster.organization_id == organization_id,
                    QuestionCluster.space_id == space_id,
                    QuestionCluster.status == "ACTIVE",
                )
            )
        )
        .scalars()
        .all()
    )
    if not clusters:
        return None

    corpus = [_tokenize_chinese(c.representative_question) for c in clusters]
    query_tok = _tokenize_chinese(event.normalized_text)
    if not query_tok:
        return None

    vectorizer = TfidfVectorizer(token_pattern=r"(?u)\b\w+\b")
    try:
        matrix = vectorizer.fit_transform([*corpus, query_tok])
    except ValueError:
        return None

    sims = cosine_similarity(matrix[-1:], matrix[:-1])[0]
    best_idx = int(np.argmax(sims))
    best_score = float(sims[best_idx])
    if best_score < similarity_threshold:
        return None

    target_cluster = clusters[best_idx]
    existing_member = (
        await session.execute(
            select(QuestionClusterMember).where(
                QuestionClusterMember.organization_id == organization_id,
                QuestionClusterMember.cluster_id == target_cluster.id,
                QuestionClusterMember.question_event_id == event.id,
            )
        )
    ).scalar_one_or_none()
    if existing_member is None:
        session.add(
            QuestionClusterMember(
                organization_id=organization_id,
                cluster_id=target_cluster.id,
                question_event_id=event.id,
                membership_score=round(best_score, 4),
            )
        )
    return target_cluster.id


async def _find_evidence_draft_in_space(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    space_id: uuid.UUID,
    representative_question: str,
) -> tuple[str, list[dict[str, str]]]:
    """仅从同 Space 的已发布 ACTIVE 文档检索候选答案草稿与引用，严禁跨空间混拼。"""
    rows = (
        await session.execute(
            select(
                Chunk.id,
                Chunk.content_text,
                Document.id,
                Document.title,
                DocumentVersion.id,
            )
            .join(DocumentVersion, DocumentVersion.id == Chunk.document_version_id)
            .join(Document, Document.id == DocumentVersion.document_id)
            .where(
                Chunk.organization_id == organization_id,
                Document.organization_id == organization_id,
                Document.space_id == space_id,
                Document.status == "ACTIVE",
            )
            .limit(80)
        )
    ).all()

    if not rows:
        return "待人工补充权威知识来源（当前空间暂无已发布文档证据）。", []

    query_tokens = set(_tokenize_chinese(representative_question).split())
    scored: list[tuple[int, tuple[Any, ...]]] = []
    for row in rows:
        chunk_tokens = set(_tokenize_chinese(row[1]).split())
        overlap = len(query_tokens & chunk_tokens)
        scored.append((overlap, row))

    scored.sort(key=lambda item: item[0], reverse=True)
    top_hits = [item[1] for item in scored[:2] if item[0] > 0] or [scored[0][1]]

    draft_parts = [hit[1].strip() for hit in top_hits]
    citations = [
        {
            "chunk_id": str(hit[0]),
            "document_id": str(hit[2]),
            "document_title": str(hit[3]),
            "document_version_id": str(hit[4]),
        }
        for hit in top_hits
    ]
    return "\n\n".join(draft_parts)[:2000], citations


async def run_hdbscan_recluster(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    space_id: uuid.UUID | None = None,
    window_days: int = 30,
    min_cluster_size: int = 3,
    thresholds: MiningThresholds = MiningThresholds(),
) -> MiningExecutionStats:
    """使用 scikit-learn HDBSCAN 对脱敏问题事件执行按 Space 严格隔离的批量重聚类。"""
    run_id = new_uuid7()
    stats = MiningExecutionStats(run_id=run_id)
    cutoff = datetime.now(UTC) - timedelta(days=window_days)

    async with org_rls_session(session, organization_id):
        # 严格排除 ACL_DENIED 越权事件，防止受限查询内容泄露进 FAQ 候选池
        stmt = select(QuestionEvent).where(
            QuestionEvent.organization_id == organization_id,
            QuestionEvent.created_at >= cutoff,
            QuestionEvent.outcome != "ACL_DENIED",
            QuestionEvent.space_id.is_not(None),
        )
        if space_id is not None:
            stmt = stmt.where(QuestionEvent.space_id == space_id)

        all_events = list(
            (await session.execute(stmt.order_by(QuestionEvent.created_at.asc()))).scalars().all()
        )
        stats.processed_events = len(all_events)
        if not all_events:
            return stats

        # 查询有效空间名称以生成隔离的 scope_key
        space_rows = (
            await session.execute(
                select(KnowledgeSpace.id, KnowledgeSpace.slug).where(
                    KnowledgeSpace.organization_id == organization_id
                )
            )
        ).all()
        space_slug_by_id = {row.id: row.slug for row in space_rows}

        # 严格按 space_id 分片聚类，绝不跨 Space 混拼文本
        events_by_space: dict[uuid.UUID, list[QuestionEvent]] = {}
        for ev in all_events:
            if ev.space_id is not None:
                events_by_space.setdefault(ev.space_id, []).append(ev)

        effective_min_size = max(2, min_cluster_size)
        for sp_id, sp_events in events_by_space.items():
            if len(sp_events) < effective_min_size:
                continue

            tokenized_docs = [_tokenize_chinese(ev.normalized_text) for ev in sp_events]
            vectorizer = TfidfVectorizer(token_pattern=r"(?u)\b\w+\b")
            try:
                tfidf_matrix = vectorizer.fit_transform(tokenized_docs)
            except ValueError:
                continue

            dense_vectors = tfidf_matrix.toarray().astype(np.float64)
            # 当特征数极少或样本完全一致时做安全聚类分组
            if dense_vectors.shape[0] >= effective_min_size:
                clusterer = HDBSCAN(
                    min_cluster_size=effective_min_size,
                    min_samples=1,
                    metric="cosine" if dense_vectors.shape[1] > 1 else "euclidean",
                    allow_single_cluster=True,
                    copy=True,
                )
                labels = clusterer.fit_predict(dense_vectors)
                probs = getattr(clusterer, "probabilities_", np.ones(len(sp_events)))
            else:
                labels = np.zeros(len(sp_events), dtype=int)
                probs = np.ones(len(sp_events))

            # 若 HDBSCAN 将高相似度重复文本归为 -1 噪声，回退按完全规范化文本聚合保底
            grouped: dict[int, list[tuple[QuestionEvent, float]]] = {}
            noise_events: list[QuestionEvent] = []
            for ev, lbl, prob in zip(sp_events, labels, probs, strict=False):
                lbl_int = int(lbl)
                if lbl_int < 0:
                    noise_events.append(ev)
                else:
                    grouped.setdefault(lbl_int, []).append((ev, float(prob)))

            if not grouped and len(noise_events) >= effective_min_size:
                # 若余弦距离极近但默认成簇判噪，按精确归一化文本检查高频簇
                by_text: dict[str, list[QuestionEvent]] = {}
                for ev in noise_events:
                    by_text.setdefault(ev.normalized_text, []).append(ev)
                synthetic_id = 0
                for same_evs in by_text.values():
                    if len(same_evs) >= effective_min_size:
                        grouped[synthetic_id] = [(e, 1.0) for e in same_evs]
                        synthetic_id += 1

            for cluster_entries in grouped.values():
                cluster_events = [item[0] for item in cluster_entries]
                metrics = _compute_anti_spam_metrics(
                    cluster_events,
                    thresholds.anti_spam_per_user_cap,
                )
                # 隐私最小群体数与反刷量校验
                if metrics["unique_user_count"] < thresholds.min_privacy_group_size:
                    continue

                representative = cluster_events[0].normalized_text
                scope_key = f"org:{organization_id}:space:{space_slug_by_id.get(sp_id, sp_id)}"

                existing_cluster = (
                    await session.execute(
                        select(QuestionCluster).where(
                            QuestionCluster.organization_id == organization_id,
                            QuestionCluster.space_id == sp_id,
                            QuestionCluster.representative_question == representative,
                            QuestionCluster.status == "ACTIVE",
                        )
                    )
                ).scalar_one_or_none()

                if existing_cluster is None:
                    cluster_obj = QuestionCluster(
                        organization_id=organization_id,
                        space_id=sp_id,
                        scope_key=scope_key,
                        algorithm_version="sklearn-hdbscan-v1",
                        cluster_version=1,
                        representative_question=representative,
                        status="ACTIVE",
                        metrics_json=metrics,
                    )
                    session.add(cluster_obj)
                    await session.flush()
                    stats.clusters_created += 1
                    stats.cluster_ids.append(cluster_obj.id)
                else:
                    cluster_obj = existing_cluster
                    cluster_obj.cluster_version += 1
                    cluster_obj.metrics_json = metrics

                for ev, prob in cluster_entries:
                    existing_link = (
                        await session.execute(
                            select(QuestionClusterMember).where(
                                QuestionClusterMember.organization_id == organization_id,
                                QuestionClusterMember.cluster_id == cluster_obj.id,
                                QuestionClusterMember.question_event_id == ev.id,
                            )
                        )
                    ).scalar_one_or_none()
                    if existing_link is None:
                        session.add(
                            QuestionClusterMember(
                                organization_id=organization_id,
                                cluster_id=cluster_obj.id,
                                question_event_id=ev.id,
                                membership_score=round(max(0.1, min(1.0, prob)), 4),
                            )
                        )

                # 达标频次与独立用户数才生成 FAQCandidate
                if (
                    metrics["effective_frequency"] < thresholds.total_frequency_threshold
                    or metrics["unique_user_count"] < thresholds.unique_user_threshold
                    or metrics["unique_session_count"] < thresholds.unique_session_threshold
                ):
                    continue

                existing_cand = (
                    await session.execute(
                        select(FAQCandidate).where(
                            FAQCandidate.organization_id == organization_id,
                            FAQCandidate.cluster_id == cluster_obj.id,
                        )
                    )
                ).scalar_one_or_none()
                if existing_cand is not None:
                    continue

                answer_draft, citations = await _find_evidence_draft_in_space(
                    session,
                    organization_id=organization_id,
                    space_id=sp_id,
                    representative_question=representative,
                )
                avg_prob = sum(p for _, p in cluster_entries) / len(cluster_entries)
                evidence_bonus = 0.25 if citations else 0.0
                confidence_score = round(min(0.99, 0.55 * avg_prob + evidence_bonus + 0.15), 4)

                candidate = FAQCandidate(
                    organization_id=organization_id,
                    cluster_id=cluster_obj.id,
                    space_id=sp_id,
                    candidate_status="PENDING_SUBMIT",
                    generated_question=representative,
                    generated_answer_json={
                        "answer_draft": answer_draft,
                        "citations": citations,
                    },
                    confidence_json={
                        "overall_score": confidence_score,
                        "cluster_cohesion": round(avg_prob, 4),
                        "citation_count": len(citations),
                        **metrics,
                    },
                )
                session.add(candidate)
                await session.flush()
                stats.candidates_created += 1
                stats.candidate_ids.append(candidate.id)

    return stats
