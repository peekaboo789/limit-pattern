# 三日涨停形态统计 —— 无界面（网页版）镜像。
# 阶段 1：构建 Vue 前端；阶段 2：python:3.11-slim 运行 FastAPI（同时托管前端页面）。
FROM node:20-bookworm-slim AS frontend
WORKDIR /src/frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY frontend/ ./
RUN npm run build

FROM python:3.11-slim AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    TZ=Asia/Shanghai \
    LP_DATA_DIR=/data \
    LP_STATIC_DIR=/app/frontend/dist \
    LP_HEADLESS=1 \
    LP_HOST=0.0.0.0 \
    LP_PORT=8000
RUN apt-get update \
    && apt-get install -y --no-install-recommends tzdata ca-certificates curl \
    && ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && echo $TZ > /etc/timezone \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt requirements-cluster.txt ./
RUN pip install -r requirements.txt -r requirements-cluster.txt
COPY backend/ ./backend/
COPY cluster-data/ ./cluster-data/
COPY --from=frontend /src/frontend/dist ./frontend/dist
# 以 root 运行：宿主机 ./data 绑定挂载由 Docker 以 root 创建，非 root 用户会写不进去
RUN mkdir -p /data
VOLUME ["/data"]
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
  CMD curl -fsS http://127.0.0.1:8000/api/health || exit 1
CMD ["python", "-m", "backend.app.main"]
