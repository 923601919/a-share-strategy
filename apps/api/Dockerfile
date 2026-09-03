FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# 依赖层（利用构建缓存；akshare/pandas 均有 py3.11 linux 轮子）
# PIP_INDEX_URL 可在国内用腾讯/阿里 PyPI 镜像加速（docker-compose build args 传入）
ARG PIP_INDEX_URL=https://pypi.org/simple/
COPY apps/api/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -i "${PIP_INDEX_URL}" -r /app/requirements.txt

# 拷贝整个仓库（.dockerignore 排除 venv/node_modules/.next/.git/.env 等）
COPY . /app

WORKDIR /app/apps/api

EXPOSE 8000

# 单 worker：内存任务队列 + spawn 隔离子进程，不支持多 worker 共享
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
