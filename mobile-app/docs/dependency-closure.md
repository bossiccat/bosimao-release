# mobile-app 依赖闭集（Dependency Closure）

> 适用范围：`mobile-app` 模块构建 APK 时消费的全部**非 Maven** 二进制输入
> （本地 AAR + sherpa-onnx KWS 模型）。Maven 依赖锁定另见 Gradle lockfile（P0-GRADLE-003）。
>
> 维护规则：**闭集是白名单**。新增/升级任何一个二进制输入，必须同步更新
> `scripts/deps-checksums.txt`（哈希）与本文件（来源/版本/路径），缺一不可。

## 策略

**二进制不入库，哈希清单入库。**

- 理由：① AAR 48.8MB + 模型归档 32.7MB，入库使仓库不可逆膨胀；
  ② 上游归档含非运行时变体（非 int8、epoch-99、test_wavs）无入库价值；
  ③ 供应链完整性由 SHA-256 清单承担 —— 攻击者无法在不改动入库文本
  （`deps-checksums.txt`，需过 code review）的情况下替换任何二进制内容。
- 执行点：`scripts/fetch-deps.ps1` 下载后立即校验归档哈希，解压后逐文件校验
  运行时闭集；不匹配 → 删除文件 → 非 0 退出。已存在文件同样先验后用。
- `.gitignore` 用**精确路径**（`app/libs/sherpa-onnx-1.13.4.aar`、模型目录名）
  而非泛匹配 `*.aar` / `sherpa-onnx-*`：任何清单外的二进制不会被静默忽略，
  会以 untracked 形式暴露在 `git status` 中。
- 唯一例外：`keywords_jax.txt` 是本项目自建文本文件（上游归档不含），其模板
  `scripts/keywords_jax.txt.template` **入库**作为唯一真源；脚本在模型就位后
  从模板恢复该文件并校验哈希。

## 清单

### 1. sherpa-onnx Android AAR（Java/Kotlin API + JNI + .so）

| 项 | 值 |
|---|---|
| 版本 | sherpa-onnx **1.13.4**（API 与 `WakeWordEngine.kt` import 核对） |
| 来源 URL | `https://github.com/k2-fsa/sherpa-onnx/releases/download/v1.13.4/sherpa-onnx-1.13.4.aar` |
| 本地路径 | `mobile-app/app/libs/sherpa-onnx-1.13.4.aar` |
| 大小 | 48,847,529 bytes（46.6 MiB） |
| SHA-256 | `03f9c4df965f21c71269365a7951a7f23b5696fddd093fa318c80d65550ab780` |
| 许可 | Apache-2.0 |
| 引入方式 | `app/build.gradle.kts` `fileTree(libs)`（见下方"已知遗留"） |

### 2. KWS 唤醒词模型（归档）

| 项 | 值 |
|---|---|
| 名称 | sherpa-onnx-kws-zipformer-wenetspeech-3.3M-2024-01-01 |
| 来源 URL | `https://github.com/k2-fsa/sherpa-onnx/releases/download/kws-models/sherpa-onnx-kws-zipformer-wenetspeech-3.3M-2024-01-01.tar.bz2` |
| 下载暂存 | `%TEMP%\sherpa-kws.tar.bz2`（校验后解压即删） |
| 大小 | 32,654,866 bytes（31.1 MiB） |
| SHA-256（归档） | `b2f7c89690dc8ce4c6ed6afeab7cd800c36ad1421fb6b6302b4a4b194cf7f35f` |
| 许可 | Apache-2.0 |

### 3. 运行时闭集（WakeWordEngine.kt 实际加载的文件）

解压目标：`mobile-app/app/src/main/assets/sherpa-onnx-kws-zipformer-wenetspeech-3.3M-2024-01-01/`

| 文件 | 大小 (bytes) | SHA-256 | 消费方 |
|---|---|---|---|
| `encoder-epoch-12-avg-2-chunk-16-left-64.int8.onnx` | 4,777,666 | `dd784973fc9d2fabb3b800d6dcd20fc3b0ca84f8e2415afe54b032878e447f4d` | `OnlineTransducerModelConfig.encoder` |
| `decoder-epoch-12-avg-2-chunk-16-left-64.int8.onnx` | 181,069 | `ed83454004d5bd16d831eaf00adcd181ed7734886aab6ef440f3ffa5aa3cfe3b` | `OnlineTransducerModelConfig.decoder` |
| `joiner-epoch-12-avg-2-chunk-16-left-64.int8.onnx` | 65,242 | `f79760052b87239e325f0567c752ad3130b30d92effb847d4307743c20c59a24` | `OnlineTransducerModelConfig.joiner` |
| `tokens.txt` | 1,627 | `72316508d9119696145abc6f1f8cdc46287535c34e5ce7e595f845cb1499cf2e` | `OnlineModelConfig.tokens` |
| `keywords_jax.txt` | 27 | `d5e91c16a4ae64197ce3d3e123ebaabb4f3598aaaebf83019401d529a766ffb5` | `KeywordSpotterConfig.keywordsFile` |

模型加载关键参数（`WakeWordEngine.kt:93-118`）：`modelType = "zipformer2"`（v0.6.1
实测该模型为 zipformer2 架构，写 zipformer 会 native 崩溃）、`numThreads = 2`、
`provider = "cpu"`、`featureDim = 80`、`sampleRate = 16000`。

`keywords_jax.txt` 内容（UTF-8，单行）：`b ō s ī m āo @波斯猫`

### 归档内非闭集文件（仅存在于本地解压目录，不进 APK 闭集管控）

非 int8 三件套（epoch-12 与 epoch-99 变体）、`keywords.txt`、`keywords_raw.txt`、
`configuration.json`、`README.md`、`test_wavs/*`。这些文件随 tar.bz2 解压落在
assets 目录（会被打包进 APK），但运行时不加载。其上游真源即第 2 节归档哈希，
不单独管控。**可选后续优化**：解压后删除非闭集文件可缩小 APK 体积（~17MB）。

## 校验流程（scripts/fetch-deps.ps1）

```
读取 scripts/deps-checksums.txt（archive:/file: 哈希清单）
 ├─ AAR：存在→先验哈希；缺失/不符→(重)下载→再验 → 失败=删除+exit 1
 ├─ 模型：tokens.txt 验过→跳过；
 │        否则删模型目录→下载 tar.bz2→验归档哈希→解压→逐文件验运行时闭集
 ├─ keywords_jax.txt：哈希不符/缺失→从仓库模板恢复→复验
 └─ jniLibs：仅提示（AAR 已含 .so，正常无需手动）
```

## 已知遗留（不在本任务范围）

- `app/build.gradle.kts:55-56` 仍用 `fileTree("libs")` 泛匹配引入 AAR。闭集管控后
  实际风险已收敛（精确文件名 + 哈希 + .gitignore 精确规则三重锚定），但 Gradle
  层面更严格的做法是 `implementation(files("libs/sherpa-onnx-1.13.4.aar"))` 精确
  引用 —— 留待 Gradle 审计后续项统一处理，避免与本任务（输入管控+文档）耦合。

## 变更记录

| 日期 | 变更 | 归档/AAR 哈希来源 |
|---|---|---|
| 2026-08-20 | 初版：闭集盘点 + 哈希校验 + 文档对齐（P0-GRADLE-002） | 本机 `sha256sum` 实测 + 从上游 URL 重新下载归档复核一致 |
