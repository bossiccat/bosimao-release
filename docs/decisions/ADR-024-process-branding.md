# ADR-024: 进程品牌化 —— jax-backend / jax-model / jax-bridge

## Status: Accepted（D1/D2 已落地 2026-08-13；D3 仅方案，待 M2-D 后续实施）

> 阶段 D「架构减疑」目标：消灭客户任务管理器里的「裸 python.exe」常驻 + llama-server.exe
> 第三方进程名，进程家族品牌化为 `jax-*`。签名/证书不在此 ADR（阶段 G，等营业执照）。
>
> 编号说明：`docs/decisions/` 已到 ADR-023，本决策顺延为 **ADR-024**。

## Background

商业化红线（`docs/master-roadmap.md` §2.2）：客户任务管理器看到「波斯猫」家族、无裸
python.exe、杀毒零误报。当前 Windows 本机进程链（`scripts/jax-services.ps1`）为：

| 进程 | 现状 | 问题 |
|------|------|------|
| backend (:8000) | `pythonw -m uvicorn app.main:app` | 裸 python.exe |
| model (:19080) | `llama-server.exe`（Comni 目录） | 第三方进程名，杀毒误报面 |
| relay_client | `python -m backend.relay.relay_client` | 裸 python.exe |
| rtc_bridge (:19092/:19093) | `python -m rtc_bridge.main` | 裸 python.exe |
| relay_server (:19090) | 部署在 CloudBase CloudRun（云端） | 不在客户本机，不产生裸 python |

## Decision

### D1. backend → jax-backend.exe（已落地）

PyInstaller onefile 打包 FastAPI，`scripts/jax-services.ps1` `Start-BackendService` 与
`scripts/start-all.ps1` 第 2 步改调 `jax-backend.exe --host 127.0.0.1 --port 8000`。
spec/入口见 `backend/packaging/`（jax-backend.spec + jax_backend_entry.py），冻结态路径
解析见 `backend/app/_frozen_paths.py`。PE 子系统=2 WINDOWS_GUI（无命令窗）。

### D2. llama-server → jax-model.exe（已落地）

把 Comni 安装目录里的 `llama-server.exe` 复制为同目录 `jax-model.exe`（3.2MB），
`scripts/start-model.ps1` 与 `Start-ModelService` 的 `$ServerBin` 指向 `jax-model.exe`。
**约束**：jax-model.exe 依赖同目录 10 个 DLL（cublas64/cublasLt64/cudart64/ggml-*/llama/
mtmd/omni），必须与这些 DLL 保持同目录，安装打包时须整目录搬运。签名留阶段 G。

### D3. relay_client + rtc_bridge → jax-bridge.exe（方案，本轮不实施）

**结论：合并进单一 jax-bridge.exe，但不并入 backend。**

- 不并入 backend：backend 是 HTTP 服务（FastAPI），relay/rtc 是长连接 WS 桥接，各自有
  独立重连/退避/假死检测（relay_client 的 SUSPECT_DEAD 退避、watchdog 的 relay 假死判定
  + 防风暴）。合并会耦合 HTTP 服务与桥接的崩溃/重启生命周期，扩大单点故障面。
- relay_client 与 rtc_bridge **同构**：都是纯 asyncio WS 转发（relay_client=公网中继 wss
  ↔ 本地 voice 网关 ws；rtc_bridge=sidecar WS ↔ MiniCPM-o APM wss），无独立 HTTP 服务
  （仅 rtc_bridge 带一个 health server），天然可共享同一 event loop + 统一健康检查。
- relay_server：**不合并**，云端 CloudRun（`deploy/relay/Dockerfile`），不在本机进程链。

**合并设计（待实施）：**

1. 新入口 `backend/bridge/main.py`（或 packaging entry）：`asyncio.gather(relay_client_main,
   rtc_bridge_main)`，共享 event loop。
2. 统一健康检查：复用 rtc_bridge `HealthServer`（:19093），新增 `relay` 存活字段
   （`relay_connected` / `relay_paired`）。
3. 打包：复用 `jax-backend.spec` 模板，换 entry + `name="jax-bridge"`。
4. 脚本：`jax-services.ps1` 的 `Start-RelayService` + `Start-RtcBridgeService` 合并为
   `Start-BridgeService`；`Stop-ServiceByName "bridge"` 统一停；watchdog 防风暴把 bridge
   视为一个单元。

**为什么本轮不实施**：合并是重构（共享 loop + 统一 health + 单进程监督），风险高，且当前
relay_client / rtc_bridge 独立运行稳定。标「待 M2-D 后续」。

### D4. 终态进程拓扑

```
客户 Windows 本机：
├─ jax-backend.exe   :8000   （FastAPI）
├─ jax-model.exe     :19080  （llama-omni，含同目录 DLL）
└─ jax-bridge.exe    （relay_client + rtc_bridge；:19092/:19093 + 公网中继桥）  ← 待实施
云端（CloudRun）：
└─ relay_server      （jax-relay，非本机，不涉及）
```

任务管理器只看到 `jax-*` 家族，无裸 python.exe / llama-server.exe。

## Consequences

正面：消灭裸 python.exe 与第三方 llama-server.exe 进程名，杀毒误报面收敛，商业化信任
基础就位（签名后彻底闭环）。

负面：
- jax-model.exe 依赖 Comni 目录的 CUDA/ggml/omni DLL，安装必须整目录搬运（~725MB），
  卸载清理需覆盖该目录。
- D3 合并后 relay/rtc 的崩溃恢复从「独立进程各管各」变为「bridge 单进程一起重启」，
  需重验 watchdog 防风暴与假死判定语义。

## Migration and rollback

- D1/D2 回滚：脚本 `$BackendExe` / `$ServerBin` 改回 `.venv pythonw -m uvicorn` /
  `llama-server.exe` 即可（jax-backend.exe / jax-model.exe 是新增文件，不影响旧路径）。
- D3 实施时：先落 `backend/bridge/main.py` + `Start-BridgeService`，灰度跑通再删旧的
  relay/rtc 两套启动分支；回滚 = 切回两个独立进程分支。

## Explicitly not doing

- 不碰代码签名（阶段 G，等营业执照）。
- 本轮不实施 D3 合并（重构风险高，独立进程已稳定）。
- 不把 relay_server 合并进本机进程（云端部署，无意义）。

## Related ADRs

ADR-017（独立进程监督模式）、ADR-012/013（RTC 传输路径）、ADR-020（TLS 回环承载）、
ADR-022（owner credential 首启 provision）。
