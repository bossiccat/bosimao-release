# POC-001: 模型视觉推理验证（风险①）

> 状态：待执行 | 判定人：架构师 高见远（经项目总监复核）

## 目标

验证 MiniCPM-o 4.5 Q4_K_M 能在 RTX 3060 12G 上运行，且单帧截图视觉判定延迟可接受（监控轮询 5-8s 预算内）。

## 步骤

```powershell
# 1. 启动模型服务（模型下载完成后）
D:\models\Comni-Windows-x64.exe  # 或手动启动 llama-omni-server
# llama-omni-server --host 127.0.0.1 --port 19080 --model D:\models\MiniCPM-o-4_5-gguf\MiniCPM-o-4_5-Q4_K_M.gguf -ngl 99 --ctx-size 8192

# 2. 健康检查
curl http://127.0.0.1:19080/health

# 3. 视觉压测（scripts/poc_001_model.ps1）
# - 取 10 张真实三 App 窗口截图（降采样宽 ≤1280）
# - POST /v1/stream/prefill（img_path_prefix=截图, cnt=1）→ /v1/stream/decode（stream=true）
# - 记录每张：prefill 耗时 / 首 token / 完整 JSON 判定耗时 / 显存峰值
# - 跑 3 轮取 P50/P95
```

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

## 结论记录

- [ ] 通过（记录实测数值）
- [ ] 降级（记录采用的备用方案）
