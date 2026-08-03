# PoC-001: 模型视觉推理延迟压测（风险①）
# 前提：模型已下载、Comni/llama-omni-server 已启动 (:19080)
# 用法:
#   python scripts/poc_001_model.py <截图目录或文件>
#   python scripts/poc_001_model.py --json <截图目录或文件>
# 退出码: 0=PASS, 1=FAIL 或输入非法, 2=运行时错误

import argparse
import asyncio
import json
import statistics
import subprocess
import sys
import time
from pathlib import Path

import httpx

BASE = "http://127.0.0.1:19080"
PROMPT = """观察这张 AI 编程工具截图，判断工作状态，只输出 JSON：
{"state": "progress"|"stuck"|"off_track"|"unknown", "summary": "中文摘要"}"""


def gpu_mem_used_mb() -> int | None:
    """nvidia-smi 查询当前显存占用(MB)；失败返回 None。"""
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=10,
        )
        if out.returncode != 0:
            return None
        first = out.stdout.strip().splitlines()[0].strip()
        return int(first)
    except Exception:  # noqa: BLE001
        return None


async def analyze_one(client: httpx.AsyncClient, img: Path) -> dict:
    t0 = time.perf_counter()
    r1 = await client.post(f"{BASE}/v1/stream/prefill", json={"img_path_prefix": str(img), "cnt": 1})
    r1.raise_for_status()
    t1 = time.perf_counter()

    # decode 流式：记录首 token（首个 chunk 到达）与完整耗时
    first_token_ms = None
    decode_ms = None
    out_text = ""
    async with client.stream(
        "POST", f"{BASE}/v1/stream/decode",
        json={"stream": True, "max_tokens": 256, "prompt": PROMPT},
    ) as r2:
        r2.raise_for_status()
        async for chunk in r2.aiter_text():
            if first_token_ms is None:
                first_token_ms = (time.perf_counter() - t1) * 1000
            out_text += chunk
    t2 = time.perf_counter()
    decode_ms = (t2 - t1) * 1000

    return {
        "image": img.name,
        "prefill_ms": round((t1 - t0) * 1000),
        "first_token_ms": round(first_token_ms) if first_token_ms is not None else None,
        "decode_ms": round(decode_ms),
        "total_ms": round((t2 - t0) * 1000),
        "output": out_text[:120],
    }


def pct(sorted_vals: list, p: float) -> float:
    if not sorted_vals:
        return 0.0
    idx = min(len(sorted_vals) - 1, int(len(sorted_vals) * p))
    return sorted_vals[idx]


async def main(path: str, json_out: bool) -> int:
    p = Path(path)
    if not p.exists():
        print(f"[错误] 截图路径不存在: {p}", file=sys.stderr)
        return 1

    if p.is_dir():
        images = [i for i in sorted(p.iterdir()) if i.suffix.lower() in (".png", ".jpg", ".jpeg")][:10]
    else:
        images = [p] if p.suffix.lower() in (".png", ".jpg", ".jpeg") else []

    if not images:
        print(f"[错误] 截图目录/文件为空或无图片: {p}", file=sys.stderr)
        return 1

    mem_before = gpu_mem_used_mb()
    mem_peak = mem_before
    results = []
    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            for img in images:
                results.append(await analyze_one(client, img))
                mem_now = gpu_mem_used_mb()
                if mem_now is not None:
                    mem_peak = max(mem_peak or 0, mem_now)
    except httpx.HTTPError as e:
        print(f"[错误] 模型服务不可达（确认 :19080 已启动）: {e}", file=sys.stderr)
        return 2

    totals = sorted(r["total_ms"] for r in results)
    prefills = sorted(r["prefill_ms"] for r in results)
    firsts = sorted(r["first_token_ms"] for r in results if r["first_token_ms"] is not None)
    decodes = sorted(r["decode_ms"] for r in results)

    stats = {
        "count": len(results),
        "prefill_p50": round(pct(prefills, 0.50)),
        "prefill_p95": round(pct(prefills, 0.95)),
        "first_token_p50": round(pct(firsts, 0.50)) if firsts else None,
        "first_token_p95": round(pct(firsts, 0.95)) if firsts else None,
        "decode_p50": round(pct(decodes, 0.50)),
        "decode_p95": round(pct(decodes, 0.95)),
        "total_p50": round(pct(totals, 0.50)),
        "total_p95": round(pct(totals, 0.95)),
        "total_max": max(totals),
        "gpu_mem_peak_mb": mem_peak,
    }
    ok = stats["total_p50"] <= 4000

    if json_out:
        print(json.dumps({"verdict": "PASS" if ok else "FAIL", "stats": stats, "samples": results},
                         ensure_ascii=False, indent=2))
    else:
        print(f"\n=== PoC-001 结果: {len(results)} 张 ===")
        for r in results:
            ft = f"{r['first_token_ms']}ms" if r["first_token_ms"] is not None else "n/a"
            print(f"  {r['image']}: prefill={r['prefill_ms']}ms first={ft} total={r['total_ms']}ms")
        print(f"  prefill  P50={stats['prefill_p50']}ms  P95={stats['prefill_p95']}ms")
        print(f"  首token  P50={stats['first_token_p50']}ms  P95={stats['first_token_p95']}ms")
        print(f"  total    P50={stats['total_p50']}ms  P95={stats['total_p95']}ms  max={stats['total_max']}ms")
        print(f"  显存峰值 {stats['gpu_mem_peak_mb'] if stats['gpu_mem_peak_mb'] is not None else 'n/a'} MB")
        print(f"\n判定: {'PASS (P50 <= 4000ms)' if ok else 'FAIL (P50 > 4000ms)'}")
    return 0 if ok else 1


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="PoC-001 模型视觉推理延迟压测")
    ap.add_argument("path", nargs="?", default="tmp/captures", help="截图目录或单张图片路径")
    ap.add_argument("--json", action="store_true", help="输出 JSON 便于报告引用")
    args = ap.parse_args()
    sys.exit(asyncio.run(main(args.path, args.json)))
