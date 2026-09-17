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
        libgl1 \
        libegl1 \
        libudev1 \
        libxcb-xfixes0 \
        libxcb-shm0 \
        pulseaudio \
        pulseaudio-utils \
        pulseaudio-module-raop \
    && rm -rf /var/lib/apt/lists/*

# 发布阻断：module-raop-sink.so 必须在镜像里（**早失败**，不等后面的自证）
# ---------------------------------------------------------------------------
# raop-sink 是唯一一个**由模块自己建出端口**的合成 sink（raop-sink.c 里
# `pa_sink_new_data_add_port(..., "network-output", ...)`），而端口正是 TRTC 接受设备的
# 判据（SDK 的列举路径在 `cmp qword [rsi+0x188], 0` 处跳过端口指针为空的设备）。
# 承载它的二进制不在 `pulseaudio` 包里，而在**独立包** `pulseaudio-module-raop`
# （Debian 把 jack/bluetooth/raop/zeroconf/lirc/gsettings 这些模块都拆成了单独的包）。
# 所以"装了 pulseaudio"**不等于**"有 module-raop-sink" —— 这里必须显式校验。
# ⚠️ 保留 `/usr/lib/pulse-*/modules/` 的**通配**：模块目录名带 PA 版本
# （bookworm = pulse-16.1+dfsg1，trixie = pulse-17.0+dfsg1，而 `python:3.11-slim`
# 目前解析到 trixie），写死版本号会在换 suite 时静默失配。
# ⚠️ 校验**不得**放进管道：`test -f ... | ...` / `ls ... | grep -q ...` 的退出码取自
# 管道最后一个命令，文件不存在也会"成功"，那是假绿。这里用 `if ! ls <glob>`：
# 通配不匹配时 sh 把字面量交给 ls，ls 报错 → 取反成立 → 打印诊断并 exit 1。
RUN set -eu; \
    if ! ls /usr/lib/pulse-*/modules/module-raop-sink.so > /dev/null 2>&1; then \
      echo "[audio] FATAL: module-raop-sink.so 不在镜像里 —— pulseaudio-module-raop 没装上，或模块目录名变了；拒绝构建"; \
      echo "[audio] 实际模块目录内容（raop 相关）："; \
      ls /usr/lib/pulse-*/modules/ | grep raop || echo "  （一个 raop 模块都没有）"; \
      exit 1; \
    fi; \
    echo "[audio] module-raop-sink.so OK: $(ls /usr/lib/pulse-*/modules/module-raop-sink.so)"

# 音频子系统（PulseAudio）——2026-09-16 线上事故沉淀，发布阻断级
# ---------------------------------------------------------------------------
# 镜像原本**没有任何音频子系统**。TRTC 的 Linux 原生层进房时直接打：
#   pulse_audio_context.cc:69   pulse server connect failed, please install pulseaudio
#   pulse_audio_context.cc:1327 current context is not ready  GetDevices wait
# 之后 EnterRoom 再无一行原生日志（26 分钟零输出），桥侧权威上行计数
# `up rms=%.0f frames=%d`（backend/rtc_bridge/session.py:308）一条都没出现
# ⇒ 手机与云端媒体面从来没共处一室（手机侧 user size 恒为 1）。
#
# 装三个包，各有明确用途：
#   · pulseaudio             —— 守护进程本体 + 标准模块（module-native-protocol-unix /
#                               module-null-sink / module-virtual-source），运行期必需；
#   · pulseaudio-utils       —— `pactl`，**只在构建期**用于清点 sink/source（见下面的自证 RUN）；
#   · pulseaudio-module-raop —— `module-raop-sink.so`（上面已单独早期校验），**只能**由这个包提供。
# 无头容器没有声卡，设备必须自己造：null-sink 造一个 null sink（同时提供 monitor source），
# 再用 module-virtual-source 派生"虚拟麦克风"；而 raop sink 是**唯一**带端口的合成 sink。
# 具体 argv 与环境变量在 cloudbridge/audio_env.py 里（运行期与构建期共用同一份）。

# 上面五个图形/设备库不是「顺手装的」：它们来自 TRTC Linux 原生库自己的 DT_NEEDED。
# 解析 build/Release 的三个 .so 的 ELF 动态段得到（2026-09-16 离线核对，见
# cloudbridge/trtc-electron-sdk-linux-versions.json 的 verification）：
#   libliteavsdk.so  需要 libGL.so.1 / libEGL.so.1 / libXrandr.so.2 / libXfixes.so.3 /
#                    libxcb.so.1 / libxcb-shm.so.0 / libxcb-xfixes.so.0 / libudev.so.1 /
#                    libX11.so.6 / libX11-xcb.so.1 / libdrm 等
#   libtxffmpeg.so   需要 libz.so.1 等
# DT_NEEDED 是**加载期**硬依赖：缺一个，dlopen 直接失败，sidecar 又在模块加载期死掉
# （与本次线上事故同一个表现）。libXrandr/libXfixes/libX11/libxcb 已由 libgtk-3-0 /
# libx11-xcb1 链带上；libGL/libEGL/libudev/libxcb-xfixes/libxcb-shm 未必，
# 这里显式声明，避免依赖传递闭包的偶然性。

# 动态链接器搜索路径：TRTC 的 Linux 原生库**没有 DT_RPATH/DT_RUNPATH**（实测三个 .so 与
# .node 的四段全为空），而 trtc_electron_sdk.node 的 DT_NEEDED 里是裸名
# `libliteavsdk.so` / `libtxffmpeg.so` / `libuser_sig_gen.so`——它们就在 .node 同目录。
# glibc 解析裸名时不搜索「加载对象自身所在目录」，所以不显式给路径就会
# dlopen 失败（又是模块加载期即死）。这里只加这一个目录，内容是 4 个 SDK 专属 .so，无冲突。
ENV LD_LIBRARY_PATH=/srv/sidecar/node_modules/trtc-electron-sdk/build/Release

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
#
# TRTC SDK 版本 pin（2026-09-16 事故沉淀，发布阻断级）
# ---------------------------------------------------------------------------
# **容器里的 sidecar 必须用 12.5.705-beta.0，不能跟桌面端一样用 13.4.802-beta.3。**
# 桌面端（Windows）继续由 sidecar/package.json pin 13.4.802-beta.3 —— 那是 **win64**，产物存在，
# 所以**不要**为了"统一版本"去改 sidecar/package.json。
#
# 选版本要过**两道**筛，缺一不可（2026-09-16 实测；URL 模板出自 sdk 自身
# scripts/download.js:61，平台标签出自 scripts/constant.js）：
#   筛 1「linux-x64 的 zip 存在」：对 111 个 12.x/13.x 版本逐个 HEAD 官方 CDN，
#        只有 24 个版本有 linux-x64 产物；**任何 13.x 都没有**——实测
#        .../13.4.802-beta.3/trtc-electron-sdk-linux-x64-13.4.802-beta.3.zip → HTTP 404，
#        这正是 DeployId 014 构建失败的确切原因。（win64 则从 12.2.703 到 13.3.801 全部 200。）
#   筛 2「zip 里真有 Node 原生绑定 `build/Release/trtc_electron_sdk.node`」：
#        **光有 zip 远远不够。** 逐版本用 HTTP Range 读 zip 中央目录列出真实产物后发现，
#        那 24 个里只有 **12 个**含该 .node；断点精确落在 12.5.705-beta.0 与 12.6.706-beta.0 之间：
#          · 带 .node：12.2.115-beta.{0,1,2,3,5,6,12} / 12.3.705-beta.{2,3} /
#                      12.4.705-alpha.0 / 12.5.705-alpha.1 / **12.5.705-beta.0**
#          · 不带：    12.6.706-beta.0 起，含 **12.7.706**（8.28 MB 却只有 libliteavsdk.so /
#                      libtxffmpeg.so / libuser_sig_gen.so 三个 .so）与 12.8.707-alpha.*
#        而 `liteav/trtc.js:41` 正是 `require('../build/Release/trtc_electron_sdk.node')`，
#        所以 12.7.706 在 Linux 上**必然在模块加载期抛 Cannot find module**——
#        与本次线上事故同一个死法，只是触发点从"下载 404"变成"包内缺件"。
#        两代版本的 scripts/download.js 做 diff 后**完全一致**（Linux 分支同样是先平铺拷
#        build/Release、再拷一份到 Release/x64），所以不是脚本变了，是 CDN 上 12.6+ 的包本身缺件。
# 结论：容器只能停在 **12.5.705-beta.0**（"带 .node"集合里最新的一个；该集合中不存在非预发布版本）。
#   白名单（含每版的产物清单与是否带 .node）见 cloudbridge/trtc-electron-sdk-linux-versions.json，
#   由 backend/tests/contract/test_trtc_linux_sdk_version_pin_contract.py 守护。
#
# 为什么"主 install + 显式装指定版本"两步：package.json 是桌面端的 pin（禁改），
# 主 install 会先按它装 13.4.802-beta.3（其 linux addon 下载会 404 并被脚本静默吞掉，
# 下面的 fail-closed 校验会拦下这种状态）；随后用 --no-save 显式把 JS 包换成
# linux 真的可用的版本，并触发该版本自己的 `install`→`download`，
# 使 **node_modules 里的 JS 包与原生 addon 同版本成对**。
ARG TRTC_ELECTRON_SDK_VERSION=12.5.705-beta.0

RUN cd sidecar && npm install --no-audit --no-fund --foreground-scripts \
    && npm install --no-save --no-audit --no-fund --foreground-scripts \
         "trtc-electron-sdk@${TRTC_ELECTRON_SDK_VERSION}"

# 原生 addon **fail-closed 校验**（2026-09-12 事故沉淀）
# ---------------------------------------------------------------------------
# trtc-electron-sdk 的原生库不在 npm 包里：`install` 脚本会从
# https://web.sdk.qcloud.com/trtc/electron/download/...zip 下载并解压到
# build/Release/。实测该下载会失败**且被脚本静默吞掉**，Docker 构建照样"成功"，
# 于是产出一个「进程能起、但连不上 TRTC」的镜像。后果是云端媒体面从未进过房间：
# 渲染进程 require('trtc-electron-sdk') 抛
#   Cannot find module '../build/Release/trtc_electron_sdk.node'
# 而这条错误在无头容器里既不进 stdout 也不落日志文件，藏了很久。
# 这里做三件事（**全部 fail-closed，任何一条不满足即 exit 1**）：
#   ① 缺失则显式重下（有界重试，仅用于抵消网络抖动）；
#   ② 仍缺失、或存在但**为 0 字节**，都让**构建失败**——宁可构建不过，也不产出坏镜像；
#   ③ 断言 node_modules 里 JS 包的 version **等于** $TRTC_ELECTRON_SDK_VERSION。
#      ①② 同时也是上面"筛 2"的构建期执行者：CDN 上 zip 存在≠包内有 .node，
#      这条校验是唯一能在构建期抓住"包内缺件"的关卡（DeployId 014 那版就是在这里会失败）。
#      addon 的下载 URL 由该包自身 package.json 的 version 拼出（scripts/download.js），
#      所以"JS 版本一致"是"addon 版本一致"的可验证代理；这条专门防 2026-09-16 那类
#      「JS 包被换成 13.x、addon 却是 12.x（或干脆没有）」的漂移。
#      同时把字节数与 sha256 打进构建日志，便于与线上产物逐次对账。
RUN set -eu; cd sidecar; \
    ADDON=node_modules/trtc-electron-sdk/build/Release/trtc_electron_sdk.node; \
    EXPECTED="${TRTC_ELECTRON_SDK_VERSION}"; \
    for attempt in 1 2 3; do \
      if [ -s "$ADDON" ]; then break; fi; \
      echo "[trtc-addon] missing or empty; download attempt ${attempt}/3"; \
      ( cd node_modules/trtc-electron-sdk && npm run download ) || true; \
    done; \
    if [ ! -s "$ADDON" ]; then \
      echo "[trtc-addon] FATAL: 原生 addon 仍缺失或为 0 字节（${ADDON}）；拒绝构建 TRTC 不可用的镜像"; \
      exit 1; \
    fi; \
    ADDON_BYTES="$(wc -c < "$ADDON")"; \
    ADDON_SHA="$(sha256sum "$ADDON" | cut -d' ' -f1)"; \
    echo "[trtc-addon] OK: bytes=${ADDON_BYTES} sha256=${ADDON_SHA} path=${ADDON}"; \
    INSTALLED="$(node -p 'require("./node_modules/trtc-electron-sdk/package.json").version')"; \
    echo "[trtc-addon] JS package version=${INSTALLED} expected=${EXPECTED}"; \
    if [ "$INSTALLED" != "$EXPECTED" ]; then \
      echo "[trtc-addon] FATAL: node_modules 里 JS 包版本（${INSTALLED}）!= ARG TRTC_ELECTRON_SDK_VERSION（${EXPECTED}）；JS 与原生 addon 版本漂移，拒绝构建"; \
      exit 1; \
    fi; \
    echo "[trtc-addon] fail-closed 校验通过（存在 + 非零字节 + 版本一致）"; \
    LDD_OUT="$(ldd "$ADDON" 2>&1 || true)"; \
    echo "$LDD_OUT" | sed 's/^/[trtc-addon] ldd| /'; \
    if echo "$LDD_OUT" | grep -q "=> not found"; then \
      echo "[trtc-addon] FATAL: 原生 addon 的动态依赖未解析（见上面 ldd 的 '=> not found'）；拒绝构建「dlopen 即失败」的镜像"; \
      exit 1; \
    fi; \
    if ! echo "$LDD_OUT" | grep -q "=> "; then \
      echo "[trtc-addon] FATAL: ldd 未能解析该 .node（输出里没有任何已解析依赖，可能不是 Linux x86-64 的 ELF）；拒绝构建"; \
      exit 1; \
    fi; \
    echo "[trtc-addon] ldd OK: $(echo "$LDD_OUT" | wc -l) 个条目全部解析（含同目录 liteav 系列 .so）"

COPY cloudbridge ./cloudbridge

# 音频子系统构建期自证（fail-closed）
# ---------------------------------------------------------------------------
# 「本机的开发环境没有 docker」这类话不能当作"参数对不对只能等线上才知道"的借口：
# 把运行期那一份 argv（cloudbridge/audio_env.py:build_plan，运行期与构建期**同一个**
# 函数）在这里对着**真实 Debian 镜像**跑一次，用 pactl 清点 sink/source 是否真的存在。
# 起不来、套接字不出现、设备清点为空 —— 任一条都让**构建失败**，绝不产出
# 「进程起得来但音频层是哑的」镜像（那正是 DeployId 015 的状态：进房永远完不成，
# 且真实 sidecar 的渲染进程日志当时也读不到，藏了 26 分钟）。
# 用临时 HOME，避免把 pulse 的 cookie/配置写进镜像层。
RUN set -eu; \
    export HOME=/tmp/pulse-selftest-home; \
    mkdir -p "$HOME"; \
    python cloudbridge/audio_env.py --self-test --runtime-dir /tmp/pulse-selftest; \
    rm -rf "$HOME" /tmp/pulse-selftest

# 平台只暴露一个端口；rtc_bridge 的 19092/19093 保持容器内 localhost，不对外。
EXPOSE 9200

# 监督进程：子进程退出即整体退出（由平台重启），替代本地 watchdog。
CMD ["python", "cloudbridge/supervisor.py"]
