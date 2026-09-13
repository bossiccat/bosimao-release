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
                [PY, "-m", "pytest", "backend/tests/contract", "-q", "-k", sel,
                 "--no-header", "-x"],
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
