# 波斯猫 商业发布交接（2026-09-15）

> **读者**：接手的会话 / 专家 / 人。**先读本文件**，再看 `docs/STATUS.md` 末节（2026-09-15 追加）。
> **本文档的判据**：凡是写"已验证"的，附可复跑的判据；凡是"我推断的"，明确标出。
> **不要相信本文档 —— 按 §6 自己跑一遍。**

---

## 0. 一句话

产品是**波斯猫（Bosimao）**：商业化移动端实时双工语音 agent —— Android/模拟器 + MiniCPM-o 端到端语音前台，
后端用 Hermes 引擎做 ACP 委派。**控制面在云端**（CloudRun `jax-voice-api`），**媒体面在用户机器**
（`rtc_bridge` + Electron sidecar + TRTC）。不 fork Hermes、不自造引擎。

⚠️ `docs/STATUS.md` 的前 1–7 节**已过期**（冻结在 2026-08-03，描述的是旧的"截屏监控"产品形态）。
以本文件与 STATUS.md 末节为准。

---

## 1. 当前状态（全部实测，附判据）

### 1.1 语音三项指标（云端模拟器，无真机）

| 指标 | 实测 | 判据 |
|---|---|---|
| 回复完整度/语速 | 同文本语速比 **0.91–1.04**（5 轮） | 修复前 0.36×（下行队列丢 38% 音频） |
| 流畅度 | 下行 `queue_drops_down = 0`、无 age-drop | 队列按"整段回复"配置（1500 帧/960000B/30000ms） |
| 打断质量 | 用户感知 **~171–310ms**；我们这层 `detect_ms` 31–47、`emit_stop_ms` 0 | 桥侧按 `reply_id` 测量（手机侧口径不可用，实测给 null） |

**一键复跑**：`./.venv/Scripts/python.exe scripts/sim/check-voice-quality-gate.py 1 1`
（需 Windows + Electron + TRTC 对端；**故意不进 CI**，ubuntu 跑不了媒体面）

### 1.2 控制面

- 兑付 `POST /api/v1/voice/internal/rtc-bridge/hello-redeem` → **ok=true redeemed=true**（用生产兑付客户端实测）
- 根因曾是**线上镜像 ≠ HEAD**（镜像是工作区快照，含中间件但不含后加的通配）⇒ **env-only 变更修不了缺代码**
- `VOICE_TRUSTED_GATEWAY_HOSTS` 现为 `0.0.0.0/0,::/0`（旧镜像支持 CIDR、不支持 `*`）

### 1.3 工作区 / CI / 门禁

| 项 | 状态 | 判据 |
|---|---|---|
| 工作区干净 | ✅ `git status --porcelain` **输出为空** | 未跟踪 130 → 0（`.gitignore` + 移出恢复材料，**非删除**） |
| 本地运行态脚本退役 | ✅ | 守卫测试 3 passed；12 个脚本 + watchdog wrapper 均不在 |
| bridge 纳入 CI | ✅ | 矩阵双服务 `jax-voice-api`(9000) / `jax-voice-bridge`(9200)，含部署前测试门禁 |
| 契约测试 | ✅ **313 passed, 1 skipped** | `pytest backend/tests/contract -q` |
| 契约有牙齿 | ✅ **13/13** 行为回退被捕获 | `scripts/mutation-check-contracts.py` |
| 发布拦路项 | **2 项**（原 3 项） | `scripts/check-release-blockers.py` |

---

## 2. 唯一 GO 路径

剩余 **2 项阻塞**，都是 P0 声明的 `EvidencePending`：

```
windows-popup-free    kind=windows-field  ← 需 Windows 现场取证
android-duplex-audio  kind=android-field  ← **需真机**
```

**三条命令**（前置：真机到位 + 已产出候选产物）：

```bash
cd C:/Users/Administrator/WorkBuddy/监视app

# 1) 记录证据（自动算 sha256 与 72h 时效，写盘前用治理层校验器验证；四条拒止路径）
./.venv/Scripts/python.exe scripts/record-release-evidence.py \
    --claim-id android-duplex-audio --kind android-field \
    --evidence <真机取证文件> --artifact <候选产物> \
    --owner <实现方> --reviewer <独立复核人必须不同>

# 2) 看阻断清单
./.venv/Scripts/python.exe scripts/check-release-blockers.py     # 退出码 0=就绪 1=阻塞 2=输入不可用

# 3) 权威判定（verify 需要 6 个必填参数；完整写法见 docs/governance/release-harness.md §运行命令）
./.venv/Scripts/python.exe scripts/release-preflight.py verify \
  --policy governance/release-policy.json \
  --claims governance/claims \
  --command-lock governance/command-lock.json \
  --repo-root . \
  --artifact-path <候选产物> \
  --evidence-root artifacts/release-evidence
```

⚠️ 证据约束：`expires_at ≤ now+72h`、绑定**当前 commit + 产物 sha256**、`reviewer ≠ owner`、证据文件哈希要对得上。
⚠️ **`record-release-evidence.py` 不评判证据真伪** —— 给它一个写着 PASS 的文件它照样写出形式合法声明。
**真正的控制点是那个独立复核人。**

---

## 3. 已知但**未处理**（不是阻塞，别和 §2 混）

1. **桌面端 pinned CA 指向本机开发 CA**：`pet-ui/src-tauri/src/main.rs` 把 `certs/ca.crt`（`CN=JaxPet Local CA` 自签）
   作为 `NODE_EXTRA_CA_CERTS`；而云端控制面是 **DigiCert** 链 ⇒ 该 pinning 对云端链路**形同虚设**。
   经**密码学验证**：`certs/cloud-control-plane-issuer.pem` 正是签发线上 leaf 的 issuer。
   改动牵动 installer / O-018 hook 与启动门禁 ⇒ **需真机验证后再动**。
   （口径：CA 安装是**用户点"同意"后**才执行的，不是静默行为。）
2. **bridge EnvParam 已用 93%**（4774/5120 字节，仅剩 346）。今天没超限，但再动一下就超。
   `scripts/check-cloud-env-budget.py` 可复核。腾余量两条路（都需重签证书）：PEM 改镜像内文件，或换 **ECDSA P-256** 客户端证书。
3. **流水线无回滚**：部署先于自证、失败不回滚（已在 workflow 顶部显式记为已知缺口 + 人工回退步骤）。
4. **磁盘**：98 条 CLEAN 的 **581.5 MB 独占**仍在磁盘上（只是不再出现在 git 状态）。robocopy 通道 ~1.2 MB/s。
5. **GitHub 侧 46 项配置来源**无法从仓库确认（30 走 `secrets.*`、4 走 `vars.*`）。
   ⚠️ `VOICE_DATABASE_URL` / `TRTC_SECRETKEY` / `QWEN_REALTIME_API_KEY` **必须放 Secrets**。

---

## 4. 工具清单

| 工具 | 用途 | 备注 |
|---|---|---|
| `scripts/check-release-blockers.py` | 打印发布拦路项 | **委托** `verify_claims`，不重写规则 |
| `scripts/record-release-evidence.py` | 记录 P0 证据 | 写盘前验证；4 条拒止路径 |
| `scripts/sim/check-voice-quality-gate.py` | 语音三项指标门禁 | 需真媒体面；前置不满足**拒绝运行**（exit 2） |
| `scripts/sim/run-sim-e2e.py` / `measure-rate-repeat.py` | 端到端模拟与重复测量 | 单进程编排（本平台会收割跨调用子进程） |
| `scripts/mutation-check-contracts.py` | 证明契约有牙齿 | 13 个变异，全部捕获 |
| `scripts/check-cloud-env-budget.py` | 云端 EnvParam 体积与必填键 | 读回真实值 |
| `scripts/inventory-untracked.py` | 未跟踪条目分类盘点 | **只读**；硬链接感知（表观/独占） |

---

## 5. 铁律（今天用真金白银换来的，接手者必须遵守）

1. **判"成功"必须绑定"本次新建"**。门禁曾 6 秒报"通过"（真实需 83 秒）—— 它读到了上一轮遗留的 JSON。
2. **测工具必须真跑它。** 只断言源码文本 = 没测。盘点工具就是这样"测试全绿、工具跑不起来"。
3. **变异结果只有在确认"守护测试真的跑了"之后才有意义**（选择子 0 命中 ≠ 没有守护）。
4. **同时扫 `contract` 与 `unit`**：守护会住在任意一侧。
5. **判服务是否正确看 `/health` 的载荷形态，不看状态码。** `jax-backend` 一直 200，但它是旧控制面。
6. **env-only 变更修不了缺代码**：线上镜像可能是工作区快照。
7. **任何重采样先低通再抽取**；**下行队列按"整段回复"配置**（帧龄过期只对上行成立）。
8. **"已交给节拍器" ≠ "用户听到的"**；跨进程/跨设备相减无意义（PC 与手机墙钟差 ~11s）。
9. **清理只看「独占」字节**（硬链接会重复计数；实测删表观 6.38 GB 只回收 1.53 GB）。
10. **改文件的工具不许用会翻译换行的 API**（`autocrlf=true` 下 `write_text` 会把 LF 变 CRLF，而 `git diff` 看不见）。
11. **禁止 `git stash`**；commit 后立即 `git rev-parse HEAD` + `git pack-refs --all`。

---

## 6. 如何自己验证（**不要相信本文档**）

```bash
cd C:/Users/Administrator/WorkBuddy/监视app
./.venv/Scripts/python.exe -m pytest backend/tests/contract -q          # 期望 313 passed, 1 skipped
./.venv/Scripts/python.exe scripts/mutation-check-contracts.py          # 期望 13/13
./.venv/Scripts/python.exe scripts/check-release-blockers.py            # 期望 2 项阻塞、工作区干净
git status --porcelain                                                  # 期望输出为空
git rev-parse HEAD                                                      # 记录基线
```

若上面任何一条与本文档不符 ⇒ **以实测为准**，并在本文件追加更正（只追加，不删历史）。
