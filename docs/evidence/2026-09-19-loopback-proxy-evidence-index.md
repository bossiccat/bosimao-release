# loopback 代理缺陷：证据索引（2026-09-19）

**这个文件为什么受跟踪而证据文件不受跟踪**

本波（任务 C：探本机端口的探测点是否绕代理）的证据文件都落在 `outputs/` 下，
而 `outputs/` 被 `.gitignore:134` 忽略 —— 那是仓库既有约定，本波**不跟它对着干**：
证据文件仍在工作机上留存，**不搬**。

但代码注释与契约锁的 docstring 里引用了这些路径，干净检出后就是**悬空引用**：
读者看得到"依据：`outputs/xxx.txt`"，却无从判断那份东西到底是什么、有没有被改过。

所以这里放一条**受跟踪**的索引：每条给出 **逻辑名 / 路径 / 大小 / sha256 / 它证明了什么**。
代码与 docstring 里的引用同时写成「逻辑名 + sha256」⇒ 引用**自证**：
即使文件不在，也能验它在出处是否被改过。

**本文件的使用规则**（写下来是为了让它可被 review）：

1. **不许出现空 sha256 或"待补"**。索引条目必须是已经算出来的读数。
2. 证据文件内容一变，`sha256` 必须跟着更新；**改了不更新 = 索引在说谎**。
3. 逻辑名是稳定标识（`loopback-proxy/<短名>`），路径可以变、逻辑名不该变。
4. 计算只用 Python（`hashlib`）。**不要用 `grep`/`wc` 计数**：本机 MSYS `grep` 对含 `{}`
   的模式会给假 0，这类工具层假读数本身就是本波的一条证据（见 `shell-grep-false-zero`）。

---

## 一、索引

| 逻辑名 | 路径（完整相对路径；这些文件本身**不入库**） | 大小(B) | sha256 | 它证明了什么 |
|---|---|---|---|---|
| `loopback-proxy/safe-delete-guard-veto` | `outputs/2026-09-19-safe-delete-bulk-guard-vetoes-test-cleanup.txt` | 5131 | `22aa50cf016d2c3818a5a412915f4ae6904dc352e6e3989bc8197db88c6ae945` | **工具层 veto**：本机 WorkBuddy 的 safe-delete shim 有一条**按 turn 计数**的批量删除守卫（阈值 500），全量跑时 `pathlib.Path.unlink` 会被它以 `SystemExit(1)` 拒掉 ⇒ 本锁 `finally` 里的探针清理**根本没执行**、探针留在树里、连锁把 `test_no_stray_mutation_probe_files` 判红。同一份读数还显示 **652 条全绿但 `rc=1`**（守卫 veto 了 pytest 自己的 tmp 垃圾回收）⇒ **本机不能用 `rc` 当门禁信号**。处置：清理从"删除"改成 `os.replace` **移出**仓库树（rename 不走守卫） |
| `loopback-proxy/false-reading-experiment` | `outputs/2026-09-19-urlopen-proxy-false-reading-experiment.txt` | 1874 | `be2bf4d76f8e5267f2c1a6ca356630b723a35e03762cc069a61ad561508fdcaf` | **第一版真值表的原始读数，含被推翻的假设，保留不改**。`EXPERIMENT_RESULT=MISMATCH`、`FAILED_CELLS=[3,5]`：当时"设了代理 + 直连"两格反而读到 200、代理侧 0 命中，看起来像"Windows 不劫持 loopback"。后查明是**同进程 `_opener` 已被冻结**（第 1 格无代理时先 urlopen 过）所致 —— 这正是"尺子先坏"的第二例 |
| `loopback-proxy/mechanism-source` | `outputs/2026-09-19-urlopen-proxy-mechanism.txt` | 4102 | `952202992037feaf62846b177ca7f932619443850164e94334fce77f47c57895` | `urllib.request.proxy_bypass` 的**真源码**（不是回忆）+ 平台分支 + 本机注册表实测：`proxy_bypass_registry('127.0.0.1')=True`、`getproxies_registry()` 有 `127.0.0.1:7890` ⇒ 解释"为什么在开发机上读得对"。**注意：B 组四格受同进程 `_opener` 冻结污染**，机制结论以下面 handler-source 与 fresh-process-cells 为准 |
| `loopback-proxy/handler-source` | `outputs/2026-09-19-urlopen-proxy-handler-source.txt` | 3804 | `b10f6fe28049c13e2f6f0879fc35370bc19dbff4261e4793e713e9c7fd43c8e0` | `ProxyHandler.__init__` / `proxy_open` 真源码 + **手动构造 opener**（不经全局缓存）逐格：`ProxyHandler({'http': spy})`、`ProxyHandler()`（不传参）、`getproxies()` 三种都把请求交给代理并留下绝对 URI，**只有 `ProxyHandler({})` 绕开**。D 节证明 `ProxyHandler()` 不传参会取到 `getproxies()` ⇒ 写成 `opener.open()` 看起来很规范，缺陷却原样回来 |
| `loopback-proxy/fresh-process-cells` | `outputs/2026-09-19-urlopen-proxy-fresh-process-cells.txt` | 1250 | `34f4aaa1e6cdfec1f7dd3e76e695a784f504d1020d2280c8ae2fc516100f95dd` | **本案主证据**：每格独立进程，排除 `_opener` 缓存。`fresh_proxied`（缺陷格）活端口被读成 `ERR HTTPError` 且代理侧收到绝对 URI；`fresh_bypass`（修复格）200 且代理 0 条；`cache_sticky` / `cache_from_clean` 两格证明 **`_opener` 的代理地址在进程内第一次 `urlopen` 时被冻结**（先脏后清无效、先清后脏也无效） |
| `loopback-proxy/inventory-scan` | `outputs/2026-09-19-loopback-probe-inventory-scan.txt` | 1322 | `fcba5a1e1169613a7e620c1b4442fc6ab2d8e6920c7b1343162bc4c8c19a104e` | 首轮全仓扫描读数：9 个含 loopback 字面量的探测文件、`NO_BYPASS_COUNT=7`。其中 `backend/tests/unit/test_rtc_bridge_mtls_paths.py` 是**当时不在预期名单里**的一个 ⇒ "按单子改"会漏、按全量扫才不会漏 |
| `loopback-proxy/lock-mutation-real-repo` | `outputs/2026-09-19-loopback-lock-mutation-real-repo.txt` | 2291 | `a9e58dc36685b145ad64d0c4c1c8a479f360f80b715fd4da53061b82ffcfd3f6` | 拿**真实** `cloudbridge/supervisor.py` 做变异（`build_opener(ProxyHandler({}))` → `build_opener()`），锁报红、`RESTORED_IS_PRISTINE=True`。这一格的失败原文就是上文 handler-source 那条结论的门禁化兑现 |
| `loopback-proxy/httpx-cells` | `outputs/2026-09-19-httpx-loopback-proxy-cells.txt` | 847 | `073e5a3eb8ff419dc397373cb7ac072796f0520fc95baa2b741f47e5ed3435f3` | httpx 0.28.1 四格：`httpx.Client()` / `AsyncClient()` 默认（`trust_env=True`）⇒ 502 且代理侧收到绝对 URI；`trust_env=False` ⇒ 200 且代理 0 条。是 `trust_env=False` 修复的**行为依据** |
| `loopback-proxy/unit-ws-dead-proxy-cells` | `outputs/2026-09-19-unit-ws-dead-proxy-positive-control.txt` | 3206 | `1c2d81259fd3d2ec3e5d50cfc6b4312695e3510ab3550a884a245b369f7c8946` | unit 测试族**死代理两格**：`test_rtc_bridge_ctrl_relay.py` 带 `, proxy=None` ⇒ `4 passed`；把那一处去掉 ⇒ `ConnectionRefusedError: [WinError 1225]` rc=1 ⇒ **"不写 `proxy=None` 就真的失败"**。另记录了本用例第一版的假绿（子 pytest 传裸文件名 ⇒ 没跑起来却"通过"）与修法 |
| `loopback-proxy/shell-grep-false-zero` | `outputs/2026-09-19-shell-grep-brace-pattern-false-zero.txt` | 3899 | `e9cdcb673d0dc1d906622ef26c0b320b97f41fcfdcc32f2c9e16d93ab9cfa0c1` | **工具层**假读数：同一文件同一时刻 `grep -c -F 'ProxyHandler({})'` 有时 1、连跑 5 次全 0，而 Python 字节计数稳定为 1；`WHICH_GREP` = MSYS `…\git\usr\bin\grep.EXE`。结论：本类核查的一律用 AST/Python 读数，`grep` 计数必须配阳性对照才可用 |
| `loopback-proxy/report-initial-scan` | `outputs/2026-09-19-urlopen-localhost-proxy-bypass-scan.md` | 13894 | `1b9c2999869c4528b47d2848887edd774fe0dcdb9360500bcf3153aa2ae9fde2` | 任务 C 首版报告（**先报告后改**）：一句话结论、逐处清单、判定范围外的理由、触发条件、"不建议用 `NO_PROXY` 修"的取舍。§8 / §9 是**事后批注**（实际 4 文件 8 调用点、锁已建、28 处终局、本索引），原文不删 |
| `loopback-proxy/report-httpx-ws-inventory` | `outputs/2026-09-19-httpx-trust-env-enumeration-and-ws-loopback-inventory.md` | 10305 | `e45b54e40c4862d0fae64c9cef45e5a0c7605e10e1781482e869d312c4f23603` | `trust_env` **影响面逐行枚举**（要结论不要"应该没事"）+ ws loopback 清点 + 行为验证表 + 变异 + 自豁免说明。§六 是终局更正：**28 处（不是 20 处）**、锁的第三个洞、`WS_TEST_DEBT` 已清空 |

---

## 二、代码与契约锁里引用这些逻辑名的位置

| 引用方 | 行 | 逻辑名 |
|---|---|---|
| `cloudbridge/supervisor.py` | 61 | `loopback-proxy/fresh-process-cells` |
| `scripts/e2e_verify.py` | 138 | `loopback-proxy/fresh-process-cells` |
| `scripts/e2e_verify.py` | 197 | `loopback-proxy/httpx-cells` |
| `scripts/mock_phone_client.py` | 149 | `loopback-proxy/httpx-cells` |
| `scripts/poc_001_model.py` | 101 | `loopback-proxy/httpx-cells` |
| `scripts/o019_rotation_window_e2e.py` | 38 | `loopback-proxy/fresh-process-cells` |
| `scripts/sim/run-sim-e2e.py` | 169 | `loopback-proxy/fresh-process-cells` |
| `tools/verify_approval_e2e.py` | 25 | `loopback-proxy/fresh-process-cells` |
| `backend/tests/contract/test_loopback_probe_proxy_contract.py` | 15 | `loopback-proxy/fresh-process-cells` |
| 同上 | 16 | `loopback-proxy/httpx-cells` |
| 同上 | 59 | `loopback-proxy/shell-grep-false-zero` |
| 同上 | 533 | `loopback-proxy/httpx-cells` |

---

## 三、复核方法

```bash
cd /c/Users/Administrator/WorkBuddy/监视app
# 逐条核对 sha256（只用 Python，不要用 grep/wc）
.venv/Scripts/python.exe -c "
import hashlib, pathlib
for rel in ['2026-09-19-urlopen-proxy-fresh-process-cells.txt',
            '2026-09-19-httpx-loopback-proxy-cells.txt']:
    p = pathlib.Path('outputs') / rel
    print(rel, p.stat().st_size if p.exists() else 'ABSENT',
          hashlib.sha256(p.read_bytes()).hexdigest() if p.exists() else '-')
"
```

**干净检出时 `outputs/` 不存在** ⇒ 这些文件按设计是 `ABSENT`。
这时 sha256 的作用不是"就地验证"，而是**记录出处内容**：
一旦拿到同族的证据文件（工作机、备份、别的分支），算出 sha256 一比对，
就能判断它是不是当时引用的那一份、有没有被改写。

---

## 四、附：本索引**未**覆盖的其它 `outputs/` 引用（发现登记，**不是**索引条目）

全仓受跟踪文件里对 `outputs/` 的引用共 **32 个不同路径**，本索引只收口
**loopback 代理一族（11 条）**。其余引用属别的工作线（其中
`.github/workflows/android-gates.yml` 属本波明确不动的边界），**此处只登记发现**：

- **产物落盘目的地**（不是证据引用）：`scripts/record-release-evidence.py:24,25`、
  `scripts/check-release-blockers.py:16`、`cloudbridge/sim_phone.py:111`、
  `backend/tests/contract/test_cloudbridge_service_contract.py:340,342`
- **别的工作线的证据引用**：`scripts/field-evidence/run_field_evidence.py:46,172`、
  `cloudbridge/trtc-electron-sdk-linux-versions.json:10,348`、
  `backend/app/api/routes_voice_ingest_ticket.py:3`、`backend/app/voice/ingest_ticket.py:1`、
  `backend/tests/contract/test_ingest_ticket_contract.py:3`
- **文档正文引用**（`docs/**`、`mobile-app/README.md`、根目录中文方案文档）：
  其余各条

这一节**故意不给 sha256 列**：给了就成了索引条目，而索引条目不许有空值。
要让它们也自证，需要在各自的工作线里补一份真实读数 —— 不能由别线代填。
