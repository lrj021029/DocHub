"""DocHub 全场景真实演示数据初始化脚本（`python -m app.db.seed_demo`）。

用途：
在已完成基础组织与账号初始化（`python -m app.db.seed`）的基础上，通过真实生产业务链路向
PostgreSQL 17（`app_runtime` + FORCE RLS）、MinIO、Qdrant 与 Redis 7 写入可覆盖全部
14 个前端工作台页面的完整演示数据，支持立即体验并全量测试：

1. **场景 A（跨部门财务与薪酬制度权限隔离）**：
   - 《2026年差旅与财务报销标准》（`finance-general`，L0，`GLOBAL`，ACTIVE + 已索引）
   - 《高管薪酬与股权激励细则（机密）》（`hr-executive` 私有，L2，HR+财务经理，ACTIVE）
   - 《2026年新员工入职指引（待授权）》（`finance-general`，L1，`QUARANTINED` 隔离示例）
   - 《核心芯片研发架构扫描件.pdf》（L3 扫描 PDF，真实触发 `BLOCKED_POLICY` 与安全审计）
   - 预置 2 条演示历史会话（员工差旅问答 + HR 高管薪酬带引用问答，可直接体验引用重鉴权与撤权脱敏）
2. **场景 B（客服 FAQ 沉淀与 Knowledge Gap 闭环）**：
   - 《生鲜食品售后与破损退款SOP》（`customer-support`，L0，`GLOBAL`，ACTIVE + 已索引）
   - 多名用户脱敏提问（含 PII 手机号自动脱敏）→ HDBSCAN 重聚类生成待审核 `FAQCandidate`
   - 1 条已发布 `HEALTHY` FAQ（写入真实 Redis 缓存，可直接体验 Fast Path 命中）
   - 1 条已发布但来源已更新的 `STALE` FAQ（直观验证三轴状态解耦：`PUBLISHED + STALE`）
   - 2 条 `KnowledgeGap`（“海外直邮保税仓清关延误处理流程”等，含已分配 `KnowledgeGapTask`）
3. **运营分析、双审计中心与 Benchmark**：
   - 写入真实 `BenchmarkRun`（`ACL Violation = 0`）与 `SecurityAuditEvent` / `AuditEvent` 样本。
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import math
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import jieba
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.ids import new_uuid7
from app.core.logging import configure_logging
from app.db.seed import seed_database
from app.db.session import AsyncSessionLocal, dispose_runtime_engine, org_rls_session
from app.modules.acl.engine import AuthoritativeACLEngine
from app.modules.acl.schemas import ACLGrantInput, ACLRevisionCreateRequest
from app.modules.acl.service import create_document_acl_revision
from app.modules.analytics.schemas import BenchmarkTriggerRequest
from app.modules.analytics.service import trigger_benchmark_run
from app.modules.audit.service import record_audit_event, record_security_audit_event
from app.modules.chat.models import (
    Conversation,
    ConversationSpace,
    LLMUsage,
    Message,
    MessageSegment,
    RetrievalAudit,
    RetrievalCandidate,
    RetrievalDecision,
)
from app.modules.citations.models import AnswerCitation
from app.modules.departments.models import Department
from app.modules.documents.models import Chunk, Document, DocumentVersion
from app.modules.documents.service import publish_document_version, upload_document_direct
from app.modules.documents.storage import MinIOStorageService
from app.modules.embeddings.providers import (
    EmbeddingBatchResult,
    EmbeddingRouter,
)
from app.modules.embeddings.providers import (
    EmbeddingProfile as RuntimeEmbeddingProfile,
)
from app.modules.faq.cache import match_faq_fast_path
from app.modules.faq.mining import QuestionRedactor, run_hdbscan_recluster
from app.modules.faq.models import FAQ
from app.modules.faq.schemas import (
    FaqReviewRequest,
    FaqSourceRefInput,
    ManualFaqCreateRequest,
)
from app.modules.faq.service import (
    create_manual_faq,
    mark_faqs_stale_for_document,
    publish_faq,
    submit_review_decision,
)
from app.modules.identity.deps import OrgContext, Principal
from app.modules.identity.models import User
from app.modules.indexing.pipeline import DocumentIngestionPipeline
from app.modules.indexing.qdrant_store import QdrantIndexStore
from app.modules.knowledge_gaps.models import KnowledgeGap, QuestionEvent
from app.modules.knowledge_gaps.schemas import GapSignalInput, KnowledgeGapTaskCreateRequest
from app.modules.knowledge_gaps.service import create_knowledge_gap_task, ingest_gap_signal
from app.modules.knowledge_spaces.models import KnowledgeSpace
from app.modules.organizations.models import Organization
from app.modules.providers.egress import (
    EgressDecision,
    EgressPurpose,
    ProviderScope,
    SensitivityLevel,
    normalize_sensitivity,
)
from app.modules.roles.models import OrganizationRole, OrganizationRolePermission, Permission

logger = logging.getLogger(__name__)


# ==============================================================================
# 演示文档真实文本内容（Markdown / 扫描 PDF 最小合规头）
# ==============================================================================

TRAVEL_POLICY_MD = """# 2026年差旅与财务报销标准

## 一、差旅住宿与交通标准
1. 一线城市（北京、上海、广州、深圳）普通员工住宿标准为 **600 元/晚**，二线城市为 **450 元/晚**。
2. 城际高铁优先预订二等座；单程超过 5 小时或机票价格低于高铁时，可经直属主管审批后预订经济舱机票。
3. 市内交通统一使用企业滴滴或保存正规机打发票，实报实销，每日封顶 150 元。

## 二、差旅餐补与报销时限
| 职级区间 | 每日餐补标准（元） | 审批节点 | 报销截止时限 |
| --- | --- | --- | --- |
| P4 - P6 普通员工 | 120 | 直属主管 + 财务共享中心 | 返程后 15 个工作日内 |
| P7 - P8 资深专家 | 150 | 部门负责人 + 财务共享中心 | 返程后 15 个工作日内 |
| M2 及以上管理层 | 200 | 财务总监复核 | 返程后 30 个自然日内 |

## 三、电子发票与合规要求
所有报销单必须附带数电发票 PDF 或增值税专用发票原件，
抬头须为公司全称并包含纳税人识别号，禁止使用定额手撕票冲抵住宿费。
"""

EXEC_COMP_MD = """# 高管薪酬与股权激励细则（机密 L2）

## 一、高管年度总薪酬结构
1. VP 及以上管理层采用“固定年薪（50%）+ 短期绩效奖金（25%）+ 长期限制性股票 RSU（25%）”组合。
2. 核心高管基薪区间为 180 万元至 360 万元/年，绩效系数与公司年度 ARR 增长率及经营现金流强绑定。

## 二、期权与 RSU 归属周期（4 年 Vesting）
- 满 12 个月一次性归属 25%（1-Year Cliff），此后每满一个季度归属 6.25%。
- 本细则仅限 **人力资源部（HR）** 与 **财务经理** 查阅，严禁向普通员工泄露。
"""

ONBOARDING_QUARANTINE_MD = """# 2026年新员工入职指引（待授权隔离示例）

## 一、首日流程
1. 上午 9:30 前往 A 栋 3 层前台领取工牌与办公笔记本电脑。
2. 完成企业邮箱激活、SSO 双因素绑定及信息安全合规考试。
3. 本文档故意保持 `QUARANTINED` 状态，用于演示新导入文档未完成 ACL 授权与发布前不可被 AI 检索。
"""

FRESH_REFUND_SOP_MD = """# 生鲜食品售后与破损退款 SOP

## 一、破损赔付标准（24 小时极速处理）
1. 客户签收生鲜商品后 **24 小时内** 反馈腐烂、变质、化水或包装破损，
   并提供清晰开箱照片与快递面单的，直接触发极速退款通道。
2. 破损比例低于 30% 时按比例退款；达到或超过 30% 时支持 **全额退款且无需退货**。
3. 退款款项于审核通过后 2 小时内原路退回至客户支付账户（微信/支付宝/银行卡）。

## 二、异常订单升级路径
单笔退款金额超过 500 元或同一用户 7 天内高频申请超过 3 次的订单，自动转交客服主管人工复核。
"""

# 构造合法但无文本层的极简扫描 PDF（L3 场景真实触发 BLOCKED_POLICY）
MINIMAL_SCANNED_PDF = (
    b"%PDF-1.4\n"
    b"1 0 obj << /Type /Catalog /Pages 2 0 R >> endobj\n"
    b"2 0 obj << /Type /Pages /Kids [3 0 R] /Count 1 >> endobj\n"
    b"3 0 obj << /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] >> endobj\n"
    b"xref\n0 4\n0000000000 65535 f \n0000000010 00000 n \n"
    b"0000000060 00000 n \n0000000117 00000 n \n"
    b"trailer << /Size 4 /Root 1 0 R >>\nstartxref\n187\n%%EOF\n"
)


class _DeterministicDemoEmbeddingRouter(EmbeddingRouter):
    """本地确定性 1024 维词袋哈希向量器（仅在未配置真实 Qwen/BGE Key 时供演示数据入库使用）。"""

    def __init__(self, dimensions: int = 1024) -> None:
        self._dimensions = dimensions

    def _encode_one(self, text: str) -> list[float]:
        vec = [0.0] * self._dimensions
        tokens = [t.strip().lower() for t in jieba.cut(text) if t.strip()]
        if not tokens:
            vec[0] = 1.0
            return vec
        for tok in tokens:
            digest = hashlib.blake2b(tok.encode("utf-8"), digest_size=8).digest()
            idx = int.from_bytes(digest[:4], "big") % self._dimensions
            sign = 1.0 if (digest[4] & 1) == 0 else -1.0
            vec[idx] += sign
        norm = math.sqrt(sum(v * v for v in vec)) or 1.0
        return [v / norm for v in vec]

    async def embed_documents(
        self,
        texts: Any,
        *,
        sensitivity_level: SensitivityLevel | str = SensitivityLevel.L0,
        org_allow_external_l1: bool = False,
        profile: RuntimeEmbeddingProfile | None = None,
        organization_id: str | None = None,
        resource_id: str | None = None,
    ) -> EmbeddingBatchResult:
        _ = org_allow_external_l1
        level = normalize_sensitivity(sensitivity_level)
        dim = (profile.dimensions if profile and profile.dimensions else None) or self._dimensions
        self._dimensions = dim
        text_list = list(texts)
        return EmbeddingBatchResult(
            vectors=[self._encode_one(t) for t in text_list],
            provider="local_bge",
            model="demo-deterministic-1024",
            dimensions=dim,
            usage_tokens=sum(len(t) for t in text_list),
            latency_ms=1.0,
            egress_decision=EgressDecision(
                allowed=True,
                sensitivity_level=level,
                org_allow_external_l1=org_allow_external_l1,
                provider_scope=ProviderScope.LOCAL,
                purpose=EgressPurpose.EMBEDDING,
                provider_name="local_bge",
                model_name="demo-deterministic-1024",
                reason_code="LOCAL_PROVIDER_ALLOWED",
                reason_message="本地确定性演示向量器（无外部数据出境）",
                organization_id=organization_id,
                resource_id=resource_id,
            ),
        )

    async def embed_query(
        self,
        query: str,
        *,
        sensitivity_level: SensitivityLevel | str = SensitivityLevel.L0,
        org_allow_external_l1: bool = False,
        profile: RuntimeEmbeddingProfile | None = None,
        organization_id: str | None = None,
        resource_id: str | None = None,
    ) -> EmbeddingBatchResult:
        return await self.embed_documents(
            [query],
            sensitivity_level=sensitivity_level,
            org_allow_external_l1=org_allow_external_l1,
            profile=profile,
            organization_id=organization_id,
            resource_id=resource_id,
        )


def _has_real_embedding_provider() -> bool:
    """检查当前环境是否已配置可用的真实 Qwen 或本地 BGE Embedding 服务（不打印密钥）。"""
    settings = get_settings()
    qwen_raw = settings.qwen_api_key.get_secret_value().strip()
    has_qwen = bool(
        qwen_raw
        and not qwen_raw.startswith("YOUR_")
        and not qwen_raw.startswith("CHANGE_ME")
    )
    return has_qwen or settings.local_bge_embedding_enabled


async def _resolve_demo_embedding_router() -> tuple[EmbeddingRouter, bool]:
    """探测真实 Embedding Provider 是否可用；不可用时退回本地确定性向量器。"""
    if not _has_real_embedding_provider():
        logger.warning(
            "未检测到真实 QWEN_API_KEY 或本地 BGE，使用本地确定性 1024 维向量 + 真实 jieba 稀疏向量"
            "写入 Qdrant；配置有效 Key 后可在文档中心重新索引。"
        )
        return _DeterministicDemoEmbeddingRouter(dimensions=1024), False

    real_router = EmbeddingRouter()
    try:
        await real_router.embed_query(
            "健康探测",
            sensitivity_level="L0",
            org_allow_external_l1=True,
        )
        logger.info("真实 Embedding Provider 探测成功，将使用真实模型向量化演示文档。")
        return real_router, True
    except Exception as exc:
        logger.warning(
            "真实 Embedding Provider 探测未通过（%s），演示初始化改用本地确定性 1024 维向量 + "
            "真实 jieba 稀疏向量写入 Qdrant；请核查 .env 中 QWEN_API_KEY 有效性。",
            type(exc).__name__,
        )
        return _DeterministicDemoEmbeddingRouter(dimensions=1024), False


async def _build_org_ctx(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    user: User,
) -> OrgContext:
    """从数据库实时构建指定用户的 OrgContext（严格遵循当前组织角色权限）。"""
    async with org_rls_session(session, organization_id, user_id=user.id):
        from app.modules.departments.models import DepartmentMembership
        from app.modules.roles.models import UserRoleMembership

        role_rows = (
            await session.execute(
                select(OrganizationRole)
                .join(
                    UserRoleMembership,
                    UserRoleMembership.organization_role_id == OrganizationRole.id,
                )
                .where(
                    UserRoleMembership.organization_id == organization_id,
                    UserRoleMembership.user_id == user.id,
                    UserRoleMembership.status == "ACTIVE",
                )
            )
        ).scalars().all()
        dept_ids = tuple(
            (
                await session.execute(
                    select(DepartmentMembership.department_id).where(
                        DepartmentMembership.organization_id == organization_id,
                        DepartmentMembership.user_id == user.id,
                        DepartmentMembership.status == "ACTIVE",
                    )
                )
            )
            .scalars()
            .all()
        )
        role_ids = [r.id for r in role_rows]
        role_codes = tuple(r.code for r in role_rows)
        perm_codes: set[str] = set()
        if role_ids:
            perm_codes = set(
                (
                    await session.execute(
                        select(Permission.code)
                        .join(
                            OrganizationRolePermission,
                            OrganizationRolePermission.permission_id == Permission.id,
                        )
                        .where(
                            OrganizationRolePermission.organization_id == organization_id,
                            OrganizationRolePermission.organization_role_id.in_(role_ids),
                        )
                    )
                )
                .scalars()
                .all()
            )
    principal = Principal(
        user_id=user.id,
        email=user.email,
        display_name=user.display_name,
        status=user.status,
        session_id=None,
        token_org_id=organization_id,
        system_roles=(),
        system_permissions=frozenset(),
    )
    return OrgContext(
        principal=principal,
        organization_id=organization_id,
        organization_name="Acme Corporation",
        organization_slug="acme-corp",
        organization_roles=role_codes,
        department_ids=dept_ids,
        permissions=frozenset(perm_codes),
    )


async def _ensure_doc_ingested(
    session: AsyncSession,
    *,
    km_ctx: OrgContext,
    space: KnowledgeSpace,
    title: str,
    filename: str,
    content_bytes: bytes,
    mime_type: str,
    sensitivity_level: str,
    grants: list[ACLGrantInput] | None,
    acl_reason: str,
    should_publish: bool,
    should_process: bool,
    storage: MinIOStorageService,
    pipeline: DocumentIngestionPipeline,
) -> tuple[Document, DocumentVersion]:
    """幂等上传、配置四维 ACL、执行解析/切片/Qdrant 建索引与发布。"""
    async with org_rls_session(session, km_ctx.organization_id, user_id=km_ctx.principal.user_id):
        existing_doc = (
            await session.execute(
                select(Document)
                .where(
                    Document.organization_id == km_ctx.organization_id,
                    Document.space_id == space.id,
                    Document.title == title,
                )
                .limit(1)
            )
        ).scalar_one_or_none()
        if existing_doc is not None:
            existing_ver = (
                await session.execute(
                    select(DocumentVersion)
                    .where(
                        DocumentVersion.document_id == existing_doc.id,
                        DocumentVersion.organization_id == km_ctx.organization_id,
                    )
                    .order_by(DocumentVersion.version_no.desc())
                    .limit(1)
                )
            ).scalar_one()
            logger.info("跳过已存在的演示文档: %s (%s)", title, existing_doc.status)
            return existing_doc, existing_ver

    upload_resp = await upload_document_direct(
        org_ctx=km_ctx,
        space_id=space.id,
        raw_filename=filename,
        mime_type=mime_type,
        file_bytes=content_bytes,
        title=title,
        sensitivity_level=sensitivity_level,
        document_id=None,
        idempotency_key=f"demo-upload-{hashlib.sha256(title.encode('utf-8')).hexdigest()[:16]}",
        session=session,
        storage=storage,
    )

    if grants:
        await create_document_acl_revision(
            org_ctx=km_ctx,
            document_id=upload_resp.document_id,
            payload=ACLRevisionCreateRequest(
                expected_revision=0,
                reason=acl_reason,
                grants=grants,
            ),
            idempotency_key=f"demo-acl-{upload_resp.document_id}",
            session=session,
        )

    if should_publish:
        await publish_document_version(
            org_ctx=km_ctx,
            version_id=upload_resp.document_version_id,
            idempotency_key=f"demo-pub-{upload_resp.document_version_id}",
            session=session,
        )

    if should_process:
        result = await pipeline.process_document_version(
            session,
            organization_id=km_ctx.organization_id,
            document_version_id=upload_resp.document_version_id,
            actor_user_id=km_ctx.principal.user_id,
        )
        logger.info(
            "演示文档流水线完成: title=%s result=%s",
            title,
            result.model_dump(mode="json"),
        )

    async with org_rls_session(session, km_ctx.organization_id, user_id=km_ctx.principal.user_id):
        doc = (
            await session.execute(
                select(Document).where(
                    Document.id == upload_resp.document_id,
                    Document.organization_id == km_ctx.organization_id,
                )
            )
        ).scalar_one()
        ver = (
            await session.execute(
                select(DocumentVersion).where(
                    DocumentVersion.id == upload_resp.document_version_id,
                    DocumentVersion.organization_id == km_ctx.organization_id,
                )
            )
        ).scalar_one()
    return doc, ver


async def seed_demo_data() -> dict[str, Any]:
    """幂等写入全 14 页可交互演示数据（场景 A + 场景 B + 审计 + Benchmark）。"""
    # 先确保基础组织、部门、角色、Space 与账号就绪
    async with AsyncSessionLocal() as init_session:
        await seed_database(init_session)

    storage = MinIOStorageService()
    qdrant_store = QdrantIndexStore()
    emb_router, use_real_emb = await _resolve_demo_embedding_router()

    pipeline = DocumentIngestionPipeline(
        storage=storage,
        embedding_router=emb_router,
        qdrant_store=qdrant_store,
    )

    async with AsyncSessionLocal() as session:
        # 1. 读取 acme-corp 及角色/部门/账号/Space
        async with session.begin():
            acme_org = (
                await session.execute(select(Organization).where(Organization.slug == "acme-corp"))
            ).scalar_one()
            users = {
                u.email: u
                for u in (
                    await session.execute(
                        select(User).where(
                            User.email.in_(
                                [
                                    "admin@acme.local",
                                    "km@acme.local",
                                    "hr@acme.local",
                                    "employee@acme.local",
                                    "auditor@acme.local",
                                ]
                            )
                        )
                    )
                )
                .scalars()
                .all()
            }

        async with org_rls_session(session, acme_org.id):
            spaces = {
                s.slug: s
                for s in (
                    await session.execute(
                        select(KnowledgeSpace).where(
                            KnowledgeSpace.organization_id == acme_org.id
                        )
                    )
                )
                .scalars()
                .all()
            }
            depts = {
                d.name: d
                for d in (
                    await session.execute(
                        select(Department).where(Department.organization_id == acme_org.id)
                    )
                )
                .scalars()
                .all()
            }
            roles = {
                r.code: r
                for r in (
                    await session.execute(
                        select(OrganizationRole).where(
                            OrganizationRole.organization_id == acme_org.id
                        )
                    )
                )
                .scalars()
                .all()
            }

        km_ctx = await _build_org_ctx(
            session, organization_id=acme_org.id, user=users["km@acme.local"]
        )
        hr_ctx = await _build_org_ctx(
            session, organization_id=acme_org.id, user=users["hr@acme.local"]
        )
        emp_ctx = await _build_org_ctx(
            session, organization_id=acme_org.id, user=users["employee@acme.local"]
        )

        # 2. 场景 A 文档：差旅报销标准（L0, GLOBAL, ACTIVE）
        travel_doc, travel_ver = await _ensure_doc_ingested(
            session,
            km_ctx=km_ctx,
            space=spaces["finance-general"],
            title="2026年差旅与财务报销标准",
            filename="travel_reimbursement_2026.md",
            content_bytes=TRAVEL_POLICY_MD.encode("utf-8"),
            mime_type="text/markdown",
            sensitivity_level="L0",
            grants=[ACLGrantInput(subject_type="GLOBAL")],
            acl_reason="初始化演示：差旅报销制度面向当前财务公开空间全体成员可读",
            should_publish=True,
            should_process=True,
            storage=storage,
            pipeline=pipeline,
        )

        # 3. 场景 A 文档：高管薪酬细则（L2, 私有空间, 仅 HR 部门 + 财务经理角色, ACTIVE）
        # 当本地 BGE 已启用时，L2 机密文档直接经真实 LocalBGEEmbeddingProvider 向量化，零数据出境
        exec_pipeline = (
            pipeline
            if (not use_real_emb or get_settings().local_bge_embedding_enabled)
            else DocumentIngestionPipeline(
                storage=storage,
                embedding_router=_DeterministicDemoEmbeddingRouter(dimensions=1024),
                qdrant_store=qdrant_store,
            )
        )
        exec_doc, exec_ver = await _ensure_doc_ingested(
            session,
            km_ctx=km_ctx,
            space=spaces["hr-executive"],
            title="高管薪酬与股权激励细则（机密）",
            filename="executive_compensation_2026.md",
            content_bytes=EXEC_COMP_MD.encode("utf-8"),
            mime_type="text/markdown",
            sensitivity_level="L2",
            grants=[
                ACLGrantInput(
                    subject_type="DEPARTMENT",
                    subject_id=depts["人力资源部"].id,
                    include_descendants=True,
                    inheritance_mode="DYNAMIC",
                ),
                ACLGrantInput(
                    subject_type="ROLE",
                    subject_id=roles["FINANCE_MANAGER"].id,
                ),
            ],
            acl_reason="初始化演示：高管薪酬机密仅授权人力资源部及财务经理角色",
            should_publish=True,
            should_process=True,
            storage=storage,
            pipeline=exec_pipeline,
        )

        # 4. Quarantine 隔离演示文档（未授权、未发布）
        await _ensure_doc_ingested(
            session,
            km_ctx=km_ctx,
            space=spaces["finance-general"],
            title="2026年新员工入职指引（待授权）",
            filename="onboarding_guide_2026.md",
            content_bytes=ONBOARDING_QUARANTINE_MD.encode("utf-8"),
            mime_type="text/markdown",
            sensitivity_level="L1",
            grants=None,
            acl_reason="",
            should_publish=False,
            should_process=False,
            storage=storage,
            pipeline=pipeline,
        )

        # 5. L3 扫描 PDF 外发阻断演示文档（自动触发 BLOCKED_POLICY 与 SecurityAuditEvent）
        await _ensure_doc_ingested(
            session,
            km_ctx=km_ctx,
            space=spaces["finance-general"],
            title="核心芯片研发架构扫描件（L3阻断示例）",
            filename="chip_arch_scanned_l3.pdf",
            content_bytes=MINIMAL_SCANNED_PDF,
            mime_type="application/pdf",
            sensitivity_level="L3",
            grants=[
                ACLGrantInput(
                    subject_type="DEPARTMENT",
                    subject_id=depts["研发部"].id,
                    include_descendants=True,
                    inheritance_mode="SNAPSHOT",
                )
            ],
            acl_reason="初始化演示：L3 研发核心架构扫描件，验证无本地 OCR 时 Fail-Closed 阻断外发",
            should_publish=False,
            should_process=True,
            storage=storage,
            pipeline=pipeline,
        )

        # 6. 场景 B 文档：生鲜食品售后破损退款 SOP（L0, GLOBAL, ACTIVE）
        fresh_doc, fresh_ver = await _ensure_doc_ingested(
            session,
            km_ctx=km_ctx,
            space=spaces["customer-support"],
            title="生鲜食品售后与破损退款 SOP",
            filename="fresh_food_refund_sop.md",
            content_bytes=FRESH_REFUND_SOP_MD.encode("utf-8"),
            mime_type="text/markdown",
            sensitivity_level="L0",
            grants=[ACLGrantInput(subject_type="GLOBAL")],
            acl_reason="初始化演示：客服售后 SOP 面向客服知识空间全体成员可读",
            should_publish=True,
            should_process=True,
            storage=storage,
            pipeline=pipeline,
        )

        # 7. 预置场景 A 演示历史会话与引用（员工差旅会话 + HR 高管薪酬会话）
        await _seed_demo_conversations(
            session=session,
            org_id=acme_org.id,
            emp_user=users["employee@acme.local"],
            hr_user=users["hr@acme.local"],
            finance_space=spaces["finance-general"],
            hr_space=spaces["hr-executive"],
            travel_doc=travel_doc,
            travel_ver=travel_ver,
            exec_doc=exec_doc,
            exec_ver=exec_ver,
        )

        # 8. 场景 B：脱敏提问、HDBSCAN FAQ 挖掘、已发布 HEALTHY/STALE FAQ 与 Knowledge Gap
        await _seed_demo_faq_and_gaps(
            session=session,
            org_id=acme_org.id,
            km_ctx=km_ctx,
            hr_ctx=hr_ctx,
            emp_ctx=emp_ctx,
            support_space=spaces["customer-support"],
            finance_space=spaces["finance-general"],
            fresh_doc=fresh_doc,
            fresh_ver=fresh_ver,
            travel_doc=travel_doc,
            travel_ver=travel_ver,
            support_dept_id=depts["客服部"].id,
        )

        # 9. 触发 1 次真实 Benchmark 运行与审计事件样本
        await trigger_benchmark_run(
            org_ctx=km_ctx,
            payload=BenchmarkTriggerRequest(suite_type="ACL"),
            idempotency_key=f"demo-bench-{acme_org.id}",
            session=session,
        )
        async with org_rls_session(session, acme_org.id, user_id=km_ctx.principal.user_id):
            record_audit_event(
                session,
                organization_id=acme_org.id,
                actor_user_id=km_ctx.principal.user_id,
                action="demo.dataset.initialized",
                resource_type="organization",
                resource_id=acme_org.id,
                details={"scenario_a": True, "scenario_b": True},
            )
            record_security_audit_event(
                session,
                organization_id=acme_org.id,
                actor_user_id=emp_ctx.principal.user_id,
                event_type="acl.document_denied",
                severity="MEDIUM",
                resource_type="document",
                resource_id=exec_doc.id,
                details={
                    "reason_code": "NO_ALLOW_GRANT_MATCH",
                    "note": "演示场景A普通员工越权拦截样本",
                },
            )

    summary = {
        "organization": "acme-corp",
        "documents_seeded": 5,
        "spaces": ["finance-general", "hr-executive", "customer-support"],
        "used_real_embedding_provider": use_real_emb,
    }
    logger.info("全场景演示数据初始化完成: %s", summary)
    return summary


async def _seed_demo_conversations(
    *,
    session: AsyncSession,
    org_id: uuid.UUID,
    emp_user: User,
    hr_user: User,
    finance_space: KnowledgeSpace,
    hr_space: KnowledgeSpace,
    travel_doc: Document,
    travel_ver: DocumentVersion,
    exec_doc: Document,
    exec_ver: DocumentVersion,
) -> None:
    """幂等写入场景 A 的 2 条演示历史会话（带段落来源映射与引用定位）。"""
    async with org_rls_session(session, org_id, user_id=emp_user.id):
        existing_emp = (
            await session.execute(
                select(Conversation).where(
                    Conversation.organization_id == org_id,
                    Conversation.user_id == emp_user.id,
                    Conversation.title == "【演示】2026年一线城市差旅住宿标准咨询",
                )
            )
        ).scalar_one_or_none()

        travel_chunk = (
            await session.execute(
                select(Chunk)
                .where(
                    Chunk.organization_id == org_id,
                    Chunk.document_version_id == travel_ver.id,
                )
                .limit(1)
            )
        ).scalar_one_or_none()

        if existing_emp is None and travel_chunk is not None:
            conv = Conversation(
                organization_id=org_id,
                user_id=emp_user.id,
                title="【演示】2026年一线城市差旅住宿标准咨询",
                status="ACTIVE",
            )
            session.add(conv)
            await session.flush()
            session.add(
                ConversationSpace(
                    organization_id=org_id,
                    conversation_id=conv.id,
                    space_id=finance_space.id,
                )
            )
            q_msg = Message(
                organization_id=org_id,
                conversation_id=conv.id,
                role="USER",
                content="北京出差普通员工的住宿标准和餐补是多少？需要多久内报销？",
                status="COMPLETED",
                is_splittable=True,
            )
            a_msg = Message(
                organization_id=org_id,
                conversation_id=conv.id,
                role="ASSISTANT",
                content=(
                    "根据《2026年差旅与财务报销标准》[1]：\n"
                    "1. 一线城市（含北京）普通员工住宿标准为 **600 元/晚**。\n"
                    "2. P4–P6 普通员工每日餐补 **120 元/天**，须在返程后 **15 个工作日内** 报销。"
                ),
                status="COMPLETED",
                answer_policy_used="STRICT_EVIDENCE",
                is_splittable=True,
            )
            session.add_all([q_msg, a_msg])
            await session.flush()

            seg = MessageSegment(
                organization_id=org_id,
                message_id=a_msg.id,
                sequence=1,
                content=a_msg.content,
                segment_type="TEXT",
                source_document_ids=[str(travel_doc.id)],
                sensitivity_level="L0",
            )
            session.add(seg)
            await session.flush()
            session.add(
                AnswerCitation(
                    organization_id=org_id,
                    message_segment_id=seg.id,
                    message_id=a_msg.id,
                    document_id=travel_doc.id,
                    document_version_id=travel_ver.id,
                    chunk_id=travel_chunk.id,
                    page_number=1,
                    bbox_json={"x1": 64, "y1": 120, "x2": 540, "y2": 210},
                    quote_hash=hashlib.sha256(travel_chunk.content_text.encode("utf-8")).hexdigest()[:32],
                    acl_revision=1,
                )
            )
            audit = RetrievalAudit(
                organization_id=org_id,
                message_id=a_msg.id,
                query_hash=hashlib.sha256(q_msg.content.encode("utf-8")).hexdigest(),
                retrieval_config_version="hybrid-rrf-v1",
                policy_revision=1,
                latency_ms=142,
                authorized_count=1,
                denied_count=0,
            )
            session.add(audit)
            await session.flush()
            session.add(
                RetrievalCandidate(
                    organization_id=org_id,
                    audit_id=audit.id,
                    chunk_id=travel_chunk.id,
                    space_id=finance_space.id,
                    per_space_rank=1,
                    retrieval_source="HYBRID_RRF",
                    score_metadata={"rrf_score": 0.0328},
                )
            )
            session.add(
                RetrievalDecision(
                    organization_id=org_id,
                    audit_id=audit.id,
                    chunk_id=travel_chunk.id,
                    document_id=travel_doc.id,
                    decision="ALLOW",
                    reason_code="GLOBAL_GRANT_MATCH",
                    acl_revision=1,
                )
            )
            session.add(
                LLMUsage(
                    organization_id=org_id,
                    message_id=a_msg.id,
                    provider="openai_compatible",
                    model="demo-seed",
                    prompt_tokens=320,
                    completion_tokens=96,
                    latency_ms=860,
                    cost_metadata={"currency": "USD", "estimated": 0.0},
                )
            )

    # HR 机密会话（用于演示撤权后段落立即隐藏脱敏）
    async with org_rls_session(session, org_id, user_id=hr_user.id):
        existing_hr = (
            await session.execute(
                select(Conversation).where(
                    Conversation.organization_id == org_id,
                    Conversation.user_id == hr_user.id,
                    Conversation.title == "【演示】高管 RSU 股权归属周期查询（HR 机密）",
                )
            )
        ).scalar_one_or_none()
        exec_chunk = (
            await session.execute(
                select(Chunk)
                .where(
                    Chunk.organization_id == org_id,
                    Chunk.document_version_id == exec_ver.id,
                )
                .limit(1)
            )
        ).scalar_one_or_none()

        if existing_hr is None and exec_chunk is not None:
            conv = Conversation(
                organization_id=org_id,
                user_id=hr_user.id,
                title="【演示】高管 RSU 股权归属周期查询（HR 机密）",
                status="ACTIVE",
            )
            session.add(conv)
            await session.flush()
            session.add(
                ConversationSpace(
                    organization_id=org_id,
                    conversation_id=conv.id,
                    space_id=hr_space.id,
                )
            )
            q_msg = Message(
                organization_id=org_id,
                conversation_id=conv.id,
                role="USER",
                content="高管限制性股票 RSU 的归属周期和 Cliff 规则是什么？",
                status="COMPLETED",
                is_splittable=True,
            )
            a_msg = Message(
                organization_id=org_id,
                conversation_id=conv.id,
                role="ASSISTANT",
                content=(
                    "依据《高管薪酬与股权激励细则（机密）》[1]：高管 RSU 采用 4 年归属周期，"
                    "满 12 个月一次性归属 25%（1-Year Cliff），此后每满一个季度归属 6.25%。"
                ),
                status="COMPLETED",
                answer_policy_used="STRICT_EVIDENCE",
                is_splittable=True,
            )
            session.add_all([q_msg, a_msg])
            await session.flush()
            seg = MessageSegment(
                organization_id=org_id,
                message_id=a_msg.id,
                sequence=1,
                content=a_msg.content,
                segment_type="TEXT",
                source_document_ids=[str(exec_doc.id)],
                sensitivity_level="L2",
            )
            session.add(seg)
            await session.flush()
            session.add(
                AnswerCitation(
                    organization_id=org_id,
                    message_segment_id=seg.id,
                    message_id=a_msg.id,
                    document_id=exec_doc.id,
                    document_version_id=exec_ver.id,
                    chunk_id=exec_chunk.id,
                    page_number=1,
                    bbox_json={"x1": 64, "y1": 160, "x2": 540, "y2": 240},
                    quote_hash=hashlib.sha256(exec_chunk.content_text.encode("utf-8")).hexdigest()[:32],
                    acl_revision=1,
                )
            )


async def _seed_demo_faq_and_gaps(
    *,
    session: AsyncSession,
    org_id: uuid.UUID,
    km_ctx: OrgContext,
    hr_ctx: OrgContext,
    emp_ctx: OrgContext,
    support_space: KnowledgeSpace,
    finance_space: KnowledgeSpace,
    fresh_doc: Document,
    fresh_ver: DocumentVersion,
    travel_doc: Document,
    travel_ver: DocumentVersion,
    support_dept_id: uuid.UUID,
) -> None:
    """幂等写入场景 B 的 FAQ 挖掘、已发布 HEALTHY/STALE FAQ 与 Knowledge Gap 闭环数据。"""
    # 检查是否已存在演示 FAQ，避免重复生成
    async with org_rls_session(session, org_id, user_id=km_ctx.principal.user_id):
        existing_faq_count = len(
            (
                await session.execute(
                    select(FAQ.id).where(FAQ.organization_id == org_id)
                )
            )
            .scalars()
            .all()
        )
        fresh_chunk = (
            await session.execute(
                select(Chunk)
                .where(
                    Chunk.organization_id == org_id,
                    Chunk.document_version_id == fresh_ver.id,
                )
                .limit(1)
            )
        ).scalar_one_or_none()
        travel_chunk = (
            await session.execute(
                select(Chunk)
                .where(
                    Chunk.organization_id == org_id,
                    Chunk.document_version_id == travel_ver.id,
                )
                .limit(1)
            )
        ).scalar_one_or_none()

    if existing_faq_count == 0:
        # 1. 写入脱敏后的高频生鲜破损退款提问事件，运行真实 HDBSCAN 重聚类生成待审核候选
        redactor = QuestionRedactor()
        raw_questions = [
            "生鲜食品签收破损怎么申请退款？我的手机号13812345678",
            "生鲜食品收到有破损烂果怎么退款？订单联系13900001111",
            "买的生鲜食品运输破损变质如何申请全额退款？",
            "生鲜食品签收后24小时内破损退款流程是什么？",
        ]
        async with org_rls_session(session, org_id, user_id=km_ctx.principal.user_id):
            for idx, raw_q in enumerate(raw_questions):
                norm_q = redactor.normalize(raw_q)
                session.add(
                    QuestionEvent(
                        organization_id=org_id,
                        space_id=support_space.id,
                        user_hash=hashlib.sha256(f"demo-user-{idx}".encode()).hexdigest()[:24],
                        session_hash=hashlib.sha256(f"demo-sess-{idx}".encode()).hexdigest()[:24],
                        department_id=support_dept_id,
                        normalized_text=norm_q,
                        sensitivity_level="L0",
                        outcome="ANSWERED",
                    )
                )

        await run_hdbscan_recluster(
            session=session,
            organization_id=org_id,
            space_id=support_space.id,
            window_days=30,
            min_cluster_size=3,
        )

        # 2. 创建并由另一角色（HR）审核发布 1 条 HEALTHY FAQ（写入真实 Redis 缓存）
        faq_created = await create_manual_faq(
            org_ctx=km_ctx,
            payload=ManualFaqCreateRequest(
                question="生鲜食品破损退款规则是什么？",
                answer=(
                    "客户签收生鲜商品后 24 小时内提供开箱照片与快递面单："
                    "破损比例低于 30% 按受损比例退款；达到或超过 30% 支持全额退款且无需退货，"
                    "审核通过后 2 小时内原路退回。"
                ),
                fact_basis="依据《生鲜食品售后与破损退款 SOP》第一章第 1-3 条",
                maintainer="客服质量运营组",
                expires_at=datetime.now(UTC) + timedelta(days=180),
                space_id=support_space.id,
                source_refs=[
                    FaqSourceRefInput(
                        document_id=fresh_doc.id,
                        document_version_id=fresh_ver.id,
                        chunk_id=fresh_chunk.id if fresh_chunk else None,
                        citation_ref="生鲜食品售后与破损退款 SOP · 第一章",
                    )
                ],
            ),
            session=session,
        )
        # 严格遵循禁自审：km 创建 -> hr 审核 -> km 发布
        await submit_review_decision(
            org_ctx=hr_ctx,
            faq_id=faq_created.id,
            payload=FaqReviewRequest(decision="APPROVED", comments="来源引用核对无误，同意发布"),
            session=session,
        )
        await publish_faq(
            org_ctx=km_ctx,
            faq_id=faq_created.id,
            session=session,
        )
        # 预热一次 Fast Path 匹配并写入真实 Redis 缓存
        await match_faq_fast_path(
            session=session,
            principal=emp_ctx,
            organization_id=org_id,
            space_ids=[support_space.id],
            question="生鲜食品破损退款规则是什么？",
        )

        # 3. 创建 1 条已发布后触发 STALE 的 FAQ（直观展示三轴状态分离：PUBLISHED + STALE）
        stale_faq = await create_manual_faq(
            org_ctx=km_ctx,
            payload=ManualFaqCreateRequest(
                question="一线城市差旅住宿标准是多少？",
                answer="一线城市（北上广深）普通员工住宿标准为 600 元/晚，二线城市为 450 元/晚。",
                fact_basis="依据《2026年差旅与财务报销标准》第一章",
                maintainer="财务共享中心",
                expires_at=datetime.now(UTC) + timedelta(days=180),
                space_id=finance_space.id,
                source_refs=[
                    FaqSourceRefInput(
                        document_id=travel_doc.id,
                        document_version_id=travel_ver.id,
                        chunk_id=travel_chunk.id if travel_chunk else None,
                        citation_ref="2026年差旅与财务报销标准 · 第一章",
                    )
                ],
            ),
            session=session,
        )
        await submit_review_decision(
            org_ctx=hr_ctx,
            faq_id=stale_faq.id,
            payload=FaqReviewRequest(decision="APPROVED", comments="审核通过"),
            session=session,
        )
        await publish_faq(
            org_ctx=km_ctx,
            faq_id=stale_faq.id,
            session=session,
        )
        # 模拟源文档发生修订，将其健康度标记为 STALE（同时保留 publication_state=PUBLISHED）
        await mark_faqs_stale_for_document(
            session=session,
            organization_id=org_id,
            document_id=travel_doc.id,
            reason="演示：源文档修订触发 STALE 健康度预警",
        )

    # 4. 场景 B Knowledge Gap：海外直邮保税仓清关延误（幂等写入 + 创建待整改任务）
    async with org_rls_session(session, org_id, user_id=km_ctx.principal.user_id):
        existing_gaps = len(
            (
                await session.execute(
                    select(KnowledgeGap.id).where(KnowledgeGap.organization_id == org_id)
                )
            )
            .scalars()
            .all()
        )
    if existing_gaps == 0:
        async with org_rls_session(session, org_id, user_id=km_ctx.principal.user_id):
            for i in range(3):
                res = await ingest_gap_signal(
                    session=session,
                    organization_id=org_id,
                    signal=GapSignalInput(
                        space_id=support_space.id,
                        user_id=new_uuid7(),
                        session_id=f"demo-gap-sess-{i}",
                        department_id=support_dept_id,
                        question_text="海外直邮保税仓清关延误超过72小时怎么赔付处理？",
                        outcome="NO_RETRIEVAL",
                        sensitivity_level="L0",
                        priority="HIGH",
                    ),
                )
            # 再插入一条中优先级缺口
            await ingest_gap_signal(
                session=session,
                organization_id=org_id,
                signal=GapSignalInput(
                    space_id=finance_space.id,
                    user_id=emp_ctx.principal.user_id,
                    session_id="demo-gap-sess-travel",
                    department_id=support_dept_id,
                    question_text="海外出差外币信用卡还款汇率折算规则是什么？",
                    outcome="INSUFFICIENT_EVIDENCE",
                    sensitivity_level="L1",
                    priority="MEDIUM",
                ),
            )

            if res.gap_id is not None:
                await create_knowledge_gap_task(
                    session,
                    organization_id=org_id,
                    actor_user_id=km_ctx.principal.user_id,
                    gap_id=res.gap_id,
                    payload=KnowledgeGapTaskCreateRequest(
                        assignee_id=km_ctx.principal.user_id,
                        priority="HIGH",
                        due_at=datetime.now(UTC) + timedelta(days=7),
                    ),
                )

    _ = AuthoritativeACLEngine


def main() -> None:
    """命令行入口：`python -m app.db.seed_demo`。"""
    configure_logging()

    async def _run() -> None:
        try:
            await seed_demo_data()
        finally:
            await dispose_runtime_engine()

    asyncio.run(_run())


if __name__ == "__main__":
    main()
