FROM debian:bookworm-slim

ENV TZ=Asia/Shanghai \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
        python3 \
        python3-flask \
        usbutils \
        procps \
        curl \
        ca-certificates \
        libusb-1.0-0 \
 && ( apt-get install -y --no-install-recommends python3-usb \
      || ( apt-get install -y --no-install-recommends python3-pip \
           && pip3 install --no-cache-dir --break-system-packages pyusb ) ) \
 && rm -rf /var/lib/apt/lists/*

# 构建期自检：pyusb 必须可导入，否则直接构建失败（而不是运行起来才发现）
RUN python3 -c "import usb.core, usb.util, flask; print('deps ok')"

WORKDIR /app
COPY panel.py /app/panel.py
RUN mkdir -p /app/data

EXPOSE 9100 8910

HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8910/healthz > /dev/null || exit 1

CMD ["python3", "-u", "/app/panel.py"]
