# 架构设计 — 贾克斯模式：AI 智能体监控中枢

> 版本：v1.0（M0 契约化定稿）
> 日期：2026-08-03
> 状态：已确认

---

## 1. 进程拓扑

```
┌─────────────────────────────────────────────────────────────────┐
│                        本机（RTX 3060 12G）                      │
│                                                                 │
│  ┌─────────────────┐     HTTP      ┌─────────────────────────┐  │
│  │ llama.cpp-omni  │  :19080       │ Python 3.11+ FastAPI    │  │
│  │ (独立子进程)      │◄─────────────►│ 单进程 + asyncio :8000   │  │
│  │ MiniCPM-o 4.5   │               │  - 截图调度器            │  │
│  │ Q4_K_M ~9GB VRAM│               │  - 推理队列（时分复用）    │  │
│  └─────────────────┘               │  - 语音会话              │  │
│                                    │  - 推送服务              │  │
│  ┌─────────────────┐   WebSocket   └───────────┬─────────────┘  │
│  │ Tauri v2 桌宠 UI │◄───────────────► /ws/pet │               │
│  │ (React + Lottie) │   状态推送+指令下行        │               │
│  └─────────────────┘                          │               │
│                                               ▼               │
│  ┌─────────────────┐        ┌─────────────────────────────┐   │
│  │ 被监控窗口        │ WGC    │ 企业微信 webhook / ntfy      │   │
│  │ Codex/Trae/Hermes│────────►│ (HTTPS 推送到手机)           │   │
│  └─────────────────┘ 截屏    └─────────────────────────────┘   │
└─────────────────────────────────────────────────────────────────┘
```

**端口约定**：
- llama.cpp-omni 模型服务：`127.0.0.1:19080`（/health、/v1/stream/prefill、/v1/stream/decode）
- FastAPI 后端：`127.0.0.1:8000`
- WebSocket：`ws://127.0.0.1:8000/ws/pet`

## 2. 模块边界与依赖方向

```
backend/app/
├── main.py              # FastAPI 实例 + 生命周期（启停模型子进程、会话清理）
├── config.py            # pydantic-settings：config/*.yaml + .env 统一加载
├── api/                 # HTTP/WS 层（只做协议转换，不含业务逻辑）
├── core/                # 编排：orchestrator（主编排）/ state（快照）/ events（事件总线）
├── capture/             # 窗口定位 + WGC 捕获 + DXGI 兜底 + 会话管理
├── engine/              # 模型客户端 + 视觉分析 + 状态判定（纯函数）+ 建议生成
├── voice/               # VAD 门控 + 全双工会话 + STT/TTS 兜底
├── push/                # PushService 插件（base/wecom/ntfy/manager）
├── services/            # reminder_service（四级渐进打扰）
└── utils/               # 结构化日志 + 指标
```

**依赖方向（禁止反向）**：
`api → services → core → capture/engine/voice/push`
`core → events（解耦提醒与检测）`

## 3. 核心数据流

### 3.1 监控判定流（V1 核心）

```
WGC 截屏(5-8s/帧) → 降采样(宽≤1280) → PNG 临时文件
  → llama-omni /v1/stream/prefill（视觉编码）
  → /v1/stream/decode（结构化 JSON 输出）
  → vision_analyzer 解析 JSON（progress/stuck/off_track/摘要）
  → status_detector 状态机（3 帧不变 + 120s 超时）
  → 触发事件 → events 总线 → reminder_service（四级打扰）→ 桌宠 UI / 手机推送
```

### 3.2 语音对话流（V1.1）

```
麦克风(16kHz) → silero-vad 门控 → 语音帧 → llama-omni 全双工会话
  → 模型原生响应（音频直出）→ 音箱播放
  → barge-in：用户开口 → 500ms 内打断 → 重新进入 Listening
  → 对话期监控降频 10-15s/帧（时分复用）
```

## 4. 显存分配策略（12G 硬约束）

- 模型权重 Q4_K_M：约 8.3GB + KV cache 1-2GB ≈ **9-10GB / 12G**，余量 2-3GB
- **单进程单模型时分复用**（asyncio 串行化 LLM 调用）；**禁止双实例**（视觉 6GB + 全 omni 9GB = 15GB > 12G）
- 对话进行中：监控降频至 10-15s/帧 + 提示词压缩
- 启动前检测显存：若 < 2GB 余量 → 降级纯视觉模式或提示用户关闭其他 CUDA 程序

## 5. 关键设计决策（摘要，详见 docs/decisions/ADR-*）

| ADR | 决策 | 理由 |
|---|---|---|
| ADR-001 | 推理引擎 llama.cpp-omni + MiniCPM-o-4_5-gguf Q4_K_M | 全模态原生支持（视觉+音频+TTS+全双工），12G 可行 |
| ADR-002 | 窗口截屏 Windows.Graphics.Capture（windows-capture 2.0.0） | 唯一能捕获 Chromium GPU 窗口（Trae）的方案；DXGI 兜底 |
| ADR-003 | 语音模型原生全双工 + silero-vad 门控 | 官方 ~800ms 端到端；sherpa-onnx + edge-tts 降级 |
| ADR-004 | 桌宠 UI Tauri v2.11.5 + React + Lottie | 透明窗口+托盘+常驻 20-60MB |
| ADR-005 | 推送 PushService 插件层：企业微信主选 + ntfy 备选 | 国内可达、免费；可插拔可熔断 |
| ADR-006 | 后台 Python 3.11+ FastAPI + asyncio 单进程 | 截屏/语音/ML 生态全 Python，直接绑定 |
| ADR-007 | 宠物视觉有机光球 + 语义色 + 四级渐进打扰 | 信任成本低、渲染开销低、可迭代皮肤 |
| ADR-008 | 监控策略 5-8s 轮询 + 3 帧不变 + 120s 超时 | 防误判双条件；阈值参数化可调 |

## 6. 可观测性

- 结构化日志：JSON lines（utils/logger.py），含 SLI 打点
- 指标：帧延迟、推理耗时（prefill/decode P50/P95）、误判计数、推送成功率
- 判定留痕：每次检测结果落盘供回归集回放

## 7. 安全

- 只读截屏 + 进程状态，不注入、不发键鼠事件（安全边界）
- webhook/密钥走 .env，不入库；.gitignore 排除
- WGC 首次授权由系统选择器完成（透明可审计）

## 8. 已确认的不可行/约束

1. WGC 首次运行需用户为每个目标窗口授权一次（系统选择器）
2. 被监控窗口最小化时 WGC 可能空帧 → DXGI 兜底 / 仅状态监控
3. 12G 显存余量小：与大型 CUDA 程序并行会 OOM → 启动前显存检测
4. 企业微信 webhook 限频约 20 条/分钟 → 提醒节流
5. 语音需 Windows 隐私设置授予麦克风权限
