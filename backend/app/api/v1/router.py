"""API v1 主路由器聚合入口。

集中挂载基础设施探针与各 Vertical Slice 业务领域路由至 `/api/v1`。
"""

from __future__ import annotations

from fastapi import APIRouter

from app.api.v1.health import router as health_router
from app.modules.acl.router import router as acl_router
from app.modules.analytics.router import analytics_router, benchmarks_router
from app.modules.audit.router import router as audit_router
from app.modules.departments.router import router as departments_router
from app.modules.documents.router import router as documents_router
from app.modules.identity.router import router as auth_router
from app.modules.knowledge_spaces.router import org_spaces_router, spaces_router
from app.modules.organizations.router import router as organizations_router
from app.modules.providers.router import router as providers_router
from app.modules.roles.router import org_roles_router, permissions_router

api_v1_router = APIRouter(prefix="/api/v1")

# 基础设施探针路由
api_v1_router.include_router(health_router)

# Slice 1：身份认证、多组织、部门树与 RBAC 路由
api_v1_router.include_router(auth_router)
api_v1_router.include_router(organizations_router)
api_v1_router.include_router(departments_router)
api_v1_router.include_router(permissions_router)
api_v1_router.include_router(org_roles_router)

# Slice 2：Knowledge Space、四维 Document ACL 与文档安全导入/发布路由
api_v1_router.include_router(org_spaces_router)
api_v1_router.include_router(spaces_router)
api_v1_router.include_router(acl_router)
api_v1_router.include_router(documents_router)

# Slice 8：运营分析、双审计中心、Provider 策略与 Benchmark 治理路由
api_v1_router.include_router(audit_router)
api_v1_router.include_router(analytics_router)
api_v1_router.include_router(benchmarks_router)
api_v1_router.include_router(providers_router)
