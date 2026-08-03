# ADR-001: 本地推理引擎选型 — llama.cpp-omni

- 状态：已接受
- 日期：2026-08-03
- 决策者：架构师 高见远（经项目总监审计）

## 背景

需要本地运行 MiniCPM-o 4.5 多模态 9B（视觉 + 音频 + TTS + 全双工语音），硬件为 RTX 3060 12G。要求：全模态能力完整、Windows 可部署、可编程化接入。

## 选项对比

| 方案 | 全模态支持 | 全双工语音 | 显存 | 结论 |
|---|---|---|---|---|
| llama.cpp-omni（OpenBMB 官方 fork） | 完整（VPM 视觉 + APM 音频 + TTS + Token2Wav） | 原生支持 + 打断 | Q4_K_M ~9GB | 选中 |
| Ollama（openbmb/minicpm-o4.5） | 仅 Text+Image（无音频/TTS） | 不支持 | 6.1GB | 否决 |
| vLLM | 支持但面向 A100/H100 服务器，Windows 弱 | 无流式语音 | 紧 | 否决 |
| LM Studio | 全模态适配不完整、不可编程化 | 无 | — | 否决 |

## 决策

- 推理引擎：**llama.cpp-omni**（GitHub: WeiyueSUN/llama.cpp-omni；releases: tc-mb/llama.cpp-omni）
- 安装包：`Comni-Setup-win64.exe`（GitHub Releases 或 ModelScope `modelscope.cn/models/OpenBMB/MiniCPM-o-4_5-gguf/app/Comni-Windows-x64.exe`）
- 模型：`OpenBMB/MiniCPM-o-4_5-gguf` Q4_K_M（LLM 4.9GB + vision F16 0.9GB + audio F16 1.2GB + TTS 0.6GB + token2wav 0.7GB ≈ 8.3GB）
- 服务端口：`127.0.0.1:19080`；HTTP API：`/health`、`/v1/stream/prefill`、`/v1/stream/decode`

## 后果

- 正面：全双工语音 + 打断为模型原生能力（官方端到端 ~800ms，3060 估算 1.0-1.5s），无需自研 STT/TTS 管线
- 负面：12G 显存余量小（约 2-3GB），不可双实例并发；模型升级需下载新 GGUF
- 替代触发条件：PoC B1 显存超限或延迟 >6s → 降 ctx/换量化/CPU offload → 终极换 MiniCPM-V（视觉）+ 云 API 语音混合
