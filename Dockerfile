# Dockerfile for Atlas
# 多阶段构建，优化镜像大小

# =============================================================================
# 阶段1: 构建阶段
# =============================================================================
FROM python:3.13-slim AS builder

# 设置工作目录
WORKDIR /build

# 安装系统依赖
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    g++ \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/*

# 复制项目文件
COPY pyproject.toml ./
COPY README.md ./
COPY LICENSE ./

# 安装uv包管理器
RUN pip install --no-cache-dir uv

# 使用uv创建虚拟环境并安装依赖
RUN uv venv /opt/venv && \
    . /opt/venv/bin/activate && \
    uv pip compile pyproject.toml --all-extras -o /tmp/requirements.txt && \
    uv pip install --no-cache -r /tmp/requirements.txt && \
    rm /tmp/requirements.txt

# =============================================================================
# 阶段2: 运行阶段
# =============================================================================
FROM python:3.13-slim

# 设置环境变量
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/opt/venv/bin:$PATH" \
    PYTHONPATH="/app/src:$PYTHONPATH" \
    ATLAS_DATA_DIR=/app/data \
    ATLAS_LOG_DIR=/app/logs \
    ATLAS_CONFIG_DIR=/app/config

# 安装运行时依赖
RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq5 \
    curl \
    && rm -rf /var/lib/apt/lists/*

# 从构建阶段复制虚拟环境
COPY --from=builder /opt/venv /opt/venv

# 创建工作目录
WORKDIR /app

# 复制项目代码
COPY src/ /app/src/
COPY config/ /app/config/
COPY scripts/ /app/scripts/

# 创建数据和日志目录
RUN mkdir -p /app/data /app/logs /app/config

# 设置权限
RUN chmod +x /app/scripts/*.py /app/scripts/*.sh || true

# 健康检查
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD python -c "from atlas.scheduler.celery_app import celery_app; import sys; sys.exit(0)" || exit 1

# 默认命令（可被docker-compose覆盖）
CMD ["python", "-m", "celery", "-A", "atlas.scheduler.celery_app", "worker", "--loglevel=info"]
