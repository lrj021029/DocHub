"""权限字典与默认 SystemRole / OrganizationRole 角色权限矩阵（单一事实来源）。

安全红线：
1. 明确分离平台级 `SYSTEM` 权限与组织级 `ORGANIZATION` 权限。
2. `SYSTEM_ADMIN` 仅拥有平台治理与组织架构管理权限，**严禁隐式授予 `document:read`、
   `document:write`、`chat:use`、`faq:read` 等业务知识内容访问权限**。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class PermissionSpec:
    """标准功能权限定义。"""

    code: str
    scope: str  # SYSTEM 或 ORGANIZATION
    description: str


@dataclass(frozen=True, slots=True)
class RoleSpec:
    """标准角色及默认权限绑定定义。"""

    code: str
    name: str
    description: str
    permission_codes: tuple[str, ...]


# 全平台标准功能权限树
PERMISSION_CATALOG: tuple[PermissionSpec, ...] = (
    # --- 平台级 SYSTEM 权限 ---
    PermissionSpec("system:org:manage", "SYSTEM", "创建与管理平台企业组织"),
    PermissionSpec("system:user:manage", "SYSTEM", "管理平台级用户账号状态与系统角色"),
    PermissionSpec("system:provider:manage", "SYSTEM", "配置平台模型与文档解析 Provider"),
    # --- 组织级 ORGANIZATION 权限 ---
    PermissionSpec("org:member:read", "ORGANIZATION", "查看当前组织成员列表"),
    PermissionSpec("org:member:manage", "ORGANIZATION", "添加、启用或停用当前组织成员"),
    PermissionSpec("org:department:read", "ORGANIZATION", "查看当前组织部门树与成员归属"),
    PermissionSpec("org:department:manage", "ORGANIZATION", "创建部门、移动子树及管理多部门兼职"),
    PermissionSpec("org:role:read", "ORGANIZATION", "查看当前组织角色与功能权限分配"),
    PermissionSpec("org:role:manage", "ORGANIZATION", "创建组织角色并向成员授予或撤销角色"),
    PermissionSpec("space:read", "ORGANIZATION", "查看当前组织知识空间元数据"),
    PermissionSpec("space:manage", "ORGANIZATION", "创建与维护知识空间及入口策略"),
    PermissionSpec("document:read", "ORGANIZATION", "阅读授权文档内容（须叠加四维 ACL 复核）"),
    PermissionSpec("document:write", "ORGANIZATION", "上传、解析、切片与发布文档版本"),
    PermissionSpec("document:acl:manage", "ORGANIZATION", "配置与变更文档四维 ACL 授权版本"),
    PermissionSpec("chat:use", "ORGANIZATION", "使用受 ACL 保护的 AI 问答工作台"),
    PermissionSpec("faq:read", "ORGANIZATION", "查阅已发布且具备来源授权的 FAQ"),
    PermissionSpec("faq:manage", "ORGANIZATION", "触发 FAQ 挖掘、审核、发布与停用"),
    PermissionSpec("gap:manage", "ORGANIZATION", "管理知识缺口队列与整改补充任务"),
    PermissionSpec("analytics:read", "ORGANIZATION", "查看运营概览、FAQ 与知识缺口指标"),
    PermissionSpec("audit:read", "ORGANIZATION", "查看当前组织业务操作审计日志"),
    PermissionSpec(
        "security_audit:read",
        "ORGANIZATION",
        "查看安全审计日志（ACL 拒绝、重放攻击、越权尝试）",
    ),
)

# 平台级 SystemRole 矩阵（SYSTEM_ADMIN 严禁包含 document:read / chat:use / faq:read）
SYSTEM_ROLE_SPECS: tuple[RoleSpec, ...] = (
    RoleSpec(
        code="SYSTEM_ADMIN",
        name="平台系统管理员",
        description="负责平台组织、账号与基础设施治理；不隐式具备任何业务文档读取权限",
        permission_codes=(
            "system:org:manage",
            "system:user:manage",
            "system:provider:manage",
            "org:member:read",
            "org:member:manage",
            "org:department:read",
            "org:department:manage",
            "org:role:read",
            "org:role:manage",
            "audit:read",
        ),
    ),
)

# 默认企业组织 OrganizationRole 矩阵（新建组织或 seed 时自动初始化）
DEFAULT_ORGANIZATION_ROLE_SPECS: tuple[RoleSpec, ...] = (
    RoleSpec(
        code="EMPLOYEE",
        name="普通员工",
        description="具备基础文档阅读、AI 问答与已发布 FAQ 查阅权限（受四维 ACL 约束）",
        permission_codes=(
            "org:department:read",
            "space:read",
            "document:read",
            "chat:use",
            "faq:read",
        ),
    ),
    RoleSpec(
        code="HR",
        name="人力资源专员",
        description="负责 HR 制度维护、成员查阅及授权范围内的知识问答与文档导入",
        permission_codes=(
            "org:member:read",
            "org:department:read",
            "space:read",
            "document:read",
            "document:write",
            "chat:use",
            "faq:read",
        ),
    ),
    RoleSpec(
        code="FINANCE_MANAGER",
        name="财务经理",
        description="负责财务制度文档维护、分析查阅与授权范围内问答",
        permission_codes=(
            "org:department:read",
            "space:read",
            "document:read",
            "document:write",
            "chat:use",
            "faq:read",
            "analytics:read",
        ),
    ),
    RoleSpec(
        code="KNOWLEDGE_MANAGER",
        name="知识库管理员",
        description="负责知识空间管理、文档与 ACL 治理、FAQ 审核发布及知识缺口闭环",
        permission_codes=(
            "org:member:read",
            "org:department:read",
            "org:role:read",
            "space:read",
            "space:manage",
            "document:read",
            "document:write",
            "document:acl:manage",
            "chat:use",
            "faq:read",
            "faq:manage",
            "gap:manage",
            "analytics:read",
        ),
    ),
    RoleSpec(
        code="SECURITY_AUDITOR",
        name="安全审计员",
        description="专职查阅业务审计与安全审计事件（ACL 拒绝、重放、越权），职责独立分离",
        permission_codes=(
            "org:member:read",
            "org:department:read",
            "org:role:read",
            "audit:read",
            "security_audit:read",
            "analytics:read",
        ),
    ),
)
