# ADR-003: 语音管线 — 模型原生全双工 + silero-vad 门控

- 状态：已接受
- 日期：2026-08-03
- 决策者：架构师 高见远

## 背景

需要"类 GPT-Live"实时语音对话（能听能说能打断）。MiniCPM-o 4.5 自带 APM 音频感知 + TTS + 全双工打断能力，无需自研 STT/TTS 全管线。

## 选项对比

**STT**：
| 方案 | 能力 | 结论 |
|---|---|---|
| 模型原生 APM | GPU 1.2GB，全双工 | 选中（主路径） |
| sherpa-onnx v1.13.2 流式 SenseVoice | CPU 60-200ms 首字，140-280MB | 兜底降级 |
| whisper.cpp | CPU 180ms+ | 否决（慢） |
| FunASR | 120-200ms | 否决（重） |

**TTS**：
| 方案 | 能力 | 结论 |
|---|---|---|
| 模型原生 TTS | GPU 0.6GB，离线，支持音色克隆 | 选中（主路径） |
| edge-tts | 云端 200-500ms+网络 | 兜底降级 |
| CosyVoice2 / GPT-SoVITS / ChatTTS | 本地但显存+配置重 | 否决 |

## 决策

- 对话语音：**模型原生全双工**（音频直入模型、语音直出，barge-in 原生）
- 麦克风门控：**silero-vad**（PyPI 6.2.1，`load_silero_vad` / `VADIterator`，16kHz，onnx 后端）
- 兜底降级链：sherpa-onnx 1.13.2（流式 STT）+ edge-tts（`--voice zh-CN-XiaoxiaoNeural`）→ 半双工分轮回话

## 后果

- 正面：端到端延迟目标 1.0-1.5s（官方 ~800ms），打断原生无需自研
- 负面：原生全双工与监控推理共享显存 → 对话期监控降频 10-15s/帧（时分复用）
- 替代触发条件：PoC B3 原生延迟 >2s 或打断不稳 → V1.1 降级半双工（STT→生成→TTS），全双工延后 V1.2
