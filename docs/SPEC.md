# SPEC — 规格即契约（可执行总规格）

> 版本：v1.0（M0 契约化定稿）2026-08-03
> 依据：spec-as-contract.md 规范：自包含 / 点名文件与接口 / 钉版本 / 写 out-of-scope / 内嵌已知坑 / 带端到端验证

---

## 1. 范围（In-Scope）

- Windows 桌面常驻桌宠，本地 MiniCPM-O 4.5 9B 监控 Codex/Trae/Hermes
- V1：截屏监控 + 跑偏/卡住/进展检测 + 四级提醒 + 手机推送 + 基础宠物
- V1.1：全双工语音对话（前置 PoC B3 通过）

## 2. Out-of-Scope（明确不做）

- 不操控被监控 agent（不注入/不键鼠/不读日志内容）
- 不做云端 SaaS / 账号体系 / 多语言 / 宠物养成 / token 成本监控
- 不做 agent 日志解析或 SDK 接入（覆盖不了 Trae）

## 3. 版本锚定（已联网核实）

| 组件 | 版本 | 来源 |
|---|---|---|
| llama.cpp-omni | Comni-Setup-win64.exe（官方 Releases / ModelScope app 内） | github.com/tc-mb/llama.cpp-omni |
| MiniCPM-o 4.5 GGUF | Q4_K_M（~8.3GB 下载，~9GB VRAM） | modelscope.cn/models/OpenBMB/MiniCPM-o-4_5-gguf |
| windows-capture | 2.0.0（PyPI 2026-04-15） | pypi.org/project/windows-capture |
| silero-vad | 6.2.1 | pypi.org/project/silero-vad |
| sherpa-onnx | 1.13.2 | pypi.org/project/sherpa-onnx |
| edge-tts | latest（voice zh-CN-XiaoxiaoNeural） | github.com/rany2/edge-tts |
| Tauri | 2.11.5（cli 2.11.4 / api 2.11.1） | crates.io/tauri |
| Python | 3.11+（venv 于 .workbuddy/binaries/python/envs/default，实际 3.13.12 亦可） | — |
| FastAPI / uvicorn | latest | — |
| XState | 5.x | — |
| Lucide | 1.16.0 / @lucide/icons 1.23.0 | lucide.dev |

## 4. 需新增的文件（全部点名）

### 4.1 后端（backend/）

| 文件 | 职责 |
|---|---|
| `run.py` | uvicorn 启动入口（--port 8000） |
| `app/main.py` | FastAPI 实例 + lifespan（启停模型子进程、会话清理） |
| `app/config.py` | pydantic-settings 加载 config/*.yaml + .env |
| `app/api/routes_status.py` | GET /api/v1/status、/api/v1/status/sessions/{app_id} |
| `app/api/routes_control.py` | POST /api/v1/control、/control/test-push、/config/reload |
| `app/api/routes_ws.py` | WS /ws/pet（状态推送 + 指令下行 + 心跳） |
| `app/core/orchestrator.py` | asyncio 主编排：监控循环/语音会话/提醒调度 |
| `app/core/state.py` | 全局状态快照（会话 + 连续不变帧计数） |
| `app/core/events.py` | 事件总线 |
| `app/capture/window_finder.py` | 进程/窗口定位（psutil + win32） |
| `app/capture/wgc_capture.py` | WGC 捕获封装 |
| `app/capture/dxgi_fallback.py` | DXGI 兜底 |
| `app/capture/session_manager.py` | 捕获会话生命周期 + 授权状态 |
| `app/engine/llama_omni_client.py` | 模型 HTTP 客户端（/health、prefill、decode） |
| `app/engine/vision_analyzer.py` | 截屏→提示词→结构化 JSON（progress/stuck/off_track/摘要） |
| `app/engine/status_detector.py` | 3 帧不变 + 120s 超时判定（纯函数） |
| `app/engine/advice_generator.py` | 优化建议生成（P1） |
| `app/voice/vad_gate.py` | silero-vad 门控（V1.1） |
| `app/voice/duplex_session.py` | 全双工会话 + barge-in（V1.1） |
| `app/voice/stt_fallback.py` | sherpa-onnx 流式 STT（V1.1） |
| `app/voice/tts_fallback.py` | edge-tts（V1.1） |
| `app/push/base.py` | PushService 抽象接口 |
| `app/push/wecom.py` | 企业微信 Provider |
| `app/push/ntfy.py` | ntfy Provider |
| `app/push/manager.py` | 路由 + 重试 + 熔断 + 限频 |
| `app/services/reminder_service.py` | 四级渐进打扰调度 |
| `app/utils/logger.py` | 结构化日志（JSON lines + SLI 打点） |
| `app/utils/metrics.py` | 指标采集 |
| `tests/unit/*` | status_detector / advice / push / config 单测 |
| `tests/integration/*` | capture→engine→push 链路（mock 模型） |
| `tests/fixtures/*` | 样本截图 + 模拟检测 JSON |

### 4.2 桌宠 UI（pet-ui/）

| 文件 | 职责 |
|---|---|
| `src-tauri/tauri.conf.json` | transparent / decorations=false / alwaysOnTop / skipTaskbar |
| `src-tauri/src/main.rs` | 应用入口 |
| `src-tauri/src/tray.rs` | 系统托盘 |
| `src-tauri/src/window.rs` | 点击穿透切换 + 吸附 |
| `src/components/Pet.tsx` | 宠物光球（监控/提醒双形态） |
| `src/components/VoiceOrb.tsx` | 语音六态光球（V1.1） |
| `src/components/MonitorPanel.tsx` | 监控面板 |
| `src/components/ReminderToast.tsx` | 提醒气泡 |
| `src/components/Settings.tsx` | 设置 |
| `src/state/petMachine.ts` | XState 六态机 |
| `src/state/wsClient.ts` | WS 客户端（心跳/重连） |
| `src/styles/tokens.css` | Design Token（无硬编码色值） |

### 4.3 配置（config/）

| 文件 | 职责 |
|---|---|
| `monitors.yaml` | 监控目标（进程名/窗口标题/轮询间隔） |
| `detection.yaml` | 帧数阈值/超时/提示词模板路径 |
| `push.yaml` | Provider 路由与限频 |
| `pet.json` | 皮肤/尺寸/透明度/动效参数 |

### 4.4 脚本（scripts/）

`setup_env.ps1` / `download_model.ps1` / `dev.ps1` / `poc_001_model.ps1` / `poc_002_capture.py` / `poc_003_voice.py` / `e2e_verify.py`

## 5. 接口签名（核心，与 openapi.yaml 一致）

```python
# push/base.py
class PushService(Protocol):
    def push(self, text: str, image: Path | None = None, title: str | None = None) -> PushResult: ...

# engine/status_detector.py（纯函数）
def detect_status(frames: Sequence[VisionResult], cfg: DetectionConfig) -> Detection: ...

# engine/vision_analyzer.py
def analyze(screenshot: Path, prompt_template: Path) -> VisionResult: ...  # JSON: progress/stuck/off_track/summary

# core/events.py
class EventBus:  # emit(event) / subscribe(handler)
```

## 6. 已知坑（写代码时注意）

1. Trae 是 Chromium GPU 窗口 → PrintWindow 黑屏，必须 WGC
2. WGC 首次需用户对每个窗口授权（系统选择器）
3. 窗口最小化 WGC 可能空帧 → DXGI 兜底
4. 12G 显存：禁止双模型实例；对话期监控降频
5. 企业微信 webhook 限频 ~20/min → 提醒节流
6. 模型子进程崩溃需自动拉起 + UI 上报

## 7. 端到端验证步骤（每版本）

```powershell
# V1
scripts\dev.ps1                    # 一键启动（模型 server → uvicorn → tauri dev）
python scripts\e2e_verify.py       # 启动→授权→观察 Codex 变化→构造卡住→提醒+推送→恢复
# V1.1
# 对宠物说话 → Listening → Thinking → Speaking ≤2.5s；打断立即响应；兜底链可手动触发
```

## 8. 验收门（与 PRD §7 一致）

- V1：四流测试全绿 + 回归率 0 + e2e 通过
- V1.1：六态机正确 + 全双工 ≤1.5s + 打断 <500ms + 兜底可用
