# 后端规格 — 显存守卫（P1）

> 版本：v1.0（M-1 修复基线）
> 日期：2026-08-03
> 状态：已确认 · 后端照做（阈值依赖 PoC B1 实测回填，见 §7）
> 依据：docs/decisions/ADR-001-model-engine.md（Q4_K_M ≈8.3GB，12G 显卡余量小）、docs/poc/POC-001-model-vision.md（通过标准：显存 ≤10.5GB/12G，余量 ≥1.5GB）、docs/openapi.yaml（/health model_vram_mb，V1.1 未实现）、backend/app/core/orchestrator.py（_llm_lock 时分复用）、backend/app/utils/metrics.py、backend/app/core/state.py（model_vram_mb / inference_busy）
> 范围：启动前可用显存校验 + 运行期显存趋势采样 + 与 LLM 时分复用锁联动。不引入新服务，仅 pynvml（优先）或 nvidia-smi 解析。

---

## 1. 目标

- 防止模型在显存不足时启动失败/崩溃/污染系统（12G 卡 + ~9GB 模型，余量仅 ~2-3GB，ADR-001 硬约束"不可双实例并发"）。
- 运行期检测显存异常增长（泄漏/多实例占用），联动监控降频或暂停，避免 OOM 拖垮后端主进程。

## 2. 启动前校验（拒绝启动/提示）

### 2.1 采集实现

```python
# backend/app/utils/vram.py（新增，单文件 ≤300 行）
def get_vram_info() -> VramInfo | None:
    """返回 {total_mb, used_mb, free_mb, process_used_mb}；GPU 不可用/无驱动 → None"""
    # 优先 pynvml：pynvml.nvmlInit() → nvmlDeviceGetHandleByIndex(0)
    #            → nvmlDeviceGetMemoryInfo() → total/used/free
    # 失败降级 nvidia-smi --query-gpu=memory.total,memory.used,memory.free --format=csv,noheader,nounits 解析
    # 两者都失败 → None（守卫降级为"跳过启动校验"，日志 WARNING，不阻断）
```

### 2.2 启动判定

```
required_free_mb = MODEL_VRAM_ESTIMATE_MB + VRAM_HEADROOM_MB
                 = 9000（占位，B1 实测模型峰值回填） + 1500（POC-001 余量标准）
启动模型服务（llama-omni）前：
  free_mb < required_free_mb → 拒绝启动模型服务 + 拒绝 Orchestrator 视觉链路
                               日志 ERROR + 明确中文提示（含 free/total/required）
                               不影响后端 HTTP/推送/WS（监控目标显示 unknown + last_error="显存不足"）
  free_mb >= required_free_mb → 正常启动
```

- 触发点：`main.py` lifespan 构建 `LlamaOmniClient`/启动模型服务前调用一次；`Orchestrator.start()` 前再校验一次（双保险，热重载后不重复）。
- 与 `/health` 联动：显存不足时 `model_server="down"`，`model_vram_mb` 上报实际峰值（openapi 已定义该字段，`x-phase: v1.1` → 本 spec 将其提前到 V1 实现，openapi 修订点）。

## 3. 运行期采样（趋势）

### 3.1 采样频率与来源

- 每 **N=10 帧**（或每 ~30s，取先到者）采样一次；来源同 §2.1（pynvml 优先），采样自身开销 <5ms，禁止阻塞监控循环（放异步任务，`asyncio.to_thread`）。
- 采样的对象：`get_vram_info()` 全卡 free/used + 若可识别模型服务进程 PID（配置 `Settings.model_server_pid` 可选）则读其 `process_used_mb`。

### 3.2 与 metrics.py 衔接（新增方法）

```python
# backend/app/utils/metrics.py（扩展，现有 Metrics 类增加字段/方法）
vram_samples: deque[float] = field(default_factory=lambda: deque(maxlen=120))  # MB
vram_peak_mb: float = 0.0
def record_vram(self, mb: float) -> None: ...      # 追加 + 更新峰值
# summary() 增加：vram_free_p50_mb / vram_free_min_mb / vram_peak_mb
```

- 趋势判定（每采样点）：
  - `free_mb < WARN_FREE_MB`（占位 `required_free_mb` 值，B1 回填）→ **WARN 级**：日志 + 联动降频（见 §4）；
  - 连续 3 个采样点 free 持续下降且低于 WARN → **CRITICAL 级**：暂停视觉监控（`orchestrator.stop_monitoring()` 语义）+ WS 通知（复用 `alert` 事件，app_id=`engine`，level=4，summary="显存不足，已暂停监控"）。

## 4. 与 orchestrator 时分复用锁配合

| 机制 | 关系 |
|---|---|
| `_llm_lock`（单实例互斥） | 保证任一时刻仅一个 LLM 调用（视觉/语音）；**显存守卫不参与该锁**——采样是只读 GPU 查询，无需持锁、不得持锁（避免与推理互斥竞争） |
| `set_voice_active`（对话期降频） | 对话期间 `voice_active_poll_interval_seconds=12` 已降低推理频率 → 显存占用平稳；守卫阈值在语音峰值上预留（B1 语音峰值回填 `{{POC-B1}}`） |
| WARN 联动 | `orchestrator` 提高各目标 `poll_interval_seconds`（×2，上限 30s）→ 降低推理密度；恢复 free 后还原 |
| CRITICAL 联动 | `stop_monitoring()` 暂停整个视觉链路（保留推送/WS）；用户手动 `resume_monitoring` 前守卫每 30s 复查，free 恢复 → 自动恢复 |

## 5. 配置项（进 Settings / config）

| 键 | 默认 | 说明 |
|---|---|---|
| `model_vram_estimate_mb` | 9000（占位 {{POC-B1}}） | 模型推理峰值估算 |
| `vram_headroom_mb` | 1500 | POC-001 余量标准 |
| `vram_warn_free_mb` | 1500（占位） | 低于即 WARN |
| `vram_sample_every_frames` | 10 | 采样间隔（帧） |
| `vram_sample_interval_s` | 30 | 采样间隔（秒，先到者） |

## 6. 文件与改动清单

| 文件 | 改动 |
|---|---|
| `backend/app/utils/vram.py` | 新增：`get_vram_info()` / 阈值常量 |
| `backend/app/utils/metrics.py` | 扩展：`record_vram` / `vram_peak_mb` / summary 字段 |
| `backend/app/main.py` | 启动前调用守卫，不足时拒绝模型链路并提示 |
| `backend/app/core/orchestrator.py` | 运行期采样任务 + WARN/CRITICAL 联动（降频/暂停/恢复） |
| `backend/app/core/state.py` | `model_vram_mb` 运行期回填（openapi /health 字段） |
| `backend/app/config.py` | `Settings` 增 §5 字段 |
| `backend/tests/unit/test_vram.py` | 新增：`get_vram_info` 降级路径（nvidia-smi 缺失/无 GPU → None）+ 阈值判定单测 |

## 7. 依赖与归属判断

| 项 | 归属 | 依赖 |
|---|---|---|
| `vram.py` 采集（pynvml/nvidia-smi 双路径）+ 启动校验 + 运行期采样框架 + metrics 扩展 | **V1 立即实现** | 无（结构先行，阈值用占位） |
| 阈值定稿（estimate/headroom/warn）与 `/health model_vram_mb` 提前 V1 | **V1 定稿** | **依赖 PoC B1 实测**：B1 记录显存峰值（推理中/空闲/语音）后回填 `{{POC-B1}}` 占位并定稿 |
| 语音峰值联动（set_voice_active 场景） | **V1.1** | PoC B3 |

## 8. 验收清单（照做）

- [ ] 显存不足启动：模型链路拒绝、`/health model_server=down`、日志中文提示、HTTP/WS 仍可用
- [ ] 无 GPU/nvidia-smi 缺失：`get_vram_info()` 返回 None，守卫跳过不阻断（WARNING 日志）
- [ ] 运行期每 N 帧采样，`metrics.summary()` 含 vram 三字段
- [ ] free < WARN → 日志 WARN + 轮询降频 ×2；恢复还原
- [ ] 连续 3 点低于 WARN → 暂停监控 + WS alert（engine, level 4）；free 恢复自动恢复
- [ ] `vram.py` / `metrics.py` 单测通过

## 9. E2E 验证

1. 正常环境启动 → 显存校验通过 → 视觉链路工作 → `/health` `model_vram_mb` 为实测峰值（>0）；
2. 模拟显存占用（另开 CUDA 进程吃 ~4GB）→ 重启后端 → 显存不足提示，`/api/v1/status` sessions 均 `unknown` + last_error="显存不足"；
3. 运行中人为占显存 → 日志 WARN → 轮询降频 → 继续占 → 监控暂停 + WS alert；释放显存 → 30s 内自动恢复。
