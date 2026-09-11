# jax-voice-bridge：云端无头 TRTC 对端 + rtc_bridge（原 PC 端两个进程合并）
#
# 构建上下文 = 仓库根。必须由 tracked 源码构成（见 cloudbridge/CANONICAL_SOURCE.json）。
# 三层职责：
#   1) python 运行时：跑 rtc_bridge（模型/整形桥）
#   2) node + electron + TRTC 原生 addon：跑无头 sidecar（TRTC 对端）
#   3) xvfb：Electron 需要 X display，容器内以虚拟显示无头运行
#
# 与 cloudapi/Dockerfile 保持同一套目录约定（backend/app 落在 /srv/backend/app，
# 运行时配置落在 /config），避免两套布局分叉。
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/srv/backend \
    DEBIAN_FRONTEND=noninteractive

WORKDIR /srv

# Electron 在 Linux 无头运行所需的共享库 + xvfb（虚拟 X）+ xauth（xvfb-run 必需）+ node/npm。
# 这些不是"本地绕行"，而是让同一份 sidecar 代码能在容器里原样运行的运行期依赖。
# 实测教训：缺 xauth 时 `xvfb-run` 直接报 "xauth command not found"，sidecar 起不来。
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates \
        xvfb \
        xauth \
        nodejs \
        npm \
        libnss3 \
        libatk1.0-0 \
        libatk-bridge2.0-0 \
        libgtk-3-0 \
        libgbm1 \
        libasound2 \
        libxss1 \
        libx11-xcb1 \
        libxcomposite1 \
        libxdamage1 \
        libxrandr2 \
        libxkbcommon0 \
        libxfixes3 \
        libdrm2 \
        fonts-liberation \
    && rm -rf /var/lib/apt/lists/*

# Python 依赖：与 cloudapi 同一闭包（rtc_bridge 会 import app.*），额外需要 websockets。
COPY cloudbridge/requirements.txt ./cloudbridge-requirements.txt
RUN pip install --no-cache-dir -r cloudbridge-requirements.txt

# 受跟踪的三棵树：rtc_bridge（零改动）、sidecar（零改动）、backend/app（共享协议与配置）
COPY backend/app ./backend/app
COPY backend/rtc_bridge ./backend/rtc_bridge
COPY sidecar ./sidecar
# 运行时配置：app/_frozen_paths.project_root() 在容器内推导为 /，即 /config
COPY config /config

# TLS 信任锚（ADR-020 A1）：sidecar 在 app.whenReady() 前必须读到 NODE_EXTRA_CA_CERTS，
# 否则 fail-closed 直接退出（实测：[main] fatal NODE_EXTRA_CA_CERTS is not set）。
# 固定的是**签发云端控制面 leaf 的那张中间 CA**（DigiCert Secure Site OV G2 TLS ... CA1），
# 由 openssl s_client 从线上真实链路抽取，比信任整个系统 CA 包严格得多。
# ⚠️ 该中间证书 notAfter=2032-12-14；若 CloudBase 更换签发 CA，此文件必须同步更新，
#    否则 sidecar 会 fail-closed 拒绝连接（这是设计意图，不是缺陷）。
COPY certs/cloud-control-plane-issuer.pem /srv/certs/cloud-control-plane-issuer.pem
ENV NODE_EXTRA_CA_CERTS=/srv/certs/cloud-control-plane-issuer.pem

# sidecar 依赖：trtc-electron-sdk 的 postinstall 会拉取 Linux 原生 addon，
# electron 会下载 Linux 运行时。**不把本机 node_modules 带进镜像**（Windows 二进制）。
RUN cd sidecar && npm install --no-audit --no-fund

COPY cloudbridge ./cloudbridge

# 平台只暴露一个端口；rtc_bridge 的 19092/19093 保持容器内 localhost，不对外。
EXPOSE 9200

# 监督进程：子进程退出即整体退出（由平台重启），替代本地 watchdog。
CMD ["python", "cloudbridge/supervisor.py"]
