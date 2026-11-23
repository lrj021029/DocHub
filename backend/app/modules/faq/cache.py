"""FAQ Fast Path 与受控 Redis 缓存基础设施。

安全与正确性原则（TECHNICAL_SPEC §10.8 & §10.9）：
1. Cache Key 规范：
   `dochub:faq:{org_id}:{space_id}:{faq_id}:v{ver}:acl{acl_rev}:src{src_hash}:g{cache_gen}`
2. **高敏感隔离红线**：L2 / L3 敏感来源关联的 FAQ 答案正文 **严禁写入 Redis**，
   仅允许 L0 / L1 缓存结构化答案；L2 / L3 一律强制回源 PostgreSQL。
3. **鉴权不可绕过**：每次 `match_faq_fast_path` 命中候选后，必须：
   (1) 校验 `publication_state == 'PUBLISHED'` 且 `source_health_state == 'HEALTHY'`；
   (2) 调用 `authorize_faq_for_principal` 完成多来源最严交集 ACL 复核；
   (3) 权限服务异常时 Fail-Closed；Redis 不可用时优雅回源 PostgreSQL。
4. 缓存失效由 PostgreSQL Outbox 事件驱动主动删除，TTL（默认 300s）仅作兜底，同时提供对账清理。
"""

from __future__ import annotations

import contextlib
import json
import logging
import uuid
from datetime import UTC, datetime
from typing import Any

import numpy as np
import redis.asyncio as aioredis
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.db.session import org_rls_session
from app.modules.faq.mining import QuestionRedactor, _tokenize_chinese
from app.modules.faq.models import (
    FAQ,
    FAQACL,
    FAQCacheIndex,
    FAQSegment,
    FAQSourceRef,
    FAQVersion,
)
from app.modules.faq.schemas import FaqFastPathMatchResponse
from app.modules.faq.service import authorize_faq_for_principal
from app.modules.identity.deps import OrgContext, Principal

logger = logging.getLogger(__name__)

DEFAULT_FAQ_CACHE_TTL_SECONDS = 300
_CACHEABLE_SENSITIVITY_LEVELS = frozenset({"L0", "L1"})


def build_faq_cache_key(
    *,
    organization_id: uuid.UUID,
    space_id: uuid.UUID,
    faq_id: uuid.UUID,
    version_no: int,
    acl_revision: int,
    source_version_hash: str,
    cache_generation: int,
) -> str:
    """按规格构造唯一带版本代次的 Redis Cache Key。"""
    short_hash = source_version_hash[:16]
    return (
        f"dochub:faq:{organization_id}:{space_id}:{faq_id}:"
        f"v{version_no}:acl{acl_revision}:src{short_hash}:g{cache_generation}"
    )


def _create_redis_client(settings: Settings | None = None) -> aioredis.Redis:
    """创建短期异步 Redis 客户端（按调用生命周期安全关闭，避免跨事件循环绑定）。"""
    cfg = settings or get_settings()
    return aioredis.from_url(
        cfg.redis_cache_url.get_secret_value(),
        decode_responses=True,
        socket_connect_timeout=2.0,
        socket_timeout=2.0,
    )


async def invalidate_faq_cache(
    *,
    organization_id: uuid.UUID,
    space_id: uuid.UUID,
    faq_id: uuid.UUID,
    settings: Settings | None = None,
) -> int:
    """按前缀扫描并删除指定 FAQ 在 Redis 中的全部缓存代次键。"""
    pattern = f"dochub:faq:{organization_id}:{space_id}:{faq_id}:*"
    client = _create_redis_client(settings)
    deleted = 0
    try:
        keys = [k async for k in client.scan_iter(match=pattern, count=100)]
        if keys:
            deleted = int(await client.delete(*keys))
    except Exception:
        logger.warning("Redis 失效清理失败，已依赖代次递增与 TTL 兜底: pattern=%s", pattern)
    finally:
        with contextlib.suppress(Exception):
            await client.aclose()
    return deleted


async def reconcile_faq_cache_for_org(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    settings: Settings | None = None,
) -> int:
    """周期对账：清理已非 PUBLISHED、已 STALE 或非当前 cache_generation 的孤儿缓存键。"""
    async with org_rls_session(session, organization_id):
        active_rows = (
            await session.execute(
                select(
                    FAQ.id,
                    FAQ.space_id,
                    FAQVersion.version_no,
                    FAQACL.revision,
                    FAQCacheIndex.source_version_hash,
                    FAQCacheIndex.cache_generation,
                )
                .join(FAQVersion, FAQVersion.id == FAQ.current_version_id)
                .join(FAQACL, FAQACL.faq_id == FAQ.id)
                .join(FAQCacheIndex, FAQCacheIndex.faq_id == FAQ.id)
                .where(
                    FAQ.organization_id == organization_id,
                    FAQ.publication_state == "PUBLISHED",
                    FAQ.source_health_state == "HEALTHY",
                )
            )
        ).all()

    valid_keys = {
        build_faq_cache_key(
            organization_id=organization_id,
            space_id=row[1],
            faq_id=row[0],
            version_no=row[2],
            acl_revision=row[3],
            source_version_hash=row[4],
            cache_generation=row[5],
        )
        for row in active_rows
    }

    pattern = f"dochub:faq:{organization_id}:*"
    client = _create_redis_client(settings)
    removed = 0
    try:
        stale_keys = [
            key async for key in client.scan_iter(match=pattern, count=200) if key not in valid_keys
        ]
        if stale_keys:
            removed = int(await client.delete(*stale_keys))
    except Exception:
        logger.warning("Redis 周期对账失败: org_id=%s", organization_id)
    finally:
        with contextlib.suppress(Exception):
            await client.aclose()
    return removed


async def _read_cache_payload(
    cache_key: str,
    *,
    settings: Settings | None = None,
) -> dict[str, Any] | None:
    """从 Redis 读取结构化 FAQ 缓存（Redis 不可用时返回 None 安全回源 PG）。"""
    client = _create_redis_client(settings)
    try:
        raw = await client.get(cache_key)
        if not raw:
            return None
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else None
    except Exception:
        logger.warning("Redis 读取失败，自动回源 PostgreSQL: key=%s", cache_key)
        return None
    finally:
        with contextlib.suppress(Exception):
            await client.aclose()


async def _write_cache_payload(
    cache_key: str,
    payload: dict[str, Any],
    *,
    ttl_seconds: int = DEFAULT_FAQ_CACHE_TTL_SECONDS,
    settings: Settings | None = None,
) -> None:
    """将结构化 L0/L1 FAQ 答案写入 Redis（L2/L3 严禁调用本函数）。"""
    client = _create_redis_client(settings)
    try:
        await client.set(
            cache_key,
            json.dumps(payload, ensure_ascii=False),
            ex=ttl_seconds,
        )
    except Exception:
        logger.warning("Redis 写入失败，不影响主流程回源响应: key=%s", cache_key)
    finally:
        with contextlib.suppress(Exception):
            await client.aclose()


def _score_faq_candidates(
    normalized_question: str,
    candidates: list[tuple[FAQ, FAQVersion]],
) -> list[tuple[float, FAQ, FAQVersion]]:
    """计算输入问题与候选 FAQ 的精确/分词余弦相似度得分。"""
    if not candidates:
        return []

    query_lower = normalized_question.strip().lower()
    query_tok = _tokenize_chinese(normalized_question)
    docs_tok = [
        _tokenize_chinese(QuestionRedactor.normalize(ver.question)) for _, ver in candidates
    ]

    tfidf_sims: list[float] = [0.0] * len(candidates)
    if query_tok and any(docs_tok):
        try:
            vec = TfidfVectorizer(token_pattern=r"(?u)\b\w+\b")
            matrix = vec.fit_transform([*docs_tok, query_tok])
            raw_sims = cosine_similarity(matrix[-1:], matrix[:-1])[0]
            tfidf_sims = [float(s) for s in raw_sims]
        except ValueError:
            pass

    scored: list[tuple[float, FAQ, FAQVersion]] = []
    for idx, (faq, ver) in enumerate(candidates):
        faq_q_norm = QuestionRedactor.normalize(ver.question).lower()
        if faq_q_norm == query_lower:
            score = 1.0
        elif query_lower in faq_q_norm or faq_q_norm in query_lower:
            score = max(0.82, float(np.clip(tfidf_sims[idx], 0.0, 1.0)))
        else:
            score = float(np.clip(tfidf_sims[idx], 0.0, 1.0))
        scored.append((score, faq, ver))

    scored.sort(key=lambda item: item[0], reverse=True)
    return scored


async def match_faq_fast_path(
    session: AsyncSession,
    *,
    principal: Principal | OrgContext,
    organization_id: uuid.UUID,
    space_ids: list[uuid.UUID],
    question: str,
    min_score: float = 0.55,
    settings: Settings | None = None,
) -> FaqFastPathMatchResponse:
    """执行 FAQ Fast Path：候选召回、状态门禁、交集 ACL、L0/L1 缓存与兜底回源。"""
    if not space_ids:
        return FaqFastPathMatchResponse(hit=False, reason="NO_SPACES_SELECTED")

    now = datetime.now(UTC)
    normalized_q = QuestionRedactor.normalize(question)
    org_ctx = principal if isinstance(principal, OrgContext) else None
    user_id = org_ctx.principal.user_id if org_ctx else principal.user_id

    async with org_rls_session(session, organization_id, user_id=user_id):
        # 1. 严格限定仅召回 PUBLISHED 且 HEALTHY 且未过期的 FAQ（STALE / DISABLED 绝不直接命中）
        rows = (
            await session.execute(
                select(FAQ, FAQVersion)
                .join(FAQVersion, FAQVersion.id == FAQ.current_version_id)
                .where(
                    FAQ.organization_id == organization_id,
                    FAQ.space_id.in_(space_ids),
                    FAQ.publication_state == "PUBLISHED",
                    FAQ.source_health_state == "HEALTHY",
                    or_(FAQ.expires_at.is_(None), FAQ.expires_at > now),
                )
            )
        ).all()

    if not rows:
        return FaqFastPathMatchResponse(hit=False, reason="NO_HEALTHY_PUBLISHED_FAQ")

    ranked = _score_faq_candidates(normalized_q, [(r[0], r[1]) for r in rows])
    qualified = [item for item in ranked if item[0] >= min_score]
    if not qualified:
        return FaqFastPathMatchResponse(hit=False, reason="BELOW_CONFIDENCE_THRESHOLD")

    for score, faq, ver in qualified:
        # 2. 权威多来源最严交集 ACL 复核（权限服务异常直接 Fail-Closed 抛出）
        auth_dec = await authorize_faq_for_principal(
            session,
            principal=principal,
            organization_id=organization_id,
            faq_id=faq.id,
        )
        if not auth_dec.allowed:
            continue

        # 3. 构造版本化 Cache Key
        async with org_rls_session(session, organization_id, user_id=user_id):
            cache_idx = (
                await session.execute(
                    select(FAQCacheIndex).where(
                        FAQCacheIndex.organization_id == organization_id,
                        FAQCacheIndex.faq_id == faq.id,
                    )
                )
            ).scalar_one_or_none()
            cache_gen = cache_idx.cache_generation if cache_idx else 1
            src_hash = cache_idx.source_version_hash if cache_idx else ver.answer_hash

            cache_key = build_faq_cache_key(
                organization_id=organization_id,
                space_id=faq.space_id,
                faq_id=faq.id,
                version_no=ver.version_no,
                acl_revision=auth_dec.acl_revision,
                source_version_hash=src_hash,
                cache_generation=cache_gen,
            )

            can_cache_body = auth_dec.max_sensitivity_level in _CACHEABLE_SENSITIVITY_LEVELS
            from_cache = False
            answer_text: str | None = None
            source_citations: list[dict[str, Any]] = []

            if can_cache_body:
                cached = await _read_cache_payload(cache_key, settings=settings)
                if cached is not None:
                    answer_text = str(cached.get("answer") or "")
                    source_citations = list(cached.get("source_citations") or [])
                    from_cache = True

            if answer_text is None:
                segs = (
                    (
                        await session.execute(
                            select(FAQSegment)
                            .where(
                                FAQSegment.organization_id == organization_id,
                                FAQSegment.faq_version_id == ver.id,
                            )
                            .order_by(FAQSegment.segment_no.asc())
                        )
                    )
                    .scalars()
                    .all()
                )
                answer_text = "\n\n".join(s.content_text for s in segs)

                refs = (
                    (
                        await session.execute(
                            select(FAQSourceRef)
                            .join(FAQSegment, FAQSegment.id == FAQSourceRef.faq_segment_id)
                            .where(
                                FAQSourceRef.organization_id == organization_id,
                                FAQSegment.faq_version_id == ver.id,
                            )
                        )
                    )
                    .scalars()
                    .all()
                )
                source_citations = [
                    {
                        "document_id": str(r.document_id),
                        "document_version_id": str(r.document_version_id),
                        "chunk_id": str(r.chunk_id) if r.chunk_id else None,
                        "citation_ref": r.citation_ref,
                    }
                    for r in refs
                ]

                # 仅 L0 / L1 允许写入 Redis；L2 / L3 绝对禁止进入缓存
                if can_cache_body:
                    await _write_cache_payload(
                        cache_key,
                        {
                            "faq_id": str(faq.id),
                            "faq_version_id": str(ver.id),
                            "question": ver.question,
                            "answer": answer_text,
                            "source_citations": source_citations,
                        },
                        settings=settings,
                    )

            # 4. 命中后递增 PostgreSQL 真实命中计数
            faq_row = await session.get(FAQ, faq.id)
            if faq_row is not None:
                faq_row.hit_count += 1

        return FaqFastPathMatchResponse(
            hit=True,
            reason="FAQ_FAST_PATH_HIT",
            faq_id=faq.id,
            faq_version_id=ver.id,
            question=ver.question,
            answer=answer_text,
            score=round(score, 4),
            from_cache=from_cache,
            acl_revision=auth_dec.acl_revision,
            source_citations=source_citations,
        )

    return FaqFastPathMatchResponse(hit=False, reason="ACL_OR_ELIGIBILITY_DENIED")
