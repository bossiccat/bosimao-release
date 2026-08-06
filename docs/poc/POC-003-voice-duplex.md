# POC-003: 全双工语音验证（风险③）

> 状态：**Comni 引擎单卡不可用（音频编码器未加载 + token2wav gpu:1 bug）→ 走降级路线** | 判定人：架构师 高见远
> 更新：2026-08-05 第二轮实测（tmp/poc_b3_pipeline.py 并发流水线）

## 目标

验证 MiniCPM-o 4.5 原生全双工语音在 3060 上延迟 ≤1.5s 且打断可用（类 GPT-Live 体验）。

## 第二轮实测结论（2026-08-05 深夜，深入根因定位）

### ❌ 最终判定：Comni 引擎（llama-server 19080）在这台 3060 单卡机上**原生 APM 不可用**

| # | 现象 | 根因 | 证据 |
|---|---|---|---|
| 1 | `omni_init` 后 `voice_audio_used:false`，`prefill` 音频 6–15ms 即返回（`kv_cache_length:0`） | **audio 编码器（audio/MiniCPM-o-4_5-audio-F16.gguf，660MB 在盘）从未被加载**——服务日志无任何 audio/APM 初始化记录；`--device`/`LLAMA_ARG_DEVICE`/`use_tts` 均不影响 | 日志 grep audio 为空；多次重启+参数组合无变化 |
| 2 | `flowGGUFModelLoader`/`voc_hg2_model`：`init_backend device=gpu:1 → invalid device 1` | **token2wav(flow/hifigan) gpu_idx=1 硬编码**，单卡（仅 CUDA0）无 device 1 → 原生 TTS 音频合成不可用 | 日志恒定 `gpu_idx=1, backend=CUDA0`；`--device CUDA0`/`--split-mode none`/`LLAMA_ARG_DEVICE=CUDA0` 均无效 |
| 3 | `omni_init` 拼接路径 `...gguftoken2wav-gguf/` 缺分隔符 | model_dir 尾部必须带 `/`（已修复脚本） | `failed to open GGUF file '...gguftoken2wav-gguf/...'` |
| 4 | `token2wav-gguf/` 缺 projector 文件（实际在 `tts/`） | 已复制补全（仍因 #2 不可用） | `ls token2wav-gguf/` |

**结论**：接口层全通（init/prefill/decode 200 + SSE 事件 + TTFT 875–1034ms 达标），
但**音频输入（audio 编码器不加载）与输出（token2wav gpu:1）双侧物理不可用** → **原生 APM 全双工在本机 Comni 引擎上无法落地**。
已满足"3+ 修复失败 → 质疑架构"条件，走 O-015 降级分支（ADR-003 预案）：**路径 B 半双工 + 手机侧 VAD 打断**。

### 可复用的有效结论（供降级路线使用）

1. **接口契约**（taowen/llama.cpp-omni）：init 内部 cnt=0；prefill 1s 16k 块循环 cnt 递增；decode 传 debug_dir，TTS 写 `round_XXX/tts_wav/*.wav`；SSE 文本 `content/is_listen/stop` —— 已回填 mobile-voice-spec §8.2
2. **并发流水线模式**：prefill 持续喂 + decode 并行收（poc_b3_pipeline.py 已验证架构）
3. **资源互斥**：监控 vision 占用 GPU 时模型推理必超时；暂停 `POST /api/v1/control {action:"pause_monitoring"}`
4. **模型可用性**：主模型 Q4_K_M + tts 语义模型正常；vision（监控用）正常

## 第三轮实测（2026-08-05 深夜，逆向 Comni GUI 引擎参数）

### ✅ 突破性进展（逆向 cpp_backend.py 拿到引擎真实参数）

| 修复 | 参数 | 效果 |
|---|---|---|
| token2wav gpu:1 → gpu:0 | omni_init 传 `token2wav_device: "gpu:0"`（Comni `_DEFAULT_TOKEN2WAV_DEVICE`） | flowGGUF/voc_hg2 初始化成功 |
| audio 编码器加载 | `media_type: 2`（Comni GUI 用 2 非 1）+ token2wav_device | `audition model initialized successfully`（660MB audio-F16 真正加载） |
| prefill 音频处理 | 上述 init 修复后 | prefill 985–2198ms（真实处理，之前 6-15ms 跳过） |

### ❌ 最终阻塞（cpp 后端单卡不可用，判定关闭）

- `prefill` 的 `audio_path_prefix` 被 Comni 定制引擎忽略（日志恒定 `aud_fname:tts` → `Unable to open file tts` → audio skipped）；`aud_fname` 字段 = 400 非法。
- **Comni GUI 全双工走的是 torch 后端（worker.py UnifiedProcessor，Python+torch），不是 llama-server cpp 后端**。cpp 后端单卡全双工 = 引擎 bug，外部参数无法绕过。
- Comni GUI 引擎启动参数（逆向，供 torch 后端评估）：`CUDA_VISIBLE_DEVICES=gpu_id`、`--ctx-size 8192`、端口 `19060+gpu_id`；omni_init 体：`media_type=2, token2wav_device="gpu:0", tts_bin_dir, tts_gpu_layers=100, output_dir`。

### 原生 APM 剩余路径

- **A. Comni torch 后端**：跑 Comni 的 worker+gateway（Python/torch/transformers），经其 HTTP API 对接；风险=3060 12GB 跑 Q4 全模态（~9GB）紧张，需实测。
- **B. 降级路线**（O-015 分支）：路径 B + 手机侧 VAD 打断（silero-vad + sherpa 流式 STT + edge-tts），立即可交付。
- **C. llama-server cpp**：单卡不可行（引擎 bug），关闭。


