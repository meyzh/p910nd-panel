#!/bin/sh
set -e
IMAGE_NAME="p910nd-panel"
TAG="latest"
TAR_FILE="${IMAGE_NAME}-${TAG}.tar"

echo "===== 导入镜像 ${TAR_FILE} ====="
docker load -i ${TAR_FILE}
echo "✅ 镜像导入完成，启动容器"
docker compose up -d
echo "✅ 启动完毕，面板地址：http://127.0.0.1:8910"
