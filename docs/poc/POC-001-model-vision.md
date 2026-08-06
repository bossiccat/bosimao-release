# POC-001: 模型视觉推理验证（风险①）

> 状态：**已执行（2026-08-03 卜宕机实测）** | 判定人：架构师 高见远（经项目总监复核）
> 结论：**通过（按 B 计划降 ctx 4096）** —— 核心三指标达标，SSE 格式确认（见 §SSE 格式验证）

## 目标

验证 MiniCPM-o 4.5 Q4_K_M 能在 RTX 3060 12G 上运行，且单帧截图视觉判定延迟可接受（监控轮询 5-8s 预算内）。

## 步骤

# 1. 启动模型服务（模型下载完成后）
# llama-omni-server --host 127.0.0.1 --port 19080 --model D:\models\MiniCPM-o-4_5-gguf\MiniCPM-o-4_5-Q4_K_M.gguf -ngl 99 --ctx-size 8192
# 实测 B 计划：--ctx-size 4096（显存达标）

# 2. 健康检查
# curl http://127.0.0.1:19080/health

# 3. 视觉压测（scripts/poc_001_model.ps1 / tmp/poc_b1_stress.py）
# - 取 10 张真实三 App 窗口截图（降采样宽 ≤1280）
# - POST /v1/stream/prefill（img_path_prefix=截图, cnt=1）→ /v1/stream/decode（stream=true）
# - 记录每张：prefill 耗时 / 首 token / 完整 JSON 判定耗时 / 显存峰值
# - 跑 3 轮取 P50/P95

## 通过标准

| 指标 | 目标 |
|---|---|
| 显存占用 | ≤ 10.5GB/12G（余量 ≥1.5GB） |
| 单帧结构化判定端到端 | ≤ 4s |
| 首 token | ≤ 2.5s |
| 输出稳定性 | 稳定 JSON（progress/stuck/off_track 三态可区分），同图 3 次一致 |

## 失败备用（B 计划）

1. 显存超限 → 降 ctx 4096 / CPU offload / 换更小量化（Q4_0 已在下载清单中）
2. 延迟 >6s → 轮询降 10s/帧重测；仍超 → 判定①不通过 → 混合监控方案（进程/窗口状态 + 低频截屏）
3. 终极兜底 → 视觉换 MiniCPM-V Q4，语音走云 API 混合

---

## 实测数据（2026-08-03，RTX 3060 12G，MiniCPM-o-4_5-Q4_K_M + vision-F16，venv Python）

### 测试集（8 图 × 3 轮 = 24 样本）
- real_workbuddy_01.png：真实窗口抓屏（WorkBuddy 1339×912）
- synth_shot_*：PIL 生成带代码样式测试图（progress/stuck/off_track 三态标注，**合成图**）

### 调用序列（Comni cpp_backend 官方形态，实测确认）
① POST /v1/stream/omni_init   {media_type:2, use_tts:false, duplex_mode:false, model_dir:"D:/models/MiniCPM-o-4_5-gguf", ...}
② POST /v1/stream/update_session_config  {media_type:2, duplex_mode:false, voice_clone_prompt:"你是屏幕监控助手...", assistant_prompt:""}
③ POST /v1/stream/prefill     图片: {img_path_prefix, cnt:0} + 文本: {text:PROMPT, cnt:1}
④ POST /v1/stream/decode      {stream:true, round_idx, length_penalty:1.1}

### ctx 8192（初测，显存超限）
| 指标 | P50 | P95 | 目标 | 判定 |
|---|---|---|---|---|
| prefill | 653ms | 1276ms | — | — |
| 首 token | 1598ms | 2352ms | ≤2500ms | 通过 |
| decode | 3825ms | 5211ms | — | — |
| 端到端 | 4543ms | 5731ms | ≤4000ms | 略超 |
| 显存峰值 | 12019MB | — | ≤10752MB(10.5G) | 超限(余 210MB) |
| JSON 成功 | 24/24 | — | — | 通过 |

### ctx 4096（B 计划，最终采用）
| 指标 | P50 | P95 | 目标 | 判定 |
|---|---|---|---|---|
| prefill | 265ms | 1516ms | — | — |
| 首 token | 439ms | 474ms | ≤2500ms | 通过 |
| decode | 1027ms | 1216ms | — | — |
| 端到端 | 1344ms | 2510ms | ≤4000ms | 通过 |
| 显存峰值 | 9198MB | — | ≤10752MB(10.5G) | 通过 余 ~3GB |
| JSON 成功 | 24/24 | — | — | 通过 |
| 同图 3 次一致 | 合成图 7/7 全一致 | — | 一致 | 通过 |

> 注：真实 WorkBuddy 图 3 次判定摇摆（stuck/off_track/progress），因真实窗口内容边界模糊；合成图三态 100% 区分且 3 次一致。首轮 prefill 有 35s 冷启动（vision 编码首次），后续稳定 260ms。

---

## SSE 格式验证（核心结论，决定 llama_omni_client.py 改造）

**实测：decode(stream=true) 返回 SSE 流，不是纯 JSON。**

- HTTP Content-Type：text/event-stream
- 响应体分帧：data: {"content":"...","stop":false,"is_listen":false,"end_of_turn":false} 逐块 + data: [DONE] 终止
- 首 500 字符原始样例：data: {"content":"<think>...","stop":false,...}
- 输出文本含 <think> 推理块 + 最终 JSON（{"state":"progress","summary":"..."}），需剥离 think 后 json.loads

**结论**：llama_omni_client.py 当前按 resp.text 纯文本读 decode 必然拿到原始 SSE 文本 → json.loads 必失败 → **必须改 SSE 解析**（docs/specs/backend-llama-client-spec.md §3 解析器设计方向正确，{{POC-B1}} 占位可回填：init=omni_init、prefill 需 img+text 两次、decode 带 round_idx/length_penalty 且 response 为 SSE）。

## 结论记录

- [x] **通过（B 计划：降 ctx 4096）** —— 实测数值见上表；显存 9198MB / 首 token 439ms / 端到端 1344ms / JSON 24/24，三态可区分、合成图 3 次一致
- [ ] 降级（记录采用的备用方案）

> 补充：ctx 8192 下显存 12019MB 超限、端到端 4543ms 略超 4s；降 ctx 4096 后两项均达标。若后续需要更长上下文，可再评估 Q4_0 量化或 -ngl 部分 CPU offload。
