# POC-003: 全双工语音验证（风险③）

> 状态：待执行 | 判定人：架构师 高见远

## 目标

验证 MiniCPM-o 4.5 原生全双工语音在 3060 上延迟 ≤1.5s 且打断可用（类 GPT-Live 体验）。

## 步骤

1. 用 Comni 桌面版/官方 Demo 跑通语音对话 + 打断基线，录屏测：说话结束→首字端到端延迟
2. Python 侧验证 silero-vad 门控：
```python
from silero_vad import load_silero_vad, VADIterator
model = load_silero_vad()   # 16kHz
vad_iterator = VADIterator(model)
# 麦克风流 16kHz 单声道 float32 → vad_iterator(input_chunk) → speech_start/speech_end
```
3. 验证打断：模型输出中重新说话，确认"压扁"而非"排队等完"
4. 验证降级链：sherpa-onnx 1.13.2 流式 STT + edge-tts（zh-CN-XiaoxiaoNeural）分轮回话 ≤2.5s

## 通过标准

| 指标 | 目标 |
|---|---|
| 原生全双工端到端 | ≤ 1.5s（P50） |
| 打断响应 | < 500ms，可连续打断 3 次不崩溃 |
| VAD 门控 | 误触发 ≤5%/10min；检测延迟 <300ms |
| 麦克风权限 | Windows 首次授权弹窗可完成 |

## 失败备用（B 计划）

1. 原生延迟 >2s 或打断不稳 → V1.1 降级半双工（VAD 完整句 → STT → 单次生成 → TTS），全双工延 V1.2（已确认降级路线）
2. VAD 误触发高 → 调 threshold / min_speech_duration_ms / min_silence_duration_ms / 换 onnx 后端

## 结论记录

- [ ] 通过（记录实测延迟）
- [ ] 降级（记录采用的降级模式）
