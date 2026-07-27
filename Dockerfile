# ProcTHOR Docker 镜像 — 程序化室内场景生成服务
# 参考 HoloScene 的 Docker 构建策略
# Build: DOCKER_BUILDKIT=1 docker compose build
FROM python:3.10-slim

EXPOSE 8001

# 切换 apt 源到阿里云镜像（国内加速）
RUN sed -i 's|deb.debian.org|mirrors.aliyun.com|g; s|security.debian.org|mirrors.aliyun.com|g' \
    /etc/apt/sources.list.d/debian.sources \
    && apt-get update \
    && apt-get install -y --no-install-recommends \
        build-essential \
        xvfb \
        libgl1 \
        libglib2.0-0 \
        libsm6 \
        libxext6 \
        libxrender1 \
        libxrandr2 \
        libxfixes3 \
        libxi6 \
        libxinerama1 \
        libxcursor1 \
        libnss3 \
        libasound2 \
    && rm -rf /var/lib/apt/lists/* \
    && apt-get clean

# 配置 pip 源为阿里云
RUN mkdir -p /root/.config/pip \
    && printf "[global]\nindex-url = https://mirrors.aliyun.com/pypi/simple\ntrusted-host = mirrors.aliyun.com\n" \
    > /root/.config/pip/pip.conf

# 环境变量
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    DISPLAY=:99

# 工作目录
WORKDIR /app

# 先复制依赖清单，利用构建缓存
COPY requirements.txt ./

# 安装依赖
RUN python -m pip install --upgrade pip setuptools wheel \
    && pip install -r requirements.txt

# 复制项目代码
COPY . .

# 创建非 root 用户
RUN adduser -u 5678 --disabled-password --gecos "" appuser && chown -R appuser /app
USER appuser

# 启动 Xvfb + gunicorn
CMD sh -c "Xvfb :99 -screen 0 1920x1080x24 & gunicorn --bind 0.0.0.0:8001 -k uvicorn.workers.UvicornWorker server:app"