# ==============================================================================
# DocHub 后端服务容器镜像（支持 FastAPI API 与 Celery Worker 共用基础镜像）
# ==============================================================================
FROM python:3.12-slim AS base

# 基础 Python 运行期安全与性能环境变量
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    APP_HOME=/app

WORKDIR ${APP_HOME}

# 安装最小必要系统依赖（curl 用于容器健康检查，ca-certificates 用于外部 HTTPS 调用）
RUN apt-get update && \
    apt-get install -y --no-install-recommends curl ca-certificates && \
    rm -rf /var/lib/apt/lists/*

# 先安装依赖以最大化利用 Docker 构建层缓存
COPY requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# 拷贝应用源码与 Alembic 迁移配置
COPY pyproject.toml ./pyproject.toml
COPY alembic.ini ./alembic.ini
COPY alembic ./alembic
COPY app ./app

# 创建非 root 运行用户，满足最小权限容器安全原则
RUN groupadd --system dochub && \
    useradd --system --gid dochub --create-home --home-dir /home/dochub dochub && \
    chown -R dochub:dochub ${APP_HOME}

USER dochub

EXPOSE 8000

# 容器存活探针（连接 /api/v1/healthz）
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8000/api/v1/healthz || exit 1

# 默认启动 FastAPI 异步服务（Celery Worker/Beat 可在 docker-compose 中覆盖 command）
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
