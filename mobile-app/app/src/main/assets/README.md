# assets/ — 唤醒词模型放置说明

本目录需要放置 sherpa-onnx KWS 模型（**不入库**，运行 `scripts/fetch-deps.ps1` 自动下载并按 `scripts/deps-checksums.txt` 校验 SHA-256，或手动下载后比对哈希）：

## 模型

- 名称：`sherpa-onnx-kws-zipformer-wenetspeech-3.3M-2024-01-01`（中文，Apache-2.0）
- 下载：https://github.com/k2-fsa/sherpa-onnx/releases/download/kws-models/sherpa-onnx-kws-zipformer-wenetspeech-3.3M-2024-01-01.tar.bz2
- 归档 SHA-256：`b2f7c89690dc8ce4c6ed6afeab7cd800c36ad1421fb6b6302b4a4b194cf7f35f`
- 解压后目录结构（WakeWordEngine.kt 按此路径加载，**int8 变体**）：

```
assets/sherpa-onnx-kws-zipformer-wenetspeech-3.3M-2024-01-01/
├── encoder-epoch-12-avg-2-chunk-16-left-64.int8.onnx
├── decoder-epoch-12-avg-2-chunk-16-left-64.int8.onnx
├── joiner-epoch-12-avg-2-chunk-16-left-64.int8.onnx
├── tokens.txt
└── keywords_jax.txt (项目自建，来自 scripts/keywords_jax.txt.template)
```

> 上游归档另含非 int8 变体、epoch-99 变体与 test_wavs/ —— 均非运行时依赖，
> 仅 int8 三件套 + tokens.txt + keywords_jax.txt 进入构建闭集（逐文件 SHA-256 见
> `scripts/deps-checksums.txt`；完整来源与策略见 `docs/dependency-closure.md`）。

- 关键词：`波斯猫`（代码 VoiceConfig.WAKEWORD；keywords_jax.txt 内容为
  `b ō s ī m āo @波斯猫`，v0.6.2 起经 keywordsFile 加载，不再运行时传拼音串）

> 若想换关键词：改 `VoiceConfig.WAKEWORD` + 更新 `scripts/keywords_jax.txt.template`
> 与 `scripts/deps-checksums.txt` 中对应哈希，免重训（spec §4.2 主选理由）。
