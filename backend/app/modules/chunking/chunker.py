"""结构感知 Parent-Child 切片引擎（StructureAwareChunker）。

设计约束（TECHNICAL_SPEC §8.1）：
1. 支持“默认模板 -> 文档类型策略 -> Space Override”三层配置合并，并输出确定性 `config_hash`。
2. 依据 `heading_path` 与章节边界组装 `ParentChunk`，
   在 Parent 内部按 Token 预算与 Overlap 切分 `ChildChunk`。
3. 表格块（`BlockType.TABLE`）独立切分，超长表格按数据行分片时**每片自动补全表头与分隔行**。
4. 公式块（`BlockType.FORMULA`）与图片块（`BlockType.IMAGE`）严格原子化完整保留，绝不截断，
   并完整保留页码与 `bounding_box` 定位信息。
5. 每个 Parent/Child Chunk 精确记录：`parent_chunk_id`、`source_block_ids`、`page_range`、
   `content_hash`、`token_count`、`chunker_version` 与 `config_hash`。
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Sequence
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

from app.core.ids import new_uuid7_str
from app.modules.parsing.schemas import BlockType, ParsedBlockData

CHUNKER_VERSION = "structure-aware-v1.0.0"

_CJK_CHAR_RE = re.compile(r"[\u4e00-\u9fff\u3000-\u303f\uff00-\uffef]")
_ASCII_WORD_RE = re.compile(r"[A-Za-z0-9_]+|[^\s\u4e00-\u9fff\u3000-\u303f\uff00-\uffef]")
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[。！？；.!?;])\s+|\n+")
_MD_TABLE_SEP_RE = re.compile(r"^\s*\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)+\|?\s*$")

# 按文档类型提供的默认切片策略模板（可通过 Space Override 覆盖）
DEFAULT_DOC_TYPE_OVERRIDES: dict[str, dict[str, Any]] = {
    "pdf": {
        "parent_max_tokens": 1200,
        "child_max_tokens": 320,
        "child_overlap_tokens": 48,
        "table_max_tokens": 400,
    },
    "markdown": {
        "parent_max_tokens": 1400,
        "child_max_tokens": 360,
        "child_overlap_tokens": 50,
    },
    "docx": {
        "parent_max_tokens": 1200,
        "child_max_tokens": 320,
        "child_overlap_tokens": 48,
    },
    "spreadsheet": {
        "parent_max_tokens": 1600,
        "child_max_tokens": 400,
        "child_overlap_tokens": 0,
        "table_max_tokens": 360,
    },
    "json": {
        "parent_max_tokens": 1000,
        "child_max_tokens": 300,
        "child_overlap_tokens": 32,
    },
}


def estimate_token_count(text: str) -> int:
    """中英混合确定性 Token 估算器（零外部词表下载依赖）。

    规则：
    - 汉字与中文标点按约 1 Token 估算；
    - 英文单词/数字按长度折算（每 4 字符约 1 Token，至少 1 Token）；
    - 保证空文本返回 0，非空文本至少返回 1。
    """
    if not text or not text.strip():
        return 0
    cjk_count = len(_CJK_CHAR_RE.findall(text))
    non_cjk = _CJK_CHAR_RE.sub(" ", text)
    ascii_tokens = 0
    for token in _ASCII_WORD_RE.findall(non_cjk):
        ascii_tokens += max(1, math.ceil(len(token) / 4))
    return max(1, cjk_count + ascii_tokens)


class PageRange(BaseModel):
    """Chunk 溯源页码区间（1-based 闭区间）。"""

    start_page: int = Field(..., ge=1, description="起始页码（1-based）")
    end_page: int = Field(..., ge=1, description="结束页码（1-based）")

    @model_validator(mode="after")
    def _validate_order(self) -> PageRange:
        if self.end_page < self.start_page:
            raise ValueError("end_page 不能小于 start_page")
        return self


class ChunkingConfig(BaseModel):
    """结构感知切片配置（支持默认模板、文档类型策略与 Space Override）。"""

    parent_max_tokens: int = Field(
        default=1200, ge=128, le=16384, description="Parent Chunk 最大 Token 预算"
    )
    child_max_tokens: int = Field(
        default=320, ge=32, le=4096, description="Child Chunk 最大 Token 预算"
    )
    child_overlap_tokens: int = Field(
        default=48, ge=0, le=1024, description="相邻正文 Child Chunk 重叠 Token 数"
    )
    table_max_tokens: int = Field(
        default=380, ge=64, le=4096, description="单片表格 Chunk 最大 Token 预算"
    )
    preserve_table_headers: bool = Field(default=True, description="表格拆分时是否每片自动补全表头")
    keep_formula_atomic: bool = Field(default=True, description="是否保持公式块完整不截断")
    keep_image_atomic: bool = Field(default=True, description="是否保持图片/图注块完整不截断")
    include_heading_prefix: bool = Field(
        default=True,
        description="是否在 Child 文本前附加章节路径前缀增强检索上下文",
    )
    doc_type_overrides: dict[str, dict[str, Any]] = Field(
        default_factory=lambda: {k: dict(v) for k, v in DEFAULT_DOC_TYPE_OVERRIDES.items()},
        description="按文档类型覆盖策略映射",
    )
    space_overrides: dict[str, dict[str, Any]] = Field(
        default_factory=dict,
        description="按 Knowledge Space ID 覆盖策略映射",
    )

    @model_validator(mode="after")
    def _validate_budgets(self) -> ChunkingConfig:
        if self.child_max_tokens > self.parent_max_tokens:
            raise ValueError("child_max_tokens 不能大于 parent_max_tokens")
        if self.child_overlap_tokens >= self.child_max_tokens:
            raise ValueError("child_overlap_tokens 必须严格小于 child_max_tokens")
        return self

    def resolve(
        self,
        *,
        doc_type: str | None = None,
        space_id: str | None = None,
        space_override: dict[str, Any] | None = None,
    ) -> ChunkingConfig:
        """合并“默认模板 -> 文档类型策略 -> Space Override”生成生效配置。"""
        base_params = {
            "parent_max_tokens": self.parent_max_tokens,
            "child_max_tokens": self.child_max_tokens,
            "child_overlap_tokens": self.child_overlap_tokens,
            "table_max_tokens": self.table_max_tokens,
            "preserve_table_headers": self.preserve_table_headers,
            "keep_formula_atomic": self.keep_formula_atomic,
            "keep_image_atomic": self.keep_image_atomic,
            "include_heading_prefix": self.include_heading_prefix,
        }
        if doc_type:
            dt_key = doc_type.strip().lower()
            if dt_key in self.doc_type_overrides:
                base_params.update(
                    {k: v for k, v in self.doc_type_overrides[dt_key].items() if k in base_params}
                )
        if space_id and space_id in self.space_overrides:
            base_params.update(
                {k: v for k, v in self.space_overrides[space_id].items() if k in base_params}
            )
        if space_override:
            base_params.update({k: v for k, v in space_override.items() if k in base_params})

        return ChunkingConfig(
            **base_params,
            doc_type_overrides={},
            space_overrides={},
        )

    def compute_config_hash(self) -> str:
        """计算当前有效切片参数的确定性 SHA-256 哈希（用于绑定 ChunkSet）。"""
        canonical = {
            "parent_max_tokens": self.parent_max_tokens,
            "child_max_tokens": self.child_max_tokens,
            "child_overlap_tokens": self.child_overlap_tokens,
            "table_max_tokens": self.table_max_tokens,
            "preserve_table_headers": self.preserve_table_headers,
            "keep_formula_atomic": self.keep_formula_atomic,
            "keep_image_atomic": self.keep_image_atomic,
            "include_heading_prefix": self.include_heading_prefix,
        }
        encoded = json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()


class ChunkData(BaseModel):
    """Parent / Child 统一切片数据结构（对齐数据库 Chunk 实体字段）。"""

    chunk_id: str = Field(default_factory=new_uuid7_str, description="切片唯一 ID（UUID v7）")
    document_version_id: str = Field(..., description="所属文档版本 ID")
    parent_chunk_id: str | None = Field(
        default=None,
        description="父切片 ID（ParentChunk 为 None，ChildChunk 指向所属 Parent）",
    )
    chunk_role: Literal["parent", "child"] = Field(..., description="切片层级角色")
    sequence_no: int = Field(..., ge=0, description="在同层级中的顺序编号")
    heading_path: list[str] = Field(default_factory=list, description="所属章节标题路径")
    text: str = Field(..., description="纯文本检索内容")
    markdown: str = Field(..., description="结构保真 Markdown 内容")
    source_block_ids: list[str] = Field(
        default_factory=list, description="来源 ParsedBlock ID 列表"
    )
    page_range: PageRange | None = Field(default=None, description="来源页码范围（1-based）")
    content_hash: str = Field(..., description="切片正文的 SHA-256 哈希")
    token_count: int = Field(..., ge=0, description="估算 Token 数量")
    chunker_version: str = Field(default=CHUNKER_VERSION, description="切片器版本号")
    config_hash: str = Field(..., description="本次切片生效配置的 SHA-256 哈希")
    metadata: dict[str, Any] = Field(
        default_factory=dict, description="块类型、坐标框、表头标记等元数据"
    )


class ChunkSetOutput(BaseModel):
    """整篇文档的 Parent-Child 切片集合结果。"""

    document_version_id: str = Field(..., description="文档版本 ID")
    chunker_version: str = Field(..., description="切片器版本")
    config_hash: str = Field(..., description="生效配置哈希")
    parents: list[ChunkData] = Field(default_factory=list, description="ParentChunk 列表")
    children: list[ChunkData] = Field(default_factory=list, description="ChildChunk 列表")
    effective_config: ChunkingConfig = Field(..., description="最终生效的切片配置")
    metadata: dict[str, Any] = Field(default_factory=dict, description="切片统计信息")


def _compute_content_hash(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _compute_page_range(blocks: Sequence[ParsedBlockData]) -> PageRange | None:
    pages = [b.page_number for b in blocks if b.page_number is not None]
    if not pages:
        return None
    return PageRange(start_page=min(pages), end_page=max(pages))


def _collect_block_bboxes(blocks: Sequence[ParsedBlockData]) -> list[dict[str, Any]]:
    boxes: list[dict[str, Any]] = []
    for block in blocks:
        if block.bounding_box is not None:
            boxes.append(
                {
                    "block_id": block.block_id,
                    "block_type": block.block_type.value,
                    "page_number": block.page_number,
                    "bounding_box": block.bounding_box,
                }
            )
    return boxes


def _parse_markdown_table_block(markdown: str) -> tuple[list[str], str, str, list[str]] | None:
    """拆解 Markdown 表格为 `(前导标题行, 表头行, 分隔行, 数据行列表)`。"""
    lines = [line.rstrip() for line in markdown.replace("\r\n", "\n").split("\n") if line.strip()]
    for idx in range(len(lines) - 1):
        if "|" in lines[idx] and _MD_TABLE_SEP_RE.match(lines[idx + 1]):
            prefix = lines[:idx]
            header_line = lines[idx]
            sep_line = lines[idx + 1]
            data_rows = lines[idx + 2 :]
            return prefix, header_line, sep_line, data_rows
    return None


class StructureAwareChunker:
    """结构感知 Parent-Child 切片器。"""

    chunker_version: str = CHUNKER_VERSION

    def __init__(self, config: ChunkingConfig | None = None) -> None:
        self._base_config = config or ChunkingConfig()

    def chunk_blocks(
        self,
        blocks: Sequence[ParsedBlockData],
        *,
        document_version_id: str,
        doc_type: str | None = None,
        space_id: str | None = None,
        space_override: dict[str, Any] | None = None,
    ) -> ChunkSetOutput:
        """将结构化 `ParsedBlockData` 序列切分为 Parent-Child 两级切片集合。"""
        effective_cfg = self._base_config.resolve(
            doc_type=doc_type,
            space_id=space_id,
            space_override=space_override,
        )
        cfg_hash = effective_cfg.compute_config_hash()

        non_empty_blocks = [
            b for b in blocks if (b.markdown and b.markdown.strip()) or (b.text and b.text.strip())
        ]
        if not non_empty_blocks:
            return ChunkSetOutput(
                document_version_id=document_version_id,
                chunker_version=self.chunker_version,
                config_hash=cfg_hash,
                parents=[],
                children=[],
                effective_config=effective_cfg,
                metadata={"parent_count": 0, "child_count": 0, "source_block_count": 0},
            )

        # 1. 按章节边界（heading_path 与 HEADING 块）将输入块分组为章节单元
        sections = self._group_into_sections(non_empty_blocks)

        # 2. 在章节内按 parent_max_tokens 组装 ParentChunk
        parent_groups: list[tuple[list[str], list[ParsedBlockData]]] = []
        for heading_path, sec_blocks in sections:
            parent_groups.extend(
                self._pack_section_into_parents(heading_path, sec_blocks, effective_cfg)
            )

        parents: list[ChunkData] = []
        children: list[ChunkData] = []
        child_seq = 0

        for parent_seq, (heading_path, p_blocks) in enumerate(parent_groups):
            parent_md = "\n\n".join((b.markdown or b.text).strip() for b in p_blocks)
            parent_text = "\n\n".join((b.text or b.markdown).strip() for b in p_blocks)
            parent_id = new_uuid7_str()
            parent_chunk = ChunkData(
                chunk_id=parent_id,
                document_version_id=document_version_id,
                parent_chunk_id=None,
                chunk_role="parent",
                sequence_no=parent_seq,
                heading_path=list(heading_path),
                text=parent_text,
                markdown=parent_md,
                source_block_ids=[b.block_id for b in p_blocks],
                page_range=_compute_page_range(p_blocks),
                content_hash=_compute_content_hash(parent_md),
                token_count=estimate_token_count(parent_md),
                chunker_version=self.chunker_version,
                config_hash=cfg_hash,
                metadata={
                    "block_types": [b.block_type.value for b in p_blocks],
                    "bounding_boxes": _collect_block_bboxes(p_blocks),
                },
            )
            parents.append(parent_chunk)

            # 3. 在当前 Parent 内部按类型边界与 Token 预算生成 ChildChunk
            parent_children = self._build_children_for_parent(
                parent=parent_chunk,
                blocks=p_blocks,
                cfg=effective_cfg,
                cfg_hash=cfg_hash,
                start_seq=child_seq,
            )
            child_seq += len(parent_children)
            children.extend(parent_children)

        return ChunkSetOutput(
            document_version_id=document_version_id,
            chunker_version=self.chunker_version,
            config_hash=cfg_hash,
            parents=parents,
            children=children,
            effective_config=effective_cfg,
            metadata={
                "parent_count": len(parents),
                "child_count": len(children),
                "source_block_count": len(non_empty_blocks),
            },
        )

    @staticmethod
    def _group_into_sections(
        blocks: Sequence[ParsedBlockData],
    ) -> list[tuple[list[str], list[ParsedBlockData]]]:
        """依据 HEADING 块与 `heading_path` 变化切分逻辑章节。"""
        sections: list[tuple[list[str], list[ParsedBlockData]]] = []
        current_path: list[str] | None = None
        current_blocks: list[ParsedBlockData] = []

        for block in blocks:
            block_path = list(block.heading_path)
            is_new_section = block.block_type == BlockType.HEADING or (
                current_path is not None and block_path != current_path
            )
            if is_new_section and current_blocks:
                sections.append((current_path or [], current_blocks))
                current_blocks = []

            if current_blocks:
                current_blocks.append(block)
            else:
                current_path = block_path
                current_blocks = [block]

        if current_blocks:
            sections.append((current_path or [], current_blocks))
        return sections

    @staticmethod
    def _pack_section_into_parents(
        heading_path: list[str],
        blocks: Sequence[ParsedBlockData],
        cfg: ChunkingConfig,
    ) -> list[tuple[list[str], list[ParsedBlockData]]]:
        """在单个章节内按 `parent_max_tokens` 组装 Parent，同时保持单块边界不被粗暴截断。"""
        groups: list[tuple[list[str], list[ParsedBlockData]]] = []
        current: list[ParsedBlockData] = []
        current_tokens = 0

        for block in blocks:
            b_tokens = estimate_token_count(block.markdown or block.text)
            if current and current_tokens + b_tokens > cfg.parent_max_tokens:
                # 避免把单独一个标题块遗留成空壳 Parent
                if len(current) == 1 and current[0].block_type == BlockType.HEADING:
                    current.append(block)
                    groups.append((heading_path, current))
                    current = []
                    current_tokens = 0
                    continue
                groups.append((heading_path, current))
                current = [block]
                current_tokens = b_tokens
            else:
                current.append(block)
                current_tokens += b_tokens

        if current:
            groups.append((heading_path, current))
        return groups

    def _format_heading_prefix(self, heading_path: list[str], cfg: ChunkingConfig) -> str:
        if not cfg.include_heading_prefix or not heading_path:
            return ""
        return f"[章节: {' > '.join(heading_path)}]\n"

    def _build_children_for_parent(
        self,
        *,
        parent: ChunkData,
        blocks: Sequence[ParsedBlockData],
        cfg: ChunkingConfig,
        cfg_hash: str,
        start_seq: int,
    ) -> list[ChunkData]:
        """将单个 Parent 内的结构块切分为 ChildChunk（保表头、公式图片原子完整、正文重叠）。"""
        result: list[ChunkData] = []
        seq = start_seq
        prose_buffer: list[ParsedBlockData] = []

        def flush_prose_buffer() -> None:
            nonlocal seq, prose_buffer
            if not prose_buffer:
                return
            prose_chunks = self._split_prose_blocks(
                parent=parent,
                blocks=prose_buffer,
                cfg=cfg,
                cfg_hash=cfg_hash,
                start_seq=seq,
            )
            seq += len(prose_chunks)
            result.extend(prose_chunks)
            prose_buffer = []

        for block in blocks:
            if block.block_type == BlockType.TABLE:
                flush_prose_buffer()
                table_chunks = self._split_table_block(
                    parent=parent,
                    block=block,
                    cfg=cfg,
                    cfg_hash=cfg_hash,
                    start_seq=seq,
                )
                seq += len(table_chunks)
                result.extend(table_chunks)
            elif (block.block_type == BlockType.FORMULA and cfg.keep_formula_atomic) or (
                block.block_type == BlockType.IMAGE and cfg.keep_image_atomic
            ):
                flush_prose_buffer()
                atomic_chunk = self._make_atomic_child(
                    parent=parent,
                    block=block,
                    cfg=cfg,
                    cfg_hash=cfg_hash,
                    seq=seq,
                )
                seq += 1
                result.append(atomic_chunk)
            else:
                prose_buffer.append(block)

        flush_prose_buffer()
        return result

    def _make_atomic_child(
        self,
        *,
        parent: ChunkData,
        block: ParsedBlockData,
        cfg: ChunkingConfig,
        cfg_hash: str,
        seq: int,
    ) -> ChunkData:
        """为公式或图片构造不可截断的原子 ChildChunk，保留完整页码与坐标框。"""
        prefix = self._format_heading_prefix(parent.heading_path, cfg)
        body_md = (block.markdown or block.text).strip()
        body_text = (block.text or block.markdown).strip()
        full_md = f"{prefix}{body_md}" if prefix else body_md
        full_text = f"{prefix}{body_text}" if prefix else body_text

        page_range = (
            PageRange(start_page=block.page_number, end_page=block.page_number)
            if block.page_number is not None
            else parent.page_range
        )
        return ChunkData(
            document_version_id=parent.document_version_id,
            parent_chunk_id=parent.chunk_id,
            chunk_role="child",
            sequence_no=seq,
            heading_path=list(parent.heading_path),
            text=full_text,
            markdown=full_md,
            source_block_ids=[block.block_id],
            page_range=page_range,
            content_hash=_compute_content_hash(full_md),
            token_count=estimate_token_count(full_md),
            chunker_version=self.chunker_version,
            config_hash=cfg_hash,
            metadata={
                "block_types": [block.block_type.value],
                "is_atomic_block": True,
                "bounding_boxes": _collect_block_bboxes([block]),
                "source_metadata": block.metadata,
            },
        )

    def _split_table_block(
        self,
        *,
        parent: ChunkData,
        block: ParsedBlockData,
        cfg: ChunkingConfig,
        cfg_hash: str,
        start_seq: int,
    ) -> list[ChunkData]:
        """独立切分表格块：若超长则按数据行拆分，并保证每片自动补全表头与分隔行。"""
        prefix = self._format_heading_prefix(parent.heading_path, cfg)
        raw_md = (block.markdown or block.text).strip()
        page_range = (
            PageRange(start_page=block.page_number, end_page=block.page_number)
            if block.page_number is not None
            else parent.page_range
        )

        parsed_table = _parse_markdown_table_block(raw_md) if cfg.preserve_table_headers else None
        full_candidate = f"{prefix}{raw_md}" if prefix else raw_md

        # 非标准 Markdown 表格或整体未超出预算时，保持单块完整输出
        if parsed_table is None or estimate_token_count(full_candidate) <= cfg.table_max_tokens:
            return [
                ChunkData(
                    document_version_id=parent.document_version_id,
                    parent_chunk_id=parent.chunk_id,
                    chunk_role="child",
                    sequence_no=start_seq,
                    heading_path=list(parent.heading_path),
                    text=full_candidate,
                    markdown=full_candidate,
                    source_block_ids=[block.block_id],
                    page_range=page_range,
                    content_hash=_compute_content_hash(full_candidate),
                    token_count=estimate_token_count(full_candidate),
                    chunker_version=self.chunker_version,
                    config_hash=cfg_hash,
                    metadata={
                        "block_types": [BlockType.TABLE.value],
                        "is_table_chunk": True,
                        "table_header_preserved": parsed_table is not None,
                        "table_part_index": 0,
                        "table_total_parts": 1,
                        "headers": block.metadata.get("headers"),
                        "bounding_boxes": _collect_block_bboxes([block]),
                    },
                )
            ]

        caption_lines, header_line, sep_line, data_rows = parsed_table
        header_block_lines = [*caption_lines, header_line, sep_line]
        header_block = "\n".join(header_block_lines)
        header_tokens = estimate_token_count(f"{prefix}{header_block}" if prefix else header_block)
        row_budget = max(32, cfg.table_max_tokens - header_tokens)

        row_groups: list[list[str]] = []
        curr_rows: list[str] = []
        curr_row_tokens = 0
        for row in data_rows:
            r_tokens = estimate_token_count(row)
            if curr_rows and curr_row_tokens + r_tokens > row_budget:
                row_groups.append(curr_rows)
                curr_rows = [row]
                curr_row_tokens = r_tokens
            else:
                curr_rows.append(row)
                curr_row_tokens += r_tokens
        if curr_rows:
            row_groups.append(curr_rows)
        if not row_groups:
            row_groups = [[]]

        total_parts = len(row_groups)
        chunks: list[ChunkData] = []
        for part_idx, group_rows in enumerate(row_groups):
            table_piece = "\n".join([*header_block_lines, *group_rows])
            content = f"{prefix}{table_piece}" if prefix else table_piece
            chunks.append(
                ChunkData(
                    document_version_id=parent.document_version_id,
                    parent_chunk_id=parent.chunk_id,
                    chunk_role="child",
                    sequence_no=start_seq + part_idx,
                    heading_path=list(parent.heading_path),
                    text=content,
                    markdown=content,
                    source_block_ids=[block.block_id],
                    page_range=page_range,
                    content_hash=_compute_content_hash(content),
                    token_count=estimate_token_count(content),
                    chunker_version=self.chunker_version,
                    config_hash=cfg_hash,
                    metadata={
                        "block_types": [BlockType.TABLE.value],
                        "is_table_chunk": True,
                        "table_header_preserved": True,
                        "table_part_index": part_idx,
                        "table_total_parts": total_parts,
                        "headers": block.metadata.get("headers"),
                        "bounding_boxes": _collect_block_bboxes([block]),
                    },
                )
            )
        return chunks

    @staticmethod
    def _split_long_text_into_units(text: str, max_tokens: int) -> list[str]:
        """将超长单块文本按句子或换行符拆分为不超过预算的细粒度片段。"""
        if estimate_token_count(text) <= max_tokens:
            return [text]

        sentences = [s.strip() for s in _SENTENCE_SPLIT_RE.split(text) if s.strip()]
        units: list[str] = []
        for sent in sentences:
            if estimate_token_count(sent) <= max_tokens:
                units.append(sent)
            else:
                # 极端超长无标点长句按字符窗口安全切分
                char_step = max(16, int(max_tokens * 0.9))
                for start in range(0, len(sent), char_step):
                    piece = sent[start : start + char_step].strip()
                    if piece:
                        units.append(piece)
        return units or [text]

    def _split_prose_blocks(
        self,
        *,
        parent: ChunkData,
        blocks: Sequence[ParsedBlockData],
        cfg: ChunkingConfig,
        cfg_hash: str,
        start_seq: int,
    ) -> list[ChunkData]:
        """按 Token 预算与 Overlap 切分普通文本/列表/代码块，同时精确维护每个切片的来源块与页码。"""
        prefix = self._format_heading_prefix(parent.heading_path, cfg)
        prefix_tokens = estimate_token_count(prefix) if prefix else 0
        body_budget = max(24, cfg.child_max_tokens - prefix_tokens)

        # 将各块展开为带溯源元数据的最小片段 (text_piece, source_block)
        units: list[tuple[str, ParsedBlockData]] = []
        for block in blocks:
            block_content = (block.markdown or block.text).strip()
            for piece in self._split_long_text_into_units(block_content, body_budget):
                units.append((piece, block))

        if not units:
            return []

        windows: list[list[tuple[str, ParsedBlockData]]] = []
        current_win: list[tuple[str, ParsedBlockData]] = []
        current_tokens = 0

        for piece, block in units:
            p_tokens = estimate_token_count(piece)
            if current_win and current_tokens + p_tokens > body_budget:
                windows.append(current_win)

                # 计算满足 child_overlap_tokens 的尾部重叠片段
                overlap_win: list[tuple[str, ParsedBlockData]] = []
                overlap_tokens = 0
                if cfg.child_overlap_tokens > 0:
                    for prev_piece, prev_block in reversed(current_win):
                        prev_t = estimate_token_count(prev_piece)
                        if overlap_win and overlap_tokens + prev_t > cfg.child_overlap_tokens:
                            break
                        if prev_t > cfg.child_overlap_tokens and not overlap_win:
                            # 若末尾单句本身超过 overlap 预算，截取其尾部若干字符作为语义衔接
                            tail_chars = max(12, cfg.child_overlap_tokens)
                            overlap_win.insert(0, (prev_piece[-tail_chars:], prev_block))
                            overlap_tokens += estimate_token_count(prev_piece[-tail_chars:])
                            break
                        overlap_win.insert(0, (prev_piece, prev_block))
                        overlap_tokens += prev_t

                current_win = [*overlap_win, (piece, block)]
                current_tokens = sum(estimate_token_count(x[0]) for x in current_win)
            else:
                current_win.append((piece, block))
                current_tokens += p_tokens

        if current_win:
            windows.append(current_win)

        chunks: list[ChunkData] = []
        for offset, win in enumerate(windows):
            body = "\n".join(piece for piece, _ in win).strip()
            full_md = f"{prefix}{body}" if prefix else body
            contributing_blocks: list[ParsedBlockData] = []
            seen_ids: set[str] = set()
            for _, blk in win:
                if blk.block_id not in seen_ids:
                    seen_ids.add(blk.block_id)
                    contributing_blocks.append(blk)

            page_range = _compute_page_range(contributing_blocks) or parent.page_range
            chunks.append(
                ChunkData(
                    document_version_id=parent.document_version_id,
                    parent_chunk_id=parent.chunk_id,
                    chunk_role="child",
                    sequence_no=start_seq + offset,
                    heading_path=list(parent.heading_path),
                    text=full_md,
                    markdown=full_md,
                    source_block_ids=[b.block_id for b in contributing_blocks],
                    page_range=page_range,
                    content_hash=_compute_content_hash(full_md),
                    token_count=estimate_token_count(full_md),
                    chunker_version=self.chunker_version,
                    config_hash=cfg_hash,
                    metadata={
                        "block_types": list(
                            dict.fromkeys(b.block_type.value for b in contributing_blocks)
                        ),
                        "has_overlap": offset > 0 and cfg.child_overlap_tokens > 0,
                        "bounding_boxes": _collect_block_bboxes(contributing_blocks),
                    },
                )
            )
        return chunks
