# ==============================================================================
# DocHub 前端生产构建镜像（多阶段构建：Node 构建静态资源 + Nginx 托管）
# ==============================================================================

# 阶段一：安装依赖并执行 TypeScript 类型检查与 Vite 生产打包
FROM node:22-alpine AS builder

WORKDIR /app

# 优先复制依赖清单以充分利用 Docker 构建缓存
COPY package.json package-lock.json* ./
RUN npm ci --no-audit --no-fund || npm install --no-audit --no-fund

# 复制前端源码并构建生产产物
COPY . .
RUN npm run typecheck && npm run build

# 阶段二：使用轻量 Nginx 镜像提供 SPA 静态资源与 /api/ 反向代理（含 SSE 支持）
FROM nginx:1.27-alpine AS runtime

# 移除默认站点配置并注入 DocHub 专用 Nginx 配置
RUN rm -f /etc/nginx/conf.d/default.conf
COPY nginx.conf /etc/nginx/conf.d/default.conf

# 复制打包产物至 Nginx 静态资源目录
COPY --from=builder /app/dist /usr/share/nginx/html

EXPOSE 80

# 容器健康检查：验证静态首页可正常响应
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD wget -qO- http://127.0.0.1/healthz-ui >/dev/null 2>&1 || exit 1

CMD ["nginx", "-g", "daemon off;"]
