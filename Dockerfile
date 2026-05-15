# Use BuildKit syntax to allow cache mounts for pip and ai2thor assets.
# Build with BuildKit enabled: e.g. `DOCKER_BUILDKIT=1 docker build .` or
# `COMPOSE_DOCKER_CLI_BUILD=1 DOCKER_BUILDKIT=1 docker compose up --build`.
# For backwards compatibility, enable BuildKit only when building; otherwise
# the `--mount` option requires BuildKit and the build will fail.
# syntax=docker/dockerfile:1.4
FROM python:3.10-slim

EXPOSE 8001

# Keeps Python from generating .pyc files in the container
ENV PYTHONDONTWRITEBYTECODE=1

# Turns off buffering for easier container logging
ENV PYTHONUNBUFFERED=1

# Xvfb display
ENV DISPLAY=:99

# Install system dependencies + Xvfb
# 安装 Unity/AI2-THOR 依赖
RUN apt-get update && apt-get install -y \
    xvfb \
    ffmpeg \
    libsm6 \
    libxext6 \
    libxrender-dev \
    libgl1 \
    libglib2.0-0 \
    libgtk2.0-0 \
    libgtk-3-0 \
    libx11-6 \
    libnss3 \
    libasound2 \
    libxcursor1 \
    libxrandr2 \
    libxi6 \
    libxinerama1 \
    libxxf86vm1 \
    mesa-utils \
    wget \
    unzip \
    && rm -rf /var/lib/apt/lists/*

# Install pip requirements. Use BuildKit cache mounts to persist pip downloads
# and ai2thor runtime assets between builds (speeds rebuilds and avoids
# re-downloading thor-Linux64-*.zip).
COPY requirements.txt .
RUN python -m pip install -r requirements.txt

WORKDIR /app
COPY . /app

# Creates a non-root user with an explicit UID and adds permission to access the /app folder
# For more info, please refer to https://aka.ms/vscode-docker-python-configure-containers
RUN adduser -u 5678 --disabled-password --gecos "" appuser && chown -R appuser /app
USER appuser

# Start Xvfb first, then launch gunicorn
CMD sh -c "Xvfb :99 -screen 0 1920x1080x24 & gunicorn --bind 0.0.0.0:8001 -k uvicorn.workers.UvicornWorker server:app"