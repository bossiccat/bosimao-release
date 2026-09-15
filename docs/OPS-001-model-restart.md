# OPS-001: 模型服务宕机恢复记录（llama-server :19080）
>
> ⚠️ **本文档描述的是已退役的链路（2026-08-03/04 形态），不要照它操作。**
> 它记录的「手机 App → 公网中继(jax-relay) → PC relay_client → 本地 voice 网关(127.0.0.1:8000)」
> 及本地服务管理/watchdog/计划任务那一整套，已于 **2026-09-11 随本地运行态脚本一并退役**
> （见 `docs/retirements/local-runtime-scripts-2026-09-11.md`：产品必须**完全走云端**）。
> 现行架构与状态请读 `docs/STATUS.md` 第 8 节与
> `docs/release/2026-09-15-commercial-release-handover.md`。**保留本节仅为历史。**


> 状态：**已恢复（2026-08-03 卜宕机实测）** | 触发：后端联调连接拒绝

## 1. 宕机确认

| 检查项 | 结果 |
|---|---|
| netstat :19080 | 无 LISTENING（端口空） |
| curl /health | 连接拒绝（exit 7） |
| 进程 llama-server.exe | 不存在（仅无关 ollama 进程） |

## 2. 引擎定位

llama-server.exe 为 **llama.cpp-omni 编译版**（Comni 桌面版内置，支持 omni_init 动态加载 vision mmproj）：

```
C:\Users\Administrator\AppData\Local\Comni\_internal\resources\build\bin\Release\llama-server.exe
```

- 配套 CUDA 库同目录（ggml-cuda.dll / cublas64_12.dll / omni.dll 等）
- vision mmproj：D:\models\MiniCPM-o-4_5-gguf\vision\MiniCPM-o-4_5-vision-F16.gguf
- 模型：D:\models\MiniCPM-o-4_5-gguf\MiniCPM-o-4_5-Q4_K_M.gguf
- 注意：Comni 桌面版通过 worker.py 以 kill-on-close Job Object 管理引擎，Comni 退出会连带杀掉 llama-server → 这是本次宕机根因之一。本次**脱离 Comni 直接启动**，进程独立常驻。

## 3. 启动命令（已验证）

```powershell
C:\Users\Administrator\AppData\Local\Comni\_internal\resources\build\bin\Release\llama-server.exe `
  --host 127.0.0.1 --port 19080 `
  --model D:\models\MiniCPM-o-4_5-gguf\MiniCPM-o-4_5-Q4_K_M.gguf `
  -ngl 99 --ctx-size 4096
```

参数依据：B1 结论 **--ctx-size 4096**（8192 爆 12G 显存）；-ngl 99 全层 GPU。`.env` 已锁定 MODEL_CTX_SIZE=4096 / MODEL_NGL=99 / MODEL_SERVER_PORT=19080。

## 4. 恢复证据

| 指标 | 值 |
|---|---|
| PID | 34876 |
| /health | `{"status":"ok","engine":"comni"}`（HTTP 200） |
| 就绪耗时 | ~20s（冷启动，日志显示模型加载完成后 listening） |
| 显存占用 | 6708MB（加载后）/ 8854MB（视觉推理峰值）→ 空闲余 ~3.3GB |
| 视觉链路 | omni_init 200 → update_session_config 200 → prefill(img 6616ms + text 3ms) → decode SSE 200（CT=text/event-stream）|
| 首 token | 473ms |
| decode | 936ms |
| 端到端 | 12240ms（含 omni_init 冷启动）|
| SSE 输出 | `{"state":"stuck","summary":"用户询问如何使用特定编程工具实现特定功能，AI未直接提供解决方案"}` JSON 解析成功 |

## 5. 常驻保障

- 本次启动方式：Bash `&` 后台 + 日志重定向，已跨多次独立会话存活（非随会话被杀）。
- 固化脚本：`scripts/start-model.ps1`（Start-Process 独立窗口 + 5min health 轮询 + 幂等防重复启动）。
- 推荐用户路径：`powershell -ExecutionPolicy Bypass -File scripts/start-model.ps1`；
  如需开机自启 → 任务计划程序注册该脚本（当前安全策略禁 schtasks，需用户在安全中心放行）。
- 开发一键：`scripts/dev.ps1` 会提示手动启动命令（默认 8192，请以 .env 的 4096 为准）。
- 回滚：停止进程 `Stop-Process -Id 34876 -Force`（或 taskkill /PID 34876 /F），重新执行 start-model.ps1 即恢复。

## 6. 遗留

- 后端 llama_omni_client 需按 B1 结论改为 SSE 解析（decode 返回 text/event-stream，非纯 JSON），见 docs/specs/backend-llama-client-spec.md。
