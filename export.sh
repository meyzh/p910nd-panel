#!/bin/sh
set -e
IMAGE_NAME="p910nd-panel"
TAG="latest"
OUTPUT="${IMAGE_NAME}-${TAG}.tar"

echo "===== 导出镜像到 ${OUTPUT} ====="
docker save -o ${OUTPUT} ${IMAGE_NAME}:${TAG}
echo "✅ 导出成功，文件：${OUTPUT}"
ls -lh ${OUTPUT}
