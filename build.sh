#!/bin/sh
set -e
IMAGE_NAME="p910nd-panel"
TAG="latest"

echo "===== 开始构建镜像 ${IMAGE_NAME}:${TAG} ====="
docker compose build
echo "✅ 构建完成：${IMAGE_NAME}:${TAG}"
