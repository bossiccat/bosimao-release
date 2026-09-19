"""变异校验：把今天修过的每处行为**故意改回去**，看对应契约测试是否变红。

为什么需要这个
--------------
契约测试的数量（296）不是质量。真正的质量问题是：**行为被破坏时它会不会红？**
本项目已多次踩到"测试有形状但没有牙齿"（例：`test_barge_in_flush_contract.py` 初版
三个用例全是 `assert "flush_downlink" in source` 的字符串扫描）。静态扫描能防"退回去"，
但证明不了行为正确 —— 而这正是"看起来通过、其实没验证"的来源。

做法：对每处改动施加一个**语义上真实的回退**，跑它的守护测试，期望**变红**。
不变红 = 该契约无区分力，必须如实记下来（不是"以后再说"）。

铁律：每个变异跑完**必须恢复**，并逐字节校验文件已还原。
"""
from __future__ import annotations

import hashlib
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PY = sys.executable

# (说明, 目标文件, 原文, 变异后, 期望变红的测试选择子)
MUTATIONS: list[tuple[str, str, str, str, str]] = [
    ("下行队列预算退回 200 帧 / 1000ms 帧龄",
     "backend/rtc_bridge/config.py",
     "down_max_frames: int = 1500", "down_max_frames: int = 200",
     "downlink_budget or downlink_queue"),

    ("下行帧龄上限退回 1000ms",
     "backend/rtc_bridge/config.py",
     "down_max_frame_age_ms: int = 30000", "down_max_frame_age_ms: int = 1000",
     "downlink_budget"),

    ("24k→16k 退回朴素抽取（去掉抗混叠低通）",
     "backend/app/voice/qwen_realtime_bridge.py",
     'filt = np.convolve(buf, _PCM16K_TAPS_ARR, mode="valid")',
     "filt = buf[(_PCM16K_TAPS - 1):]   # MUTATED: 无低通，退化为朴素抽取",
     "qwen_pcm_resample"),

    ("cloudapi 去掉受信网关中间件装配",
     "cloudapi/main.py",
     "app.add_middleware(\n    TrustedGatewayIdentityMiddleware,",
     "pass  # mutated: middleware removed\n_unused = (TrustedGatewayIdentityMiddleware,",
     "cloudapi_hello_wiring"),

    ("trusted_gateway 去掉 * 通配放行",
     "backend/app/voice/trusted_gateway.py",
     "_allow_any_source:\n            return True",
     "_allow_any_source:\n            return False  # MUTATED: '*' 放行被关掉",
     "trusted_gateway_source_policy"),

    ("桌面端 sign-url 指回旧控制面 jax-backend",
     "pet-ui/src-tauri/src/main.rs",
     'const DEFAULT_CONTROL_PLANE_URL: &str =\n    "https://jax-voice-api-283963-7-1436773060.sh.run.tcloudbase.com";',
     'const DEFAULT_CONTROL_PLANE_URL: &str =\n    "https://jax-backend-283963-7-1436773060.sh.run.tcloudbase.com";',
     "control_plane_endpoint"),
    # ---- 更早的关键行为（2026-09-14 扩展：让"有没有牙齿"成为例行度量）----
    ("打断不再冲刷 sidecar pacer（去掉 flush_downlink）",
     "backend/rtc_bridge/session.py",
     'await self._send_msg({"type": MSG_CTRL, "action": "flush_downlink"})',
     'pass  # MUTATED: 打断不再冲刷 sidecar pacer',
     "barge_in_flush"),

    ("播放期上行门控不再丢弃（去掉 continue ⇒ 回声会喂给云端）",
     "backend/rtc_bridge/session.py",
     'self.stats["up_gated_playback"] = self.stats.get("up_gated_playback", 0) + 1\n                continue',
     'self.stats["up_gated_playback"] = self.stats.get("up_gated_playback", 0) + 1  # MUTATED',
     "gate or barge or up_q or consume"),

    ("打断不再请求模型取消 response（payload 换掉）",
     "backend/app/voice/qwen_realtime_bridge.py",
     'payload = {"type": "response.cancel"}',
     'payload = {"type": "noop"}  # MUTATED',
     "barge_in_response_cancel"),

    ("下行帧龄丢弃失效（阈值放大 1000 倍 ⇒ 几乎不再丢）",
     "backend/rtc_bridge/bounded_audio_queue.py",
     "while self._entries and (now - self._entries[0].created_at) * 1000.0 > limit_ms:",
     "while self._entries and (now - self._entries[0].created_at) * 1000.0 > limit_ms * 1000:  # MUTATED",
     "queue or attribution"),

    ("hello 门禁去掉 certificate_binding 比对",
     "backend/app/api/routes_voice_hello.py",
     "or certificate != expected_certificate_binding):",
     "or False):  # MUTATED: 不再比对 certificate_binding",
     "cloudapi_hello_wiring"),

    ("兑付错误码不再分类（恒 n/a ⇒ 无法归因）",
     "backend/rtc_bridge/redemption.py",
     'code = str((response.json() or {}).get("code", "n/a"))',
     'code = "n/a"  # MUTATED',
     "commercial or redemption or redeem or error"),
    ("发布拦路项检查器硬编码放行（不再委托 verify_claims）",
     "scripts/check-release-blockers.py",
     "    result = verify_claims(\n        policy=policy,",
     '    result = {"verdict": "pass", "errors": []}  # MUTATED: 硬编码放行\n    _ignored = verify_claims(\n        policy=policy,',
     "release_blockers"),

    # ---- 2026-09-19 PE 子系统门禁（claim windows-popup-free）----
    # 这个脚本此前是**假门禁**：--expect-gui 是装饰参数、扫描非递归、没有机器标记，
    # 于是"顶层 5 个 exe 全 GUI"被当成"产品无 CUI"。下面三个变异分别把三类缺陷
    # 退回去，守护测试 backend/tests/contract/test_pe_subsystem_gate_contract.py
    # 必须变红（隔离树实测 3/3 捕获）。
    ("PE 门禁判定恒真（永远 PASS ⇒ 门禁无法变红）",
     "scripts/pe-subsystem-verify.py",
     "    if non_gui == 0:",
     "    if True:  # MUTATED: 永远 PASS",
     "pe_subsystem_gate"),

    ("PE 排除清单退回深度不限（会把随包 resources 的 CUI 一起剪掉 ⇒ 假绿）",
     "scripts/pe-subsystem-verify.py",
     "if prune_root_children and at_root and d in CARGO_ARTIFACT_DIRS:",
     "if prune_root_children and d in CARGO_ARTIFACT_DIRS:  # MUTATED",
     "pe_subsystem_gate"),

    ("PE 模式检查被去掉（--expect-gui 退回装饰参数 ⇒ 输入口径不设防）",
     "scripts/pe-subsystem-verify.py",
     "    if args.expect_gui == args.report_only:",
     "    if False:  # MUTATED: 模式不再必需，退回初版装饰参数",
     "pe_subsystem_gate"),

    # ---- legacy watchdog 计划任务清理的静态锁（同一 claim）----
    # 清理逻辑在 pet-ui/src-tauri/installer/o018-installer-hooks.nsh，由
    # scripts/verify-o018-installer-contract.py 的 5 条新 check 锁住。
    ("安装器 legacy 清理改回 NSIS 内建 Exec（会给 CUI 子进程分配可见控制台）",
     "pet-ui/src-tauri/installer/o018-installer-hooks.nsh",
     "nsExec::ExecToLog '\"$SYSDIR", "Exec '\"$SYSDIR",
     "o018_installer_contract"),

    ("安装器 legacy 清理挪到 fail-closed Abort 之后（安装中止时不再清理）",
     "pet-ui/src-tauri/installer/o018-installer-hooks.nsh",
     '  !insertmacro JAX_LEGACY_WATCHDOG_TASK_CLEANUP\n\n'
     '  DetailPrint "Provisioning sidecar credential (O-018 slice 2)..."',
     '  DetailPrint "Provisioning sidecar credential (O-018 slice 2)..."',
     "o018_installer_contract"),

    # ---- 2026-09-19 PE 信任判据（发布路径上的生产可信门）----
    # isPeBinary 初版只读前 2 字节判 "MZ"，实测 40,000 字节 0x41 + 开头 MZ 就能过，
    # 即注释里声称的 "PE provenance" 实际只验了魔数。守护住在
    # scripts/test/sidecar-trust-pe.test.js（node 套件），由
    # backend/tests/contract/test_sidecar_trust_pe_contract.py 拉起 —— 少了这座桥
    # 那些 node 用例不在任何 workflow 的收集范围内，等于没跑。
    ("PE 信任判据退回「只看得到 MZ 就算 PE」（4MB 任意 blob 又能通过）",
     "scripts/lib/sidecar-trust.js",
     "    return magic === OPTIONAL_MAGIC_PE32 || magic === OPTIONAL_MAGIC_PE32_PLUS;",
     '    return true;  // MUTATED: 退回「只看得到 MZ 就算 PE」',
     "sidecar_trust_pe_contract"),

    ("sidecar 源码闭集漏登记一个被 require 的文件（随包 app 运行期会 require 失败）",
     "scripts/lib/sidecar-package-common.js",
     "  'main.js', 'phone.js', 'resample.js', 'rtc-startup.js', 'rtc-termination.js',",
     "  'main.js', 'phone.js', 'rtc-startup.js', 'rtc-termination.js',  # MUTATED",
     "sidecar_trust_pe_contract"),

    # ---- 2026-09-19 原生闭集的跨源不变式锁 ----
    # 同一份"原生集 5 个名字"有 5 份副本，其中 4 份在生产路径上（JS 构建期 ×2、
    # Rust 启动期 ×2，跨语言）。此前互无锁：改了 JS 忘了改 Rust 的后果不是构建失败，
    # 而是装机后 sidecar 拒绝 spawn（ManifestInvalid / RuntimeUntrusted），CI 全程不可见。
    # 守护是 scripts/test/sidecar-package.test.js 的
    # "the native closed set is one set across every production copy"，由
    # backend/tests/contract/test_sidecar_trust_pe_contract.py 拉起。
    ("原生集只在 sidecar-trust.js 少一个名字（构建期可信门悄悄少验一个文件）",
     "scripts/lib/sidecar-trust.js",
     "  'liteav_media_server.exe',\n", "",
     "sidecar_trust_pe_contract"),

    ("原生集只在 sidecar-package-common.js 少一个名字（provenance 哈希覆盖集缩水）",
     "scripts/lib/sidecar-package-common.js",
     "  'liteav_media_server.exe',\n", "",
     "sidecar_trust_pe_contract"),

    ("原生集只在 Rust 启动期可信门少一个名字（JS/Rust 漂移，构建期完全看不见）",
     "pet-ui/src-tauri/src/sidecar_runtime_trust.rs",
     '    "liteav_media_server.exe",\n', "",
     "sidecar_trust_pe_contract"),

    ("原生集只在 Rust 启动期精确集合相等少一个名字（REQUIRED 与 manifest 不一致）",
     "pet-ui/src-tauri/src/sidecar_integrity.rs",
     '        "resources/app/node_modules/trtc-electron-sdk/build/Release/liteav_media_server.exe",\n', "",
     "sidecar_trust_pe_contract"),

    # ---- 2026-09-19 可信门策略版本（trust_version）----
    # 策略版本写进 provenance manifest 并比对，是为了让"策略变了"成为机械后果：
    # 按旧策略构建的 generation 即使还在磁盘上也会校验失败、强制重建。
    # 守护住在 scripts/test/sidecar-trust-pe.test.js + scripts/test/sidecar-package.test.js。
    ("策略版本比对被废（旧策略的 generation 又能通过生产可信门）",
     "scripts/lib/sidecar-trust.js",
     "  if (declared !== TRUST_VERSION) fail('SIDECAR_PACKAGE_TRUST_VERSION_MISMATCH');",
     "  if (false) fail('SIDECAR_PACKAGE_TRUST_VERSION_MISMATCH');  // MUTATED: 比对被废",
     "sidecar_trust_pe_contract"),

    ("只改 JS 侧策略版本（Rust 启动期门禁的常量没跟着改 ⇒ 跨语言漂移）",
     "scripts/lib/sidecar-trust.js",
     "const TRUST_VERSION = '1.0.0';",
     "const TRUST_VERSION = '1.1.0';  // MUTATED: 只改 JS 侧",
     "sidecar_trust_pe_contract"),

    # ---- 2026-09-19 跨语言 PE 判据锁（JS 构建期 isPeBinary ↔ Rust 启动期 is_pe_binary）----
    # `sidecar_runtime_trust.rs` 的头注释声称"与 scripts/lib/sidecar-trust.js 同一策略"，
    # 而实测那句话一度是**假的**：Rust 侧只判 2 字节 MZ，且它跑在每次启动、全部客户机上。
    # 守护是 scripts/test/sidecar-package.test.js 的
    # "the Rust startup PE judgement is the same structural judgement as the JS one"。
    #
    # ⚠️ 覆盖边界（必须如实说明）：这条 pytest 桥**跑不到 Rust 的行为牙齿** ——
    # "MZ + 40,000 字节填充必须判否""真实 172MB 产物必须放行"住在 Rust 单元测试里，
    # 而没有任何 workflow 跑 `cargo test`。下面三个变异只能证明**判据形状/常量值**的锁有牙齿；
    # Rust 行为侧的变异另由临时脚本验证（见本轮报告）。
    ("Rust 启动期 PE 签名常量被改（两侧判据不再同口径）",
     "pet-ui/src-tauri/src/sidecar_runtime_trust.rs",
     "const PE_SIGNATURE: u32 = 0x0000_4550;",
     "const PE_SIGNATURE: u32 = 0x0000_4551;  // MUTATED: 只改 Rust 侧",
     "sidecar_trust_pe_contract"),

    ("Rust 启动期 PE 判据收窄成只认 PE32（丢掉 PE32+ ⇒ 全部真实产物会被判否）",
     "pet-ui/src-tauri/src/sidecar_runtime_trust.rs",
     "    magic == OPTIONAL_MAGIC_PE32 || magic == OPTIONAL_MAGIC_PE32_PLUS",
     "    magic == OPTIONAL_MAGIC_PE32  // MUTATED: 丢掉 PE32+",
     "sidecar_trust_pe_contract"),

    ("subsystem 被塞进 Rust 的「是不是 PE」事实判据（策略混入）",
     "pet-ui/src-tauri/src/sidecar_runtime_trust.rs",
     "    magic == OPTIONAL_MAGIC_PE32 || magic == OPTIONAL_MAGIC_PE32_PLUS\n}",
     "    let subsystem = magic;  // MUTATED: 策略混入事实判断\n"
     "    magic == OPTIONAL_MAGIC_PE32 || magic == OPTIONAL_MAGIC_PE32_PLUS\n}",
     "sidecar_trust_pe_contract"),
]


def _apply_mutation(path: Path, old: str, new: str) -> tuple[bytes, bool]:
    """施加变异；**按字节**保留原始行尾，返回 (原始字节, 是否命中锚点)。

    为什么必须走字节：`Path.read_text/write_text` 会做换行翻译（本仓 core.autocrlf=true，
    i/lf 而 w/crlf），于是"读出来再写回去"会把**原本 LF 的文件变成 CRLF** ⇒ 恢复不忠实。
    实测就这么踩了一次（main.rs 由 LF 被改成 CRLF，且脚本因此在第一个变异后就中止，
    一个结果都没拿到）。做法：读原始字节 → 归一化为 LF 匹配替换 → 按原换行风格写回；
    恢复时直接写回原始字节，保证逐字节一致。
    """
    raw = path.read_bytes()
    crlf = b"\r\n" in raw
    text = raw.decode("utf-8").replace("\r\n", "\n")
    if old not in text:
        return raw, False
    mutated = text.replace(old, new, 1)
    path.write_bytes((mutated.replace("\n", "\r\n") if crlf else mutated).encode("utf-8"))
    return raw, True


def main() -> int:
    results: list[tuple[str, bool, str]] = []
    for desc, rel, old, new, sel in MUTATIONS:
        path = ROOT / rel
        raw, hit = _apply_mutation(path, old, new)
        if not hit:
            results.append((desc, False, "❌ 变异锚点未命中（原文不匹配）——校验本身失效"))
            continue
        try:
            proc = subprocess.run(
                # 必须同时扫 contract **与** unit：守护会住在任意一侧。
                # 实测踩过——只跑 contract 时，"上行播放期门控"与"下行帧龄丢弃"的守护
                # （test_uplink_playback_gating.py / test_queue_attribution.py 在 unit/）
                # 完全不可见，于是被误报成"契约无区分力"，白白得出 3 个假缺口。
                [PY, "-m", "pytest", "backend/tests/contract", "backend/tests/unit",
                 "-q", "-k", sel, "--no-header", "-x"],
                cwd=str(ROOT), capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=600)
            if proc.returncode == 5:      # pytest: no tests collected
                results.append((desc, False, "⚠️ 选择子未命中任何用例（测试根本没跑）"))
                continue
            caught = proc.returncode != 0
            tail = [l for l in proc.stdout.splitlines() if "passed" in l or "failed" in l
                    or "error" in l][-1:] or ["(无摘要)"]
            # 选择子命中为空同样算"没牙齿"：测试根本没跑
            if "no tests ran" in proc.stdout or "deselected" in proc.stdout and not caught:
                caught = False
                tail = ["⚠️ 选择子未命中任何用例"]
            results.append((desc, caught, tail[0][:150]))
        finally:
            path.write_bytes(raw)          # 逐字节恢复
            if path.read_bytes() != raw:
                print(f"!! 恢复失败（字节不一致），必须手工处理: {rel}")
                return 3
    print("\n============ 变异校验结果 ============")
    bad = 0
    for desc, caught, note in results:
        mark = "✅ 捕获" if caught else "❌ 未捕获（契约无区分力）"
        if not caught:
            bad += 1
        print(f"  {mark}  {desc}\n        {note}")
    print(f"\n{len(results) - bad}/{len(results)} 处行为回退能被契约测试捕获")
    return 0 if bad == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
