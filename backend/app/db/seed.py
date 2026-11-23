"""DocHub 幂等初始化种子脚本（开发与集成测试环境专用）。

初始化内容：
1. 全平台功能权限字典（`permissions`）与平台系统角色（`SYSTEM_ADMIN`）。
2. 两个严格隔离的演示组织：`acme-corp`（主演示企业）与 `beta-corp`（跨租户隔离验证企业）。
3. 组织部门树与 Closure Table：
   - `acme-corp`：总部 -> 财务部 / 人力资源部 / 客服部 / 研发部
   - `beta-corp`：Beta 总部 -> Beta 研发部
4. 各组织标准 `OrganizationRole` 及权限绑定。
5. 六个演示账号：`admin@acme.local`、`hr@acme.local`、`employee@acme.local`、
   `km@acme.local`、`auditor@acme.local`、`bob@beta.local`。
6. Slice 2 演示模型 Profile 与 Knowledge Space：
   - 默认 EmbeddingProvider/Profile：`qwen-text-embedding-v3`（1024 维）与 `local-bge-m3`（1024 维）
   - `acme-corp` 演示空间：`finance-general`（PUBLIC）、`hr-executive`（PRIVATE，显式加入 hr/km）、
     `customer-support`（PUBLIC）
   - `beta-corp` 演示空间：`beta-general`（PUBLIC）

安全说明：
- 所有租户表写入均通过 `org_rls_session` 在 `app_runtime` 角色下完成，严格遵循 FORCE RLS。
- 开发测试密码优先读取环境变量 `DOCHUB_SEED_DEMO_PASSWORD`；在 `production` 环境下强制拒绝
  使用回退口令，防止弱口令进入生产环境。
"""

from __future__ import annotations

import asyncio
import logging
import os
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.core.logging import configure_logging
from app.core.security import hash_password
from app.db.session import AsyncSessionLocal, dispose_runtime_engine, org_rls_session
from app.modules.departments.models import Department, DepartmentMembership
from app.modules.departments.service import create_department_in_tx
from app.modules.identity.models import User
from app.modules.indexing.models import EmbeddingProfile, EmbeddingProvider
from app.modules.knowledge_spaces.models import (
    KnowledgeSpace,
    SpaceMembership,
    SpacePolicy,
)
from app.modules.organizations.models import Organization, OrganizationMember
from app.modules.roles.catalog import (
    DEFAULT_ORGANIZATION_ROLE_SPECS,
    PERMISSION_CATALOG,
    SYSTEM_ROLE_SPECS,
)
from app.modules.roles.models import (
    OrganizationRole,
    OrganizationRolePermission,
    Permission,
    SystemRole,
    SystemRolePermission,
    UserRoleMembership,
    UserSystemRole,
)

logger = logging.getLogger(__name__)

_DEV_FALLBACK_PASSWORD_ENV = "DOCHUB_SEED_DEMO_PASSWORD"
_DEV_DEFAULT_PASSWORD = "DocHubDemo#2026!"


def resolve_seed_password(settings: Settings) -> str:
    """解析初始化演示账号口令；生产环境禁止未显式配置口令时运行种子脚本。"""
    env_pwd = os.getenv(_DEV_FALLBACK_PASSWORD_ENV, "").strip()
    if env_pwd:
        return env_pwd
    if settings.app_env == "production":
        raise RuntimeError(
            f"生产环境禁止使用默认演示密码，必须显式设置环境变量 {_DEV_FALLBACK_PASSWORD_ENV}"
        )
    return _DEV_DEFAULT_PASSWORD


async def _ensure_permissions_and_system_roles(
    session: AsyncSession,
) -> tuple[dict[str, Permission], dict[str, SystemRole]]:
    """幂等初始化权限字典与平台 SystemRole。"""
    perm_by_code: dict[str, Permission] = {}
    sys_role_by_code: dict[str, SystemRole] = {}

    async with session.begin():
        for spec in PERMISSION_CATALOG:
            perm = (
                await session.execute(select(Permission).where(Permission.code == spec.code))
            ).scalar_one_or_none()
            if perm is None:
                perm = Permission(
                    code=spec.code,
                    scope=spec.scope,
                    description=spec.description,
                )
                session.add(perm)
                await session.flush()
            else:
                perm.scope = spec.scope
                perm.description = spec.description
            perm_by_code[spec.code] = perm

        for role_spec in SYSTEM_ROLE_SPECS:
            sys_role = (
                await session.execute(
                    select(SystemRole).where(SystemRole.code == role_spec.code)
                )
            ).scalar_one_or_none()
            if sys_role is None:
                sys_role = SystemRole(
                    code=role_spec.code,
                    name=role_spec.name,
                    description=role_spec.description,
                )
                session.add(sys_role)
                await session.flush()
            sys_role_by_code[role_spec.code] = sys_role

            existing_perm_ids = set(
                (
                    await session.execute(
                        select(SystemRolePermission.permission_id).where(
                            SystemRolePermission.system_role_id == sys_role.id
                        )
                    )
                )
                .scalars()
                .all()
            )
            for perm_code in role_spec.permission_codes:
                perm = perm_by_code[perm_code]
                if perm.id not in existing_perm_ids:
                    session.add(
                        SystemRolePermission(
                            system_role_id=sys_role.id,
                            permission_id=perm.id,
                        )
                    )

    return perm_by_code, sys_role_by_code


async def _ensure_organization(
    session: AsyncSession,
    *,
    slug: str,
    name: str,
) -> Organization:
    """幂等创建或读取企业组织。"""
    async with session.begin():
        org = (
            await session.execute(select(Organization).where(Organization.slug == slug))
        ).scalar_one_or_none()
        if org is None:
            org = Organization(slug=slug, name=name, status="ACTIVE")
            session.add(org)
            await session.flush()
    return org


async def _ensure_user(
    session: AsyncSession,
    *,
    email: str,
    display_name: str,
    password_hash: str,
) -> User:
    """幂等创建或读取用户账号。"""
    normalized = email.strip().lower()
    async with session.begin():
        user = (
            await session.execute(select(User).where(User.email == normalized))
        ).scalar_one_or_none()
        if user is None:
            user = User(
                email=normalized,
                display_name=display_name,
                password_hash=password_hash,
                status="ACTIVE",
            )
            session.add(user)
            await session.flush()
    return user


async def _ensure_org_roles_and_departments(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    perm_by_code: dict[str, Permission],
    root_dept_name: str,
    child_dept_names: tuple[str, ...],
) -> tuple[dict[str, OrganizationRole], dict[str, Department]]:
    """在 org_rls_session 下幂等初始化组织角色与部门树（含 Closure Table）。"""
    role_by_code: dict[str, OrganizationRole] = {}
    dept_by_name: dict[str, Department] = {}

    async with org_rls_session(session, organization_id):
        for spec in DEFAULT_ORGANIZATION_ROLE_SPECS:
            role = (
                await session.execute(
                    select(OrganizationRole).where(
                        OrganizationRole.organization_id == organization_id,
                        OrganizationRole.code == spec.code,
                    )
                )
            ).scalar_one_or_none()
            if role is None:
                role = OrganizationRole(
                    organization_id=organization_id,
                    code=spec.code,
                    name=spec.name,
                    description=spec.description,
                    status="ACTIVE",
                )
                session.add(role)
                await session.flush()
            role_by_code[spec.code] = role

            existing_perm_ids = set(
                (
                    await session.execute(
                        select(OrganizationRolePermission.permission_id).where(
                            OrganizationRolePermission.organization_id == organization_id,
                            OrganizationRolePermission.organization_role_id == role.id,
                        )
                    )
                )
                .scalars()
                .all()
            )
            for p_code in spec.permission_codes:
                perm = perm_by_code[p_code]
                if perm.id not in existing_perm_ids:
                    session.add(
                        OrganizationRolePermission(
                            organization_id=organization_id,
                            organization_role_id=role.id,
                            permission_id=perm.id,
                        )
                    )

        root_dept = (
            await session.execute(
                select(Department).where(
                    Department.organization_id == organization_id,
                    Department.parent_id.is_(None),
                    Department.name == root_dept_name,
                )
            )
        ).scalar_one_or_none()
        if root_dept is None:
            root_dept = await create_department_in_tx(
                session,
                organization_id=organization_id,
                name=root_dept_name,
                parent_id=None,
            )
        dept_by_name[root_dept_name] = root_dept

        for child_name in child_dept_names:
            child_dept = (
                await session.execute(
                    select(Department).where(
                        Department.organization_id == organization_id,
                        Department.parent_id == root_dept.id,
                        Department.name == child_name,
                    )
                )
            ).scalar_one_or_none()
            if child_dept is None:
                child_dept = await create_department_in_tx(
                    session,
                    organization_id=organization_id,
                    name=child_name,
                    parent_id=root_dept.id,
                )
            dept_by_name[child_name] = child_dept

    return role_by_code, dept_by_name


async def _ensure_member_bindings(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    user: User,
    department: Department,
    org_role: OrganizationRole | None,
) -> None:
    """在 org_rls_session 下幂等绑定组织成员、主部门归属及可选组织角色。"""
    async with org_rls_session(session, organization_id, user_id=user.id):
        member = (
            await session.execute(
                select(OrganizationMember).where(
                    OrganizationMember.organization_id == organization_id,
                    OrganizationMember.user_id == user.id,
                )
            )
        ).scalar_one_or_none()
        if member is None:
            session.add(
                OrganizationMember(
                    organization_id=organization_id,
                    user_id=user.id,
                    status="ACTIVE",
                )
            )

        dept_membership = (
            await session.execute(
                select(DepartmentMembership).where(
                    DepartmentMembership.organization_id == organization_id,
                    DepartmentMembership.department_id == department.id,
                    DepartmentMembership.user_id == user.id,
                    DepartmentMembership.status == "ACTIVE",
                )
            )
        ).scalar_one_or_none()
        if dept_membership is None:
            session.add(
                DepartmentMembership(
                    organization_id=organization_id,
                    department_id=department.id,
                    user_id=user.id,
                    membership_type="PRIMARY",
                    is_primary=True,
                    status="ACTIVE",
                )
            )

        if org_role is not None:
            role_membership = (
                await session.execute(
                    select(UserRoleMembership).where(
                        UserRoleMembership.organization_id == organization_id,
                        UserRoleMembership.user_id == user.id,
                        UserRoleMembership.organization_role_id == org_role.id,
                        UserRoleMembership.status == "ACTIVE",
                    )
                )
            ).scalar_one_or_none()
            if role_membership is None:
                session.add(
                    UserRoleMembership(
                        organization_id=organization_id,
                        user_id=user.id,
                        organization_role_id=org_role.id,
                        status="ACTIVE",
                    )
                )


async def _ensure_embedding_profiles_for_org(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    settings: Settings,
) -> dict[str, EmbeddingProfile]:
    """在 org_rls_session 下幂等初始化 Qwen 优先与本地 BGE 备用向量模型 Profile。"""
    profiles: dict[str, EmbeddingProfile] = {}

    async with org_rls_session(session, organization_id):
        provider_specs = (
            ("qwen-official", "qwen", "config:QWEN_API_BASE_URL", True),
            (
                "local-bge-service",
                "local_bge",
                "config:LOCAL_BGE_EMBEDDING_URL",
                settings.local_bge_embedding_enabled,
            ),
        )
        providers: dict[str, EmbeddingProvider] = {}
        for code, ptype, ref, enabled in provider_specs:
            prov = (
                await session.execute(
                    select(EmbeddingProvider).where(
                        EmbeddingProvider.organization_id == organization_id,
                        EmbeddingProvider.code == code,
                    )
                )
            ).scalar_one_or_none()
            if prov is None:
                prov = EmbeddingProvider(
                    organization_id=organization_id,
                    code=code,
                    provider_type=ptype,
                    base_url_ref=ref,
                    enabled=enabled,
                )
                session.add(prov)
                await session.flush()
            providers[code] = prov

        profile_specs = (
            (
                "qwen-text-embedding-v3",
                providers["qwen-official"].id,
                settings.qwen_embedding_model,
                settings.qwen_embedding_dimensions,
            ),
            (
                "local-bge-m3",
                providers["local-bge-service"].id,
                settings.local_bge_embedding_model,
                settings.local_bge_embedding_dimensions,
            ),
        )
        for p_code, provider_id, model_id, dims in profile_specs:
            prof = (
                await session.execute(
                    select(EmbeddingProfile).where(
                        EmbeddingProfile.organization_id == organization_id,
                        EmbeddingProfile.code == p_code,
                    )
                )
            ).scalar_one_or_none()
            if prof is None:
                prof = EmbeddingProfile(
                    organization_id=organization_id,
                    provider_id=provider_id,
                    code=p_code,
                    model_id=model_id,
                    model_version="v1",
                    dimensions=dims,
                    language_config={"languages": ["zh-CN", "en"], "sparse_tokenizer": "jieba"},
                    status="ACTIVE",
                )
                session.add(prof)
                await session.flush()
            profiles[p_code] = prof

    return profiles


async def _ensure_space(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    slug: str,
    name: str,
    description: str,
    visibility: str,
    answer_policy: str,
    default_profile_id: uuid.UUID,
    explicit_member_users: tuple[User, ...] = (),
) -> KnowledgeSpace:
    """在 org_rls_session 下幂等初始化 KnowledgeSpace、策略及私有空间显式成员。"""
    async with org_rls_session(session, organization_id):
        space = (
            await session.execute(
                select(KnowledgeSpace).where(
                    KnowledgeSpace.organization_id == organization_id,
                    KnowledgeSpace.slug == slug,
                )
            )
        ).scalar_one_or_none()
        if space is None:
            space = KnowledgeSpace(
                organization_id=organization_id,
                name=name,
                slug=slug,
                description=description,
                visibility=visibility,
                answer_policy=answer_policy,
                default_embedding_profile_id=default_profile_id,
                status="ACTIVE",
                config_json={"answer_policy": answer_policy},
                config_version=1,
            )
            session.add(space)
            await session.flush()

            session.add(
                SpacePolicy(
                    organization_id=organization_id,
                    space_id=space.id,
                    policy_json={"visibility": visibility, "answer_policy": answer_policy},
                    revision=1,
                )
            )

        for member_user in explicit_member_users:
            existing_sm = (
                await session.execute(
                    select(SpaceMembership).where(
                        SpaceMembership.organization_id == organization_id,
                        SpaceMembership.space_id == space.id,
                        SpaceMembership.user_id == member_user.id,
                    )
                )
            ).scalar_one_or_none()
            if existing_sm is None:
                session.add(
                    SpaceMembership(
                        organization_id=organization_id,
                        space_id=space.id,
                        user_id=member_user.id,
                        role="MEMBER",
                        status="ACTIVE",
                    )
                )

    return space


async def seed_database(
    session: AsyncSession,
    *,
    settings: Settings | None = None,
) -> dict[str, uuid.UUID]:
    """执行幂等全量种子初始化，返回关键组织与示范 Space ID 映射。"""
    cfg = settings or get_settings()
    demo_password = resolve_seed_password(cfg)
    shared_password_hash = hash_password(demo_password)

    perm_by_code, sys_role_by_code = await _ensure_permissions_and_system_roles(session)

    # 1. 创建两个强隔离演示组织
    acme_org = await _ensure_organization(
        session,
        slug="acme-corp",
        name="Acme Corp 示范企业",
    )
    beta_org = await _ensure_organization(
        session,
        slug="beta-corp",
        name="Beta Corp 隔离测试企业",
    )

    # 2. 初始化各自组织的角色与部门树（含 Closure Table）
    acme_roles, acme_depts = await _ensure_org_roles_and_departments(
        session,
        organization_id=acme_org.id,
        perm_by_code=perm_by_code,
        root_dept_name="总部",
        child_dept_names=("财务部", "人力资源部", "客服部", "研发部"),
    )
    beta_roles, beta_depts = await _ensure_org_roles_and_departments(
        session,
        organization_id=beta_org.id,
        perm_by_code=perm_by_code,
        root_dept_name="Beta 总部",
        child_dept_names=("Beta 研发部",),
    )

    # 3. 幂等初始化 6 个演示账号
    admin_user = await _ensure_user(
        session,
        email="admin@acme.local",
        display_name="Acme 系统管理员",
        password_hash=shared_password_hash,
    )
    hr_user = await _ensure_user(
        session,
        email="hr@acme.local",
        display_name="Acme 人力资源专员",
        password_hash=shared_password_hash,
    )
    employee_user = await _ensure_user(
        session,
        email="employee@acme.local",
        display_name="Acme 研发员工",
        password_hash=shared_password_hash,
    )
    km_user = await _ensure_user(
        session,
        email="km@acme.local",
        display_name="Acme 知识库管理员",
        password_hash=shared_password_hash,
    )
    auditor_user = await _ensure_user(
        session,
        email="auditor@acme.local",
        display_name="Acme 安全审计员",
        password_hash=shared_password_hash,
    )
    bob_user = await _ensure_user(
        session,
        email="bob@beta.local",
        display_name="Beta 研发员工",
        password_hash=shared_password_hash,
    )

    # 4. 授予 admin@acme.local 平台 SYSTEM_ADMIN（绝不授予任何文档读取组织角色）
    system_admin_role = sys_role_by_code["SYSTEM_ADMIN"]
    async with session.begin():
        existing_sys_binding = (
            await session.execute(
                select(UserSystemRole).where(
                    UserSystemRole.user_id == admin_user.id,
                    UserSystemRole.system_role_id == system_admin_role.id,
                )
            )
        ).scalar_one_or_none()
        if existing_sys_binding is None:
            session.add(
                UserSystemRole(
                    user_id=admin_user.id,
                    system_role_id=system_admin_role.id,
                    status="ACTIVE",
                )
            )

    # 5. 绑定各账号的组织成员、部门归属与组织角色
    await _ensure_member_bindings(
        session,
        organization_id=acme_org.id,
        user=admin_user,
        department=acme_depts["总部"],
        org_role=None,
    )
    await _ensure_member_bindings(
        session,
        organization_id=acme_org.id,
        user=hr_user,
        department=acme_depts["人力资源部"],
        org_role=acme_roles["HR"],
    )
    await _ensure_member_bindings(
        session,
        organization_id=acme_org.id,
        user=employee_user,
        department=acme_depts["研发部"],
        org_role=acme_roles["EMPLOYEE"],
    )
    await _ensure_member_bindings(
        session,
        organization_id=acme_org.id,
        user=km_user,
        department=acme_depts["总部"],
        org_role=acme_roles["KNOWLEDGE_MANAGER"],
    )
    await _ensure_member_bindings(
        session,
        organization_id=acme_org.id,
        user=auditor_user,
        department=acme_depts["总部"],
        org_role=acme_roles["SECURITY_AUDITOR"],
    )
    await _ensure_member_bindings(
        session,
        organization_id=beta_org.id,
        user=bob_user,
        department=beta_depts["Beta 研发部"],
        org_role=beta_roles["EMPLOYEE"],
    )

    # 6. 初始化 EmbeddingProfile 与演示 KnowledgeSpace
    acme_profiles = await _ensure_embedding_profiles_for_org(
        session,
        organization_id=acme_org.id,
        settings=cfg,
    )
    beta_profiles = await _ensure_embedding_profiles_for_org(
        session,
        organization_id=beta_org.id,
        settings=cfg,
    )

    qwen_acme_id = acme_profiles["qwen-text-embedding-v3"].id
    bge_acme_id = acme_profiles["local-bge-m3"].id
    primary_acme_id = (
        bge_acme_id if cfg.embedding_primary_provider == "local_bge" else qwen_acme_id
    )
    primary_beta_id = (
        beta_profiles["local-bge-m3"].id
        if cfg.embedding_primary_provider == "local_bge"
        else beta_profiles["qwen-text-embedding-v3"].id
    )

    finance_space = await _ensure_space(
        session,
        organization_id=acme_org.id,
        slug="finance-general",
        name="财务与报销制度空间",
        description="面向企业全员的公开财务报销指南与合规制度（受文档四维 ACL 控制）",
        visibility="PUBLIC",
        answer_policy="STRICT_EVIDENCE",
        default_profile_id=primary_acme_id,
    )
    hr_exec_space = await _ensure_space(
        session,
        organization_id=acme_org.id,
        slug="hr-executive",
        name="HR 薪酬与人事机密空间",
        description="私有空间：仅限 HR 专员与知识库管理员授权成员进入",
        visibility="PRIVATE",
        answer_policy="STRICT_EVIDENCE",
        default_profile_id=bge_acme_id,
        explicit_member_users=(hr_user, km_user),
    )
    support_space = await _ensure_space(
        session,
        organization_id=acme_org.id,
        slug="customer-support",
        name="客服知识与工单手册空间",
        description="公开客服话术与产品排查手册空间",
        visibility="PUBLIC",
        answer_policy="GROUNDED_WITH_GENERAL_CONTEXT",
        default_profile_id=primary_acme_id,
    )
    beta_space = await _ensure_space(
        session,
        organization_id=beta_org.id,
        slug="beta-general",
        name="Beta 默认知识空间",
        description="Beta 组织隔离测试空间",
        visibility="PUBLIC",
        answer_policy="STRICT_EVIDENCE",
        default_profile_id=primary_beta_id,
    )

    logger.info(
        "种子初始化完成: acme-corp=%s beta-corp=%s",
        acme_org.id,
        beta_org.id,
    )
    return {
        "acme-corp": acme_org.id,
        "beta-corp": beta_org.id,
        "space:finance-general": finance_space.id,
        "space:hr-executive": hr_exec_space.id,
        "space:customer-support": support_space.id,
        "space:beta-general": beta_space.id,
    }


async def _cli_main() -> None:
    """命令行异步入口。"""
    settings = get_settings()
    configure_logging(settings.log_level)
    try:
        async with AsyncSessionLocal() as session:
            await seed_database(session, settings=settings)
    finally:
        await dispose_runtime_engine()


if __name__ == "__main__":
    asyncio.run(_cli_main())
