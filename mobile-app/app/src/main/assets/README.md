# assets/ — 唤醒词模型放置说明

本目录需要放置 sherpa-onnx KWS 模型（**不入库**，运行 `scripts/fetch-deps.ps1` 自动下载，或手动下载）：

## 模型

- 名称：`sherpa-onnx-kws-zipformer-wenetspeech-3.3M-2024-01-01`（中文，Apache-2.0）
- 下载：https://github.com/k2-fsa/sherpa-onnx/releases/download/kws-models/sherpa-onnx-kws-zipformer-wenetspeech-3.3M-2024-01-01.tar.bz2
- 解压后目录结构（WakeWordEngine.kt 按此路径加载）：

```
assets/sherpa-onnx-kws-zipformer-wenetspeech-3.3M-2024-01-01/
├── encoder-epoch-12-avg-2-chunk-16-left-64.onnx
├── decoder-epoch-12-avg-2-chunk-16-left-64.onnx
├── joiner-epoch-12-avg-2-chunk-16-left-64.onnx
├── tokens.txt
└── keywords.txt (参考)
```

- 关键词：`波斯猫`（代码 VoiceConfig.WAKEWORD；sherpa 免训练，createStream 传原始中文，引擎内部 ppinyin 转 token）

> 若想换关键词，直接改 VoiceConfig.WAKEWORD 即可，无需重训（spec §4.2 主选理由）。
