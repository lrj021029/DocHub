"""API v1 主路由器聚合入口。

集中挂载基础设施探针与各 Vertical Slice 业务领域路由至 `/api/v1`。
"""

from __future__ import annotations

from fastapi import APIRouter

from app.api.v1.health import router as health_router
from app.modules.departments.router import router as departments_router
from app.modules.identity.router import router as auth_router
from app.modules.organizations.router import router as organizations_router
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
