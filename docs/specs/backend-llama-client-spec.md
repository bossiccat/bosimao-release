# 后端规格 — llama.cpp-omni 客户端 SSE 改造（P0 最致命）

> 版本：v1.0（M-1 修复基线）
> 日期：2026-08-03
> 状态：已确认 · 供后端 M-1 照做（参数细节待 PoC B1 回填，见 §7）
> 依据：docs/decisions/ADR-001-model-engine.md、docs/poc/POC-001-model-vision.md、backend/app/engine/llama_omni_client.py、backend/app/engine/vision_analyzer.py、backend/app/core/orchestrator.py
> 关联缺陷：**现 `LlamaOmniClient.vision_analyze/chat` 把 `decode` 当普通 JSON 读 `resp.text`——llama.cpp-omni `stream=true` 实际返回 SSE 流（`data:{"content":...}` 逐块 + `data:[DONE]` 终止），当前实现必然拿到原始 SSE 文本、JSON 判定必失败。** 另缺 `omni_init` 会话初始化，prefill 缺 `audio_path_prefix` 参数。

---

## 1. 官方调用序列（唯一正确形态）

llama.cpp-omni（MiniCPM-o 4.5，独立子进程 `127.0.0.1:19080`）一轮推理的正确时序：

```
① POST /v1/stream/init          omni_init：建立会话，返回会话句柄/上下文
② POST /v1/stream/prefill       {img_path_prefix, audio_path_prefix, cnt}
                                 视觉：img_path_prefix=截图路径, audio_path_prefix=null, cnt=1
                                 语音：audio_path_prefix=音频路径, cnt=帧数（V1.1）
③ POST /v1/stream/decode        {stream: true, ...}
                                 响应为 SSE：data: {"content":"..."} 逐 token 块
                                             data: [DONE]          流结束
```

> **参数占位（{{POC-B1}}）**：`init` 请求体字段名与返回会话字段、`prefill` 是否必须携带 `audio_path_prefix`（空值传法）、`decode` 的 `max_tokens`/`prompt` 承载方式（body JSON 还是 SSE 发送）——**均以 PoC B1 实测为准**。本 spec 只定接口与解析器结构，具体字段形态留占位待回填。回填时同步修订本文 §3/§4 与 `llama_omni_client.py`。

---

## 2. 目标接口（替换现 LlamaOmniClient 内部实现，对外签名兼容）

`VisionAnalyzer.analyze()` / `orchestrator` 不感知改动；`vision_analyze(image, prompt, max_tokens)` 与 `chat(prompt, max_tokens)` 签名**保持不变**，内部改为 SSE 消费。

```python
class LlamaOmniClient:
    # —— 会话生命周期（新增）——
    async def init_session(self) -> SessionRef: ...          # ① omni_init
    async def prefill(self, img_path: Path | None, audio_path: Path | None, cnt: int) -> None: ...  # ②

    # —— 推理（改造）——
    async def vision_analyze(self, image_path: Path, prompt: str, max_tokens: int = 256) -> str:
        # init → prefill(img, None, 1) → decode_stream(prompt, max_tokens) 拼接
    async def chat(self, prompt: str, max_tokens: int = 512) -> str:
        # init → decode_stream(prompt, max_tokens) 拼接（纯文本）
    async def decode_stream(self, prompt: str, max_tokens: int) -> AsyncIterator[str]:
        # ③ 发起 stream=true，逐 SSE 块 yield 文本增量
```

---

## 3. SSE 逐行解析器（新增，独立小模块）

建议放 `backend/app/engine/sse.py`（单文件 ≤300 行约束），提供纯函数解析器，**不绑定 httpx 之外的类型**，便于单测。

```python
@dataclass
class SseEvent:
    kind: str            # "delta" | "done" | "error"
    content: str = ""    # delta: 文本增量；error: 错误描述
    raw: str = ""        # 原始行（调试用）

def parse_sse_line(line: str) -> SseEvent | None:
    """解析单行 SSE：`data: <payload>`；返回 None 表示空行/注释/事件名行"""
    # 规则：
    # - 去除行尾 \r\n；strip()
    # - 空行 → None（SSE 事件分隔符，本协议逐块发送可不依赖）
    # - 不以 "data:" 开头 → None
    # - payload 为 "[DONE]" → SseEvent(kind="done")
    # - payload 为 JSON 且含 "content" → SseEvent(kind="delta", content=obj["content"])
    # - payload 为 JSON 且含 "error" → SseEvent(kind="error", content=obj["error"])
    # - 其余非 JSON/结构不符 → 抛 SseProtocolError（协议错误分类，见 §5）

async def iter_sse_chunks(resp: httpx.Response) -> AsyncIterator[SseEvent]:
    """逐行消费 httpx 流式响应（resp.aiter_lines），封装 parse_sse_line，
    抛出 SseProtocolError / 透传底层网络错误"""
```

- 验收：单测 `tests/unit/test_sse.py` 覆盖——标准 delta 行 / `[DONE]` / 空行跳过 / 注释行跳过 / 非 `data:` 前缀跳过 / 畸形 JSON 抛 `SseProtocolError` / `{"error":...}` 分类为 error。
- **禁止**：用正则一次性匹配整包；禁止忽略畸形行（静默丢 token 会造成 JSON 判定残缺）。

---

## 4. 客户端方法细化（错误/超时/重试语义）

### 4.1 错误分类（统一以 `ModelServerError` 为基类抛出）

| 分类 | 判定 | 可重试 | 处理 |
|---|---|---|---|
| `ModelNetworkError` | `httpx.TransportError`（连接拒绝/超时/断流） | ✅ 是 | 整轮重试 1 次（见 §4.3） |
| `SseProtocolError` | 非 SSE 响应（content-type 不符）/ 畸形行 / 流内缺 `[DONE]` | ❌ 否 | 记日志 + `ModelServerError`；orchestrator 置 UNKNOWN |
| `ModelError` | HTTP 4xx/5xx；SSE `{"error":...}`；模型加载失败 | ❌ 否 | 记日志 + `ModelServerError`；orchestrator 置 UNKNOWN |

三者在 `llama_omni_client.py` 内定义（`ModelNetworkError(SseProtocolError(ModelServerError))` 不交叉继承，保持平级三类，统一聚合出口为 `ModelServerError`）。

### 4.2 超时

| 阶段 | 默认值 | 说明 |
|---|---|---|
| connect / 首字节 | 10s | 服务未起时快速失败 |
| prefill | 60s | 视觉预填充（B1 实测后校准，占位） |
| decode 流空闲（相邻块间隔） | 20s | 超过视为断流 → `ModelNetworkError` |
| 整轮 | prefill + decode 上限 120s | 超过 → 失败，不无限等待 |

超时值均进 `Settings`（`model_*` 组），不散写常量。

### 4.3 重试策略

- **只重试网络类**：整轮（init→prefill→decode）重试 1 次，退避 1s；重试后仍失败 → 抛 `ModelServerError`。
- 协议/模型错误不重试（重试无意义且浪费时间）。
- 重试计数与耗时计入 `metrics.record_analysis`（见 vram-guard-spec §6 指标扩展，此处仅消费）。

---

## 5. orchestrator `_llm_lock` 单实例互斥衔接

`orchestrator._tick_one` 现状已用 `async with self._llm_lock` 包裹 `analyzer.analyze(frame_path)`——**保持并显式契约化**：

1. **锁覆盖整轮**：`init → prefill → decode 流消费完毕` 全程持锁。SSE 逐块 yield 不得把锁释放给其他协程（asyncio 锁非可重入，若 `decode_stream` 内部再次 `async with self._llm_lock` 会死锁——禁止嵌套获取）。
2. 语音会话（V1.1 `set_voice_active`）与视觉监控共享同一 `_llm_lock`：对话期间视觉推理自然被串行/降频（`voice_active_poll_interval_seconds` 已生效），显存不双实例并发（ADR-001 硬约束）。
3. 单轮失败不得破坏锁状态：`finally` 中释放（`async with` 天然保证），异常上抛由 `_tick_one` 捕获置 UNKNOWN（现状逻辑保留）。
4. 新增：`_llm_lock.locked()` 状态并入 `state.inference_busy`（当前 `GlobalState.inference_busy` 未接线——改造时在进入/退出锁处写入，供 `/health` 与 UI 展示）。

---

## 6. 文件与改动清单

| 文件 | 改动 |
|---|---|
| `backend/app/engine/sse.py` | 新增：`SseEvent` / `parse_sse_line` / `iter_sse_chunks` |
| `backend/app/engine/llama_omni_client.py` | 改造：`init_session/prefill/decode_stream` 新增；`vision_analyze/chat` 内部改 SSE 拼接；错误三分类；超时/重试 |
| `backend/app/engine/vision_analyzer.py` | 无接口改动（`analyze` 仍返回 `VisionResult`） |
| `backend/app/config.py` | `Settings` 增模型超时/重试字段（`model_connect_timeout_s` 等） |
| `backend/app/core/orchestrator.py` | 锁语义确认（不嵌套）；`inference_busy` 接线 |
| `backend/tests/unit/test_sse.py` | 新增解析器单测（§3 验收） |

---

## 7. 依赖与归属判断

| 项 | 归属 | 依赖 |
|---|---|---|
| SSE 解析器（§3）+ 错误分类 + 超时/重试 + 锁语义（§4/§5） | **V1 立即实现** | 无（结构与单测先行，与 PoC B1 并行） |
| `init/prefill/decode` 的**具体请求体字段与响应字段** | **V1 定稿** | **依赖 PoC B1 实测回填 §1/§3/§4 的 `{{POC-B1}}` 占位**；B1 通过后 1 个工作日内回填并锁定接口 |
| `decode` 语音双工（audio 路径） | **V1.1** | PoC B3（voice duplex） |

> 若 PoC B1 判定①不通过（显存超限/延迟>6s），按 POC-001 §失败备用降级（换量化/降 ctx/换 MiniCPM-V），本客户端接口不变、仅上游模型服务形态变化，契约仍有效。

---

## 8. 验收清单（照做）

- [ ] `parse_sse_line` 单测全绿（§3 用例）
- [ ] 真实服务（B1 环境）下 `vision_analyze` 返回**纯文本**（非原始 SSE），`VisionAnalyzer` 能 `json.loads` 出三态 JSON
- [ ] 流中断/服务未启动 → `ModelNetworkError` 且重试 1 次；重试后仍败 → `ModelServerError`，orchestrator 置 UNKNOWN，不崩循环
- [ ] 畸形 SSE 行 → `SseProtocolError` 不静默跳过，不产出残缺 JSON
- [ ] `_llm_lock` 全程单实例持锁，无死锁；`state.inference_busy` 随锁状态变化

## 9. 端到端验证（E2E）

1. 启动 llama-omni 服务 → `curl /health` up；
2. 后端起 → `/api/v1/status` engine.model_loaded=true；
3. 取真实窗口截图 → 触发一轮监控 tick → `/api/v1/status/sessions/codex` `state∈{progress,stuck,off_track}` 且 `last_summary` 为中文摘要（非 SSE 原文）；
4. 杀掉模型服务 → 下一轮 tick 会话置 `unknown`、日志含网络错误重试记录、进程不退出；
5. 重启模型服务 → 恢复 `progress/stuck/off_track` 判定。
