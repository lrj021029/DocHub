"""本地专用轻量文档解析器集合（零 Mock，直接处理真实文件字节）。

包含 7 类真实解析器（均继承 `SyncLightweightParser` 并实现 `DocumentParser` 协议）：
1. `MarkdownParser`：维护 `#` 标题层级 `heading_path`，识别代码块、表格、公式、列表、图片与段落。
2. `DocxParser`：基于 `python-docx` 按原始顺序提取 Heading、段落、列表与表格并生成 Markdown 表头。
3. `TxtParser`：支持 UTF-8 / UTF-8-SIG / GB18030 / GBK 自动编码探测与空行分段。
4. `ExcelParser`：基于 `openpyxl` 按 Sheet 提取表头与数据行，输出结构化表格块与 Markdown。
5. `CsvParser`：基于标准 `csv` 模块自动探测编码与分隔符，保留表头与行列结构。
6. `JsonParser`：格式化提取 JSON 对象/数组层级结构，保留完整键路径 `heading_path`。
7. `PlainPdfFallbackParser`：基于 `pypdf` 快速提取纯文本 PDF，并自动检测是否为需要 OCR 的扫描件。
"""

from __future__ import annotations

import csv
import io
import json
import re
from typing import Any

import openpyxl
from docx import Document as DocxDocument
from docx.oxml.table import CT_Tbl
from docx.oxml.text.paragraph import CT_P
from docx.table import Table as DocxTable
from docx.text.paragraph import Paragraph as DocxParagraph
from pypdf import PdfReader

from app.core.errors import ValidationError
from app.modules.parsing.base import SyncLightweightParser
from app.modules.parsing.schemas import BlockType, ParseArtifactBundle, ParsedBlockData

_ATX_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")
_TABLE_SEP_RE = re.compile(r"^\s*\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)+\|?\s*$")
_LIST_ITEM_RE = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+.+$")
_IMAGE_LINE_RE = re.compile(r"^!\[(?P<alt>[^\]]*)\]\((?P<src>[^)]+)\)\s*$")
_DOCX_HEADING_RE = re.compile(r"^(?:heading|标题)\s*(\d+)$", re.IGNORECASE)


def decode_text_bytes(raw: bytes) -> tuple[str, str]:
    """按 UTF-8-SIG -> UTF-8 -> GB18030 -> GBK 顺序自动探测并解码文本字节。"""
    if raw.startswith(b"\xef\xbb\xbf"):
        return raw.decode("utf-8-sig"), "utf-8-sig"
    for encoding in ("utf-8", "gb18030", "gbk"):
        try:
            return raw.decode(encoding), encoding
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace"), "utf-8-replace"


def _escape_md_cell(value: Any) -> str:
    """转义表格单元格内的管道符与换行符，保证生成的 Markdown 表格结构完整。"""
    if value is None:
        return ""
    text = str(value).strip()
    return text.replace("|", r"\|").replace("\r\n", " ").replace("\n", " ")


def build_markdown_table(headers: list[str], rows: list[list[str]]) -> str:
    """将表头与数据行组装为标准 GitHub Flavored Markdown 表格。"""
    col_count = max(len(headers), max((len(r) for r in rows), default=0))
    if col_count == 0:
        return ""
    norm_headers = [
        _escape_md_cell(headers[i] if i < len(headers) else f"列{i + 1}") for i in range(col_count)
    ]
    lines = [
        "| " + " | ".join(norm_headers) + " |",
        "| " + " | ".join("---" for _ in range(col_count)) + " |",
    ]
    for row in rows:
        padded = [_escape_md_cell(row[i] if i < len(row) else "") for i in range(col_count)]
        lines.append("| " + " | ".join(padded) + " |")
    return "\n".join(lines)


def _split_markdown_table_row(line: str) -> list[str]:
    stripped = line.strip()
    if stripped.startswith("|"):
        stripped = stripped[1:]
    if stripped.endswith("|"):
        stripped = stripped[:-1]
    return [cell.strip() for cell in stripped.split("|")]


class MarkdownParser(SyncLightweightParser):
    """Markdown 文档专用解析器：精确维护 `#` 标题层级、表格、代码块、公式与段落。"""

    parser_provider = "lightweight_markdown"
    parser_version = "1.0.0"
    supported_extensions = (".md", ".markdown", ".mdx")
    supported_mime_types = ("text/markdown", "text/x-markdown")

    def parse_bytes(
        self,
        file_bytes: bytes,
        *,
        filename: str,
        mime_type: str = "",
        extra_options: dict[str, Any] | None = None,
    ) -> ParseArtifactBundle:
        content, encoding = decode_text_bytes(file_bytes)
        lines = content.replace("\r\n", "\n").replace("\r", "\n").split("\n")

        blocks: list[ParsedBlockData] = []
        heading_stack: list[tuple[int, str]] = []
        idx = 0
        total = len(lines)

        while idx < total:
            line = lines[idx]
            stripped = line.strip()

            if not stripped:
                idx += 1
                continue

            # 1. ATX 标题（# ~ ######）
            heading_match = _ATX_HEADING_RE.match(stripped)
            if heading_match:
                level = len(heading_match.group(1))
                title = heading_match.group(2).strip()
                while heading_stack and heading_stack[-1][0] >= level:
                    heading_stack.pop()
                heading_stack.append((level, title))
                path = [t for _, t in heading_stack]
                blocks.append(
                    ParsedBlockData(
                        block_type=BlockType.HEADING,
                        heading_path=path,
                        text=title,
                        markdown=f"{'#' * level} {title}",
                        metadata={"level": level, "filename": filename},
                    )
                )
                idx += 1
                continue

            current_path = [t for _, t in heading_stack]

            # 2. 围栏代码块（``` 或 ~~~）
            if stripped.startswith("```") or stripped.startswith("~~~"):
                fence = stripped[:3]
                lang = stripped[3:].strip()
                code_lines: list[str] = []
                idx += 1
                while idx < total and not lines[idx].strip().startswith(fence):
                    code_lines.append(lines[idx])
                    idx += 1
                if idx < total:
                    idx += 1
                code_body = "\n".join(code_lines)
                blocks.append(
                    ParsedBlockData(
                        block_type=BlockType.CODE,
                        heading_path=current_path,
                        text=code_body,
                        markdown=f"```{lang}\n{code_body}\n```",
                        metadata={"language": lang, "filename": filename},
                    )
                )
                continue

            # 3. 块级 LaTeX 公式（$$ ... $$）
            if stripped.startswith("$$"):
                if stripped.endswith("$$") and len(stripped) > 4:
                    formula_body = stripped[2:-2].strip()
                    idx += 1
                else:
                    formula_lines: list[str] = []
                    rest = stripped[2:].strip()
                    if rest:
                        formula_lines.append(rest)
                    idx += 1
                    while idx < total and not lines[idx].strip().endswith("$$"):
                        formula_lines.append(lines[idx])
                        idx += 1
                    if idx < total:
                        tail = lines[idx].strip()[:-2].strip()
                        if tail:
                            formula_lines.append(tail)
                        idx += 1
                    formula_body = "\n".join(formula_lines).strip()
                blocks.append(
                    ParsedBlockData(
                        block_type=BlockType.FORMULA,
                        heading_path=current_path,
                        text=formula_body,
                        markdown=f"$$\n{formula_body}\n$$",
                        metadata={"filename": filename},
                    )
                )
                continue

            # 4. Markdown 表格（表头行 + 分隔行 + 数据行）
            if "|" in line and idx + 1 < total and _TABLE_SEP_RE.match(lines[idx + 1]):
                table_lines = [lines[idx], lines[idx + 1]]
                headers = _split_markdown_table_row(lines[idx])
                idx += 2
                row_count = 0
                while idx < total and lines[idx].strip() and "|" in lines[idx]:
                    table_lines.append(lines[idx])
                    row_count += 1
                    idx += 1
                table_md = "\n".join(table_lines)
                blocks.append(
                    ParsedBlockData(
                        block_type=BlockType.TABLE,
                        heading_path=current_path,
                        text=table_md,
                        markdown=table_md,
                        metadata={
                            "headers": headers,
                            "row_count": row_count,
                            "filename": filename,
                        },
                    )
                )
                continue

            # 5. 独立图片行
            img_match = _IMAGE_LINE_RE.match(stripped)
            if img_match:
                alt = img_match.group("alt").strip()
                src = img_match.group("src").strip()
                blocks.append(
                    ParsedBlockData(
                        block_type=BlockType.IMAGE,
                        heading_path=current_path,
                        text=alt or src,
                        markdown=stripped,
                        metadata={"alt": alt, "src": src, "filename": filename},
                    )
                )
                idx += 1
                continue

            # 6. 列表块
            if _LIST_ITEM_RE.match(line):
                list_lines: list[str] = []
                while idx < total and lines[idx].strip() and _LIST_ITEM_RE.match(lines[idx]):
                    list_lines.append(lines[idx].rstrip())
                    idx += 1
                list_text = "\n".join(list_lines)
                blocks.append(
                    ParsedBlockData(
                        block_type=BlockType.LIST,
                        heading_path=current_path,
                        text=list_text,
                        markdown=list_text,
                        metadata={"item_count": len(list_lines), "filename": filename},
                    )
                )
                continue

            # 7. 普通段落
            para_lines: list[str] = []
            while idx < total:
                curr = lines[idx]
                curr_s = curr.strip()
                if not curr_s:
                    break
                if (
                    _ATX_HEADING_RE.match(curr_s)
                    or curr_s.startswith("```")
                    or curr_s.startswith("~~~")
                    or curr_s.startswith("$$")
                    or _IMAGE_LINE_RE.match(curr_s)
                    or _LIST_ITEM_RE.match(curr)
                    or ("|" in curr and idx + 1 < total and _TABLE_SEP_RE.match(lines[idx + 1]))
                ):
                    break
                para_lines.append(curr.rstrip())
                idx += 1
            para_text = "\n".join(para_lines).strip()
            if para_text:
                blocks.append(
                    ParsedBlockData(
                        block_type=BlockType.PARAGRAPH,
                        heading_path=current_path,
                        text=para_text,
                        markdown=para_text,
                        metadata={"filename": filename},
                    )
                )

        markdown = "\n\n".join(b.markdown for b in blocks if b.markdown)
        return ParseArtifactBundle(
            blocks=blocks,
            markdown=markdown,
            raw_json={
                "filename": filename,
                "encoding": encoding,
                "block_count": len(blocks),
            },
            rendered_pages=[],
            parser_provider=self.parser_provider,
            parser_version=self.parser_version,
            sha256=self.compute_sha256(file_bytes),
            metadata={"encoding": encoding, "block_count": len(blocks)},
        )


class DocxParser(SyncLightweightParser):
    """DOCX 文档专用解析器（基于 `python-docx`，按原始顺序提取标题层级、段落与表格）。"""

    parser_provider = "lightweight_docx"
    parser_version = "1.0.0"
    supported_extensions = (".docx",)
    supported_mime_types = (
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    )

    @staticmethod
    def _detect_heading_level(paragraph: DocxParagraph) -> int | None:
        style_name = (paragraph.style.name if paragraph.style else "") or ""
        if style_name.strip().lower() == "title":
            return 1
        match = _DOCX_HEADING_RE.match(style_name.strip())
        if match:
            return min(max(int(match.group(1)), 1), 6)
        return None

    @staticmethod
    def _is_list_paragraph(paragraph: DocxParagraph) -> bool:
        style_name = ((paragraph.style.name if paragraph.style else "") or "").lower()
        if "list" in style_name or "列表" in style_name:
            return True
        ppr = paragraph._p.pPr  # noqa: SLF001
        return ppr is not None and ppr.numPr is not None

    def parse_bytes(
        self,
        file_bytes: bytes,
        *,
        filename: str,
        mime_type: str = "",
        extra_options: dict[str, Any] | None = None,
    ) -> ParseArtifactBundle:
        try:
            doc = DocxDocument(io.BytesIO(file_bytes))
        except Exception as exc:
            raise ValidationError(
                f"无法解析 DOCX 文件（{filename}）：文件格式损坏或不受支持"
            ) from exc

        blocks: list[ParsedBlockData] = []
        heading_stack: list[tuple[int, str]] = []
        table_count = 0

        # 按 Word 文档 body 原始 XML 顺序遍历，确保段落与表格相对位置严格保真
        for child in doc.element.body.iterchildren():
            if isinstance(child, CT_P):
                para = DocxParagraph(child, doc)
                text = para.text.strip()
                if not text:
                    continue
                heading_level = self._detect_heading_level(para)
                if heading_level is not None:
                    while heading_stack and heading_stack[-1][0] >= heading_level:
                        heading_stack.pop()
                    heading_stack.append((heading_level, text))
                    path = [t for _, t in heading_stack]
                    blocks.append(
                        ParsedBlockData(
                            block_type=BlockType.HEADING,
                            heading_path=path,
                            text=text,
                            markdown=f"{'#' * heading_level} {text}",
                            metadata={"level": heading_level, "filename": filename},
                        )
                    )
                elif self._is_list_paragraph(para):
                    current_path = [t for _, t in heading_stack]
                    blocks.append(
                        ParsedBlockData(
                            block_type=BlockType.LIST,
                            heading_path=current_path,
                            text=text,
                            markdown=f"- {text}",
                            metadata={"filename": filename},
                        )
                    )
                else:
                    current_path = [t for _, t in heading_stack]
                    blocks.append(
                        ParsedBlockData(
                            block_type=BlockType.PARAGRAPH,
                            heading_path=current_path,
                            text=text,
                            markdown=text,
                            metadata={"filename": filename},
                        )
                    )
            elif isinstance(child, CT_Tbl):
                table = DocxTable(child, doc)
                raw_rows: list[list[str]] = []
                for row in table.rows:
                    cells = [cell.text.strip().replace("\n", " ") for cell in row.cells]
                    if any(cells):
                        raw_rows.append(cells)
                if not raw_rows:
                    continue
                table_count += 1
                headers = raw_rows[0]
                data_rows = raw_rows[1:]
                table_md = build_markdown_table(headers, data_rows)
                current_path = [t for _, t in heading_stack]
                blocks.append(
                    ParsedBlockData(
                        block_type=BlockType.TABLE,
                        heading_path=current_path,
                        text=table_md,
                        markdown=table_md,
                        metadata={
                            "table_index": table_count,
                            "headers": headers,
                            "row_count": len(data_rows),
                            "filename": filename,
                        },
                    )
                )

        markdown = "\n\n".join(b.markdown for b in blocks if b.markdown)
        return ParseArtifactBundle(
            blocks=blocks,
            markdown=markdown,
            raw_json={
                "filename": filename,
                "block_count": len(blocks),
                "table_count": table_count,
            },
            rendered_pages=[],
            parser_provider=self.parser_provider,
            parser_version=self.parser_version,
            sha256=self.compute_sha256(file_bytes),
            metadata={"block_count": len(blocks), "table_count": table_count},
        )


class TxtParser(SyncLightweightParser):
    """纯文本文档解析器（支持 UTF-8 / GBK 自动编码探测与空行分段）。"""

    parser_provider = "lightweight_txt"
    parser_version = "1.0.0"
    supported_extensions = (".txt", ".log", ".text")
    supported_mime_types = ("text/plain",)

    def parse_bytes(
        self,
        file_bytes: bytes,
        *,
        filename: str,
        mime_type: str = "",
        extra_options: dict[str, Any] | None = None,
    ) -> ParseArtifactBundle:
        text, encoding = decode_text_bytes(file_bytes)
        normalized = text.replace("\r\n", "\n").replace("\r", "\n")
        paragraphs = [p.strip() for p in re.split(r"\n\s*\n+", normalized) if p.strip()]

        blocks = [
            ParsedBlockData(
                block_type=BlockType.PARAGRAPH,
                heading_path=[],
                text=para,
                markdown=para,
                metadata={"paragraph_index": idx, "encoding": encoding, "filename": filename},
            )
            for idx, para in enumerate(paragraphs)
        ]
        markdown = "\n\n".join(b.markdown for b in blocks)
        return ParseArtifactBundle(
            blocks=blocks,
            markdown=markdown,
            raw_json={
                "filename": filename,
                "encoding": encoding,
                "paragraph_count": len(blocks),
            },
            rendered_pages=[],
            parser_provider=self.parser_provider,
            parser_version=self.parser_version,
            sha256=self.compute_sha256(file_bytes),
            metadata={"encoding": encoding, "paragraph_count": len(blocks)},
        )


class ExcelParser(SyncLightweightParser):
    """Excel 工作簿专用解析器（基于 `openpyxl`，按 Sheet 提取表头与数据行）。"""

    parser_provider = "lightweight_excel"
    parser_version = "1.0.0"
    supported_extensions = (".xlsx", ".xlsm")
    supported_mime_types = (
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "application/vnd.ms-excel",
    )

    def parse_bytes(
        self,
        file_bytes: bytes,
        *,
        filename: str,
        mime_type: str = "",
        extra_options: dict[str, Any] | None = None,
    ) -> ParseArtifactBundle:
        try:
            wb = openpyxl.load_workbook(io.BytesIO(file_bytes), data_only=True, read_only=True)
        except Exception as exc:
            raise ValidationError(
                f"无法解析 Excel 文件（{filename}）：文件损坏或格式不兼容"
            ) from exc

        blocks: list[ParsedBlockData] = []
        sheet_summaries: list[dict[str, Any]] = []
        try:
            for sheet_idx, sheet_name in enumerate(wb.sheetnames, start=1):
                ws = wb[sheet_name]
                extracted_rows: list[list[str]] = []
                for row in ws.iter_rows(values_only=True):
                    cells = ["" if cell is None else str(cell).strip() for cell in row]
                    if any(cells):
                        extracted_rows.append(cells)

                if not extracted_rows:
                    continue

                # 裁掉末尾连续全空列，避免 Excel 格式空白列污染表格
                max_non_empty_col = max(
                    (idx + 1 for row in extracted_rows for idx, val in enumerate(row) if val),
                    default=0,
                )
                trimmed_rows = [row[:max_non_empty_col] for row in extracted_rows]
                headers = trimmed_rows[0]
                data_rows = trimmed_rows[1:]
                table_md = build_markdown_table(headers, data_rows)

                blocks.append(
                    ParsedBlockData(
                        block_type=BlockType.HEADING,
                        page_number=sheet_idx,
                        heading_path=[sheet_name],
                        text=sheet_name,
                        markdown=f"## {sheet_name}",
                        metadata={"level": 2, "sheet_name": sheet_name, "filename": filename},
                    )
                )
                blocks.append(
                    ParsedBlockData(
                        block_type=BlockType.TABLE,
                        page_number=sheet_idx,
                        heading_path=[sheet_name],
                        text=table_md,
                        markdown=table_md,
                        metadata={
                            "sheet_name": sheet_name,
                            "headers": headers,
                            "row_count": len(data_rows),
                            "filename": filename,
                        },
                    )
                )
                sheet_summaries.append(
                    {
                        "sheet_name": sheet_name,
                        "headers": headers,
                        "row_count": len(data_rows),
                    }
                )
        finally:
            wb.close()

        markdown = "\n\n".join(b.markdown for b in blocks if b.markdown)
        return ParseArtifactBundle(
            blocks=blocks,
            markdown=markdown,
            raw_json={"filename": filename, "sheets": sheet_summaries},
            rendered_pages=[],
            parser_provider=self.parser_provider,
            parser_version=self.parser_version,
            sha256=self.compute_sha256(file_bytes),
            metadata={"sheet_count": len(sheet_summaries), "block_count": len(blocks)},
        )


class CsvParser(SyncLightweightParser):
    """CSV 表格专用解析器（标准 `csv` 模块，自动探测编码与表头行列结构）。"""

    parser_provider = "lightweight_csv"
    parser_version = "1.0.0"
    supported_extensions = (".csv", ".tsv")
    supported_mime_types = ("text/csv", "application/csv", "text/tab-separated-values")

    def parse_bytes(
        self,
        file_bytes: bytes,
        *,
        filename: str,
        mime_type: str = "",
        extra_options: dict[str, Any] | None = None,
    ) -> ParseArtifactBundle:
        text, encoding = decode_text_bytes(file_bytes)
        sample = text[:4096]
        delimiter = "\t" if filename.lower().endswith(".tsv") else ","
        if sample.strip() and not filename.lower().endswith(".tsv"):
            try:
                dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
                delimiter = dialect.delimiter
            except csv.Error:
                delimiter = ","

        reader = csv.reader(io.StringIO(text), delimiter=delimiter)
        rows = [[cell.strip() for cell in row] for row in reader if any(c.strip() for c in row)]

        blocks: list[ParsedBlockData] = []
        headers: list[str] = []
        data_rows: list[list[str]] = []
        if rows:
            headers = rows[0]
            data_rows = rows[1:]
            table_md = build_markdown_table(headers, data_rows)
            blocks.append(
                ParsedBlockData(
                    block_type=BlockType.TABLE,
                    heading_path=[filename],
                    text=table_md,
                    markdown=table_md,
                    metadata={
                        "headers": headers,
                        "row_count": len(data_rows),
                        "delimiter": delimiter,
                        "encoding": encoding,
                        "filename": filename,
                    },
                )
            )

        markdown = "\n\n".join(b.markdown for b in blocks)
        return ParseArtifactBundle(
            blocks=blocks,
            markdown=markdown,
            raw_json={
                "filename": filename,
                "encoding": encoding,
                "delimiter": delimiter,
                "headers": headers,
                "row_count": len(data_rows),
            },
            rendered_pages=[],
            parser_provider=self.parser_provider,
            parser_version=self.parser_version,
            sha256=self.compute_sha256(file_bytes),
            metadata={
                "encoding": encoding,
                "headers": headers,
                "row_count": len(data_rows),
            },
        )


class JsonParser(SyncLightweightParser):
    """JSON 结构化文档解析器：保留对象/数组键路径并格式化输出结构块。"""

    parser_provider = "lightweight_json"
    parser_version = "1.0.0"
    supported_extensions = (".json",)
    supported_mime_types = ("application/json", "text/json")

    def _collect_blocks(
        self,
        node: Any,
        *,
        path: list[str],
        blocks: list[ParsedBlockData],
        filename: str,
    ) -> None:
        if isinstance(node, dict):
            scalar_lines: list[str] = []
            nested_items: list[tuple[str, Any]] = []
            for key, val in node.items():
                key_str = str(key)
                if isinstance(val, (dict, list)):
                    nested_items.append((key_str, val))
                else:
                    scalar_lines.append(f"- **{key_str}**: {val}")

            if path:
                title = path[-1]
                level = min(len(path), 6)
                blocks.append(
                    ParsedBlockData(
                        block_type=BlockType.HEADING,
                        heading_path=list(path),
                        text=title,
                        markdown=f"{'#' * level} {title}",
                        metadata={"key_path": ".".join(path), "filename": filename},
                    )
                )
            if scalar_lines:
                summary = "\n".join(scalar_lines)
                blocks.append(
                    ParsedBlockData(
                        block_type=BlockType.PARAGRAPH,
                        heading_path=list(path),
                        text=summary,
                        markdown=summary,
                        metadata={"key_path": ".".join(path), "filename": filename},
                    )
                )
            for child_key, child_val in nested_items:
                self._collect_blocks(
                    child_val,
                    path=[*path, child_key],
                    blocks=blocks,
                    filename=filename,
                )
        elif isinstance(node, list):
            if path:
                title = path[-1]
                level = min(len(path), 6)
                blocks.append(
                    ParsedBlockData(
                        block_type=BlockType.HEADING,
                        heading_path=list(path),
                        text=title,
                        markdown=f"{'#' * level} {title}",
                        metadata={
                            "key_path": ".".join(path),
                            "array_length": len(node),
                            "filename": filename,
                        },
                    )
                )
            # 若数组全部为扁平同构对象，自动转为 Markdown 表格增强检索体验
            if node and all(
                isinstance(item, dict)
                and all(not isinstance(v, (dict, list)) for v in item.values())
                for item in node
            ):
                headers: list[str] = []
                for item in node:
                    for k in item:
                        ks = str(k)
                        if ks not in headers:
                            headers.append(ks)
                rows = [[str(item.get(h, "")) for h in headers] for item in node]
                table_md = build_markdown_table(headers, rows)
                blocks.append(
                    ParsedBlockData(
                        block_type=BlockType.TABLE,
                        heading_path=list(path),
                        text=table_md,
                        markdown=table_md,
                        metadata={
                            "key_path": ".".join(path),
                            "headers": headers,
                            "row_count": len(rows),
                            "filename": filename,
                        },
                    )
                )
            else:
                formatted = json.dumps(node, ensure_ascii=False, indent=2)
                blocks.append(
                    ParsedBlockData(
                        block_type=BlockType.CODE,
                        heading_path=list(path),
                        text=formatted,
                        markdown=f"```json\n{formatted}\n```",
                        metadata={
                            "language": "json",
                            "key_path": ".".join(path),
                            "array_length": len(node),
                            "filename": filename,
                        },
                    )
                )
        else:
            val_str = str(node)
            blocks.append(
                ParsedBlockData(
                    block_type=BlockType.PARAGRAPH,
                    heading_path=list(path),
                    text=val_str,
                    markdown=val_str,
                    metadata={"key_path": ".".join(path), "filename": filename},
                )
            )

    def parse_bytes(
        self,
        file_bytes: bytes,
        *,
        filename: str,
        mime_type: str = "",
        extra_options: dict[str, Any] | None = None,
    ) -> ParseArtifactBundle:
        text, encoding = decode_text_bytes(file_bytes)
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ValidationError(f"JSON 文件语法无效（{filename}）：{exc.msg}") from exc

        blocks: list[ParsedBlockData] = []
        self._collect_blocks(parsed, path=[], blocks=blocks, filename=filename)
        markdown = "\n\n".join(b.markdown for b in blocks if b.markdown)
        return ParseArtifactBundle(
            blocks=blocks,
            markdown=markdown,
            raw_json=parsed if isinstance(parsed, (dict, list)) else {"value": parsed},
            rendered_pages=[],
            parser_provider=self.parser_provider,
            parser_version=self.parser_version,
            sha256=self.compute_sha256(file_bytes),
            metadata={"encoding": encoding, "block_count": len(blocks)},
        )


class PlainPdfFallbackParser(SyncLightweightParser):
    """纯文本 PDF 本机快速解析器（基于 `pypdf`），具备扫描件自动识别能力。

    当检测到 PDF 提取文本极少（疑似扫描件或纯图片 PDF）时，
    在 `bundle.metadata` 中显式标记 `is_scanned=True` 与 `needs_ocr=True`，
    供 `ParserRouter` 决策走 MinerU（L0/授权 L1）或本地 OCR / Fail-Closed（L2/L3）。
    """

    parser_provider = "lightweight_plain_pdf"
    parser_version = "1.0.0"
    supported_extensions = (".pdf",)
    supported_mime_types = ("application/pdf",)

    def __init__(
        self,
        *,
        min_chars_per_page: int = 20,
        min_total_chars: int = 20,
        max_cache_entries: int = 128,
    ) -> None:
        super().__init__(max_cache_entries=max_cache_entries)
        self.min_chars_per_page = min_chars_per_page
        self.min_total_chars = min_total_chars

    def inspect_pdf_bytes(self, file_bytes: bytes) -> dict[str, Any]:
        """快速探查 PDF 页数、提取字符数与是否为扫描件。"""
        try:
            reader = PdfReader(io.BytesIO(file_bytes))
        except Exception as exc:
            return {
                "readable": False,
                "page_count": 0,
                "extracted_char_count": 0,
                "is_scanned": True,
                "needs_ocr": True,
                "error": type(exc).__name__,
            }

        page_count = len(reader.pages)
        total_chars = 0
        pages_with_text = 0
        for page in reader.pages:
            raw_text = (page.extract_text() or "").strip()
            compact_len = len(re.sub(r"\s+", "", raw_text))
            total_chars += compact_len
            if compact_len >= self.min_chars_per_page:
                pages_with_text += 1

        avg_chars = (total_chars / page_count) if page_count > 0 else 0.0
        is_scanned = (
            page_count == 0
            or total_chars < self.min_total_chars
            or avg_chars < self.min_chars_per_page
        )
        return {
            "readable": True,
            "page_count": page_count,
            "extracted_char_count": total_chars,
            "avg_chars_per_page": round(avg_chars, 2),
            "pages_with_text": pages_with_text,
            "is_scanned": is_scanned,
            "needs_ocr": is_scanned,
        }

    def parse_bytes(
        self,
        file_bytes: bytes,
        *,
        filename: str,
        mime_type: str = "",
        extra_options: dict[str, Any] | None = None,
    ) -> ParseArtifactBundle:
        try:
            reader = PdfReader(io.BytesIO(file_bytes))
        except Exception as exc:
            raise ValidationError(f"无法读取 PDF 文件（{filename}）：文件损坏或加密") from exc

        blocks: list[ParsedBlockData] = []
        page_count = len(reader.pages)
        total_chars = 0
        page_summaries: list[dict[str, Any]] = []

        for page_num, page in enumerate(reader.pages, start=1):
            raw_text = (page.extract_text() or "").replace("\r\n", "\n").strip()
            compact_len = len(re.sub(r"\s+", "", raw_text))
            total_chars += compact_len
            page_summaries.append({"page_number": page_num, "char_count": compact_len})
            if not raw_text:
                continue

            paragraphs = [p.strip() for p in re.split(r"\n\s*\n+", raw_text) if p.strip()]
            for para in paragraphs:
                blocks.append(
                    ParsedBlockData(
                        block_type=BlockType.PARAGRAPH,
                        page_number=page_num,
                        heading_path=[],
                        text=para,
                        markdown=para,
                        metadata={"filename": filename, "page_number": page_num},
                    )
                )

        avg_chars = (total_chars / page_count) if page_count > 0 else 0.0
        is_scanned = (
            page_count == 0
            or total_chars < self.min_total_chars
            or avg_chars < self.min_chars_per_page
        )
        markdown = "\n\n".join(b.markdown for b in blocks)
        return ParseArtifactBundle(
            blocks=blocks,
            markdown=markdown,
            raw_json={
                "filename": filename,
                "page_count": page_count,
                "extracted_char_count": total_chars,
                "is_scanned": is_scanned,
                "needs_ocr": is_scanned,
                "pages": page_summaries,
            },
            rendered_pages=[],
            parser_provider=self.parser_provider,
            parser_version=self.parser_version,
            sha256=self.compute_sha256(file_bytes),
            metadata={
                "page_count": page_count,
                "extracted_char_count": total_chars,
                "avg_chars_per_page": round(avg_chars, 2),
                "is_scanned": is_scanned,
                "needs_ocr": is_scanned,
            },
        )
