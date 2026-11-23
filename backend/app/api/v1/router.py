"""API v1 主路由器聚合入口。

后续 Vertical Slice 1~8 的各业务领域路由统一在此注册挂载到 `/api/v1`。
"""

from __future__ import annotations

from fastapi import APIRouter

from app.api.v1.health import router as health_router

api_v1_router = APIRouter(prefix="/api/v1")

# 基础设施探针路由
api_v1_router.include_router(health_router)
