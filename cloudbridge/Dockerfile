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
        ffmpeg \
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
# edge-tts 用于**云端生成手机模拟器的中文提示音**（真语音，非音调），配合 ffmpeg 转 16k wav。
COPY cloudbridge/requirements.txt ./cloudbridge-requirements.txt
RUN pip install --no-cache-dir -r cloudbridge-requirements.txt \
    && pip install --no-cache-dir edge-tts==7.2.8

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

# ⚠️ 客户端私钥/证书一律**不烘进镜像**：镜像层会被任何能拉取镜像的人读到。
#    RTC_BRIDGE_CLIENT_CERT_PEM / RTC_BRIDGE_CLIENT_KEY_PEM /
#    RTC_BRIDGE_CONTROL_PLANE_CA_PEM 由 CloudRun 以环境变量注入，supervisor 启动时
#    经 cloudbridge/tls_material.py 落成 0o600 临时文件，再把 RTC_BRIDGE_*_FILE
#    注入 rtc_bridge 子进程。此处只放公开的 CA 信任锚（上面的 issuer.pem），
#    禁止 COPY client.key / client.crt / ca.key。

# sidecar 依赖：trtc-electron-sdk 的 `install` 脚本会联网下载 Linux 原生 addon，
# electron 会下载 Linux 运行时。**不把本机 node_modules 带进镜像**（Windows 二进制）。
# 注意：上面的注释曾写成「postinstall 会拉取」——实际是 `install`→`download`，且**会静默失败**，
# 所以下面必须显式校验（见紧接其后的 fail-closed 段）。
# --foreground-scripts：让 install 脚本的输出进构建日志（默认被 npm 吞掉，
# 这正是「下载失败却构建成功」长期看不见的原因之一）。
RUN cd sidecar && npm install --no-audit --no-fund --foreground-scripts

# 原生 addon **fail-closed 校验**（2026-09-12 事故沉淀）
# ---------------------------------------------------------------------------
# trtc-electron-sdk 的原生库不在 npm 包里：`install` 脚本会从
# https://web.sdk.qcloud.com/trtc/electron/download/...zip 下载并解压到
# build/Release/。实测该下载会失败**且被脚本静默吞掉**，Docker 构建照样"成功"，
# 于是产出一个「进程能起、但连不上 TRTC」的镜像。后果是云端媒体面从未进过房间：
# 渲染进程 require('trtc-electron-sdk') 抛
#   Cannot find module '../build/Release/trtc_electron_sdk.node'
# 而这条错误在无头容器里既不进 stdout 也不落日志文件，藏了很久。
# 这里做两件事：① 缺失则显式重下（有界重试，仅用于抵消网络抖动）；
#              ② 仍缺失就让**构建失败**——宁可构建不过，也不产出坏镜像。
RUN set -eu; cd sidecar; \
    ADDON=node_modules/trtc-electron-sdk/build/Release/trtc_electron_sdk.node; \
    for attempt in 1 2 3; do \
      if [ -f "$ADDON" ]; then break; fi; \
      echo "[trtc-addon] missing; download attempt ${attempt}/3"; \
      ( cd node_modules/trtc-electron-sdk && npm run download ) || true; \
    done; \
    if [ ! -f "$ADDON" ]; then \
      echo "[trtc-addon] FATAL: 原生 addon 仍缺失（${ADDON}）；拒绝构建 TRTC 不可用的镜像"; \
      exit 1; \
    fi; \
    echo "[trtc-addon] OK: $(wc -c < "$ADDON") bytes"

COPY cloudbridge ./cloudbridge

# 平台只暴露一个端口；rtc_bridge 的 19092/19093 保持容器内 localhost，不对外。
EXPOSE 9200

# 监督进程：子进程退出即整体退出（由平台重启），替代本地 watchdog。
CMD ["python", "cloudbridge/supervisor.py"]
