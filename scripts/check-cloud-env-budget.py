"""发布前检查：云端 EnvParam 实际大小 vs 平台 5KB 上限 + CI 必填键齐备度。

为什么必须实测
--------------
审计提出「EnvParam 有 5KB 上限，而 CI 往里面塞 4 份 PEM，累加**极可能**触顶」。
这是可判定的：读回线上真实 EnvParams，按 CI 的提交口径（`json.dumps` 后的字符串长度）
量一次就知道。**不要靠估。**

不打印任何值：只打印键名、长度、布尔、哈希前 12 位。
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

ENV_ID = "jinhong-d2g55ycl591208475"
LIMIT_BYTES = 5 * 1024
WARN_RATIO = 0.85   # 余量告急：一次编辑就可能顶破，视为发布风险
ROOT = Path(__file__).resolve().parents[1]  # scripts/ 的上一级 = 仓库根
WORKFLOW = ROOT / ".github" / "workflows" / "deploy-cloudrun.yml"


def tcb_api(action: str, body: dict) -> dict:
    # Windows：`tcb` 是 .CMD 包装器，Python 的 subprocess 不解析 PATHEXT，
    # 直接传 "tcb" 会 FileNotFoundError（从 bash 手敲却能跑，所以极易误判成"没登录"）。
    exe = shutil.which("tcb") or "tcb"
    argv = [exe, "api", "tcbr", action, "--api-version", "2022-02-17",
            "--body", json.dumps(body), "--json"]
    proc = subprocess.run(
        argv, capture_output=True, text=True, encoding="utf-8", errors="replace",
        shell=exe.lower().endswith((".cmd", ".bat")),
    )
    out = (proc.stdout or "").strip()
    i = out.find("{")
    if i < 0:
        raise RuntimeError(f"{action} 无 JSON 输出: {out[:200]} {proc.stderr[:200]}")
    return json.loads(out[i:])


def serv(service: str) -> dict:
    data = tcb_api("DescribeCloudRunServerDetail",
                   {"EnvId": ENV_ID, "ServerName": service})["data"]
    raw = (data.get("ServerConfig") or {}).get("EnvParams") or "{}"
    env = json.loads(raw) if isinstance(raw, str) else dict(raw)
    versions = data.get("OnlineVersionInfos") or []
    return {"env": env, "version": (versions[0].get("ServerName") if versions
                                    else data.get("ServerName")),
            "image": (versions[0].get("ImageUrl") if versions else "")}


def required_keys(service: str) -> list[str]:
    """从 workflow 矩阵里抽该服务的 required_env（键名清单）。

    必须按**缩进**收敛取值范围：YAML 折叠标量（`>-`）会一直吃到下一次缩进回退为止，
    用「连续非空行」当边界会越过 required_env 吞掉后面的 `env:` / `steps:` 整段
    （实测把 255 个 token 当成"必填键"，其中 243 个是 YAML 噪声）。
    """
    lines = WORKFLOW.read_text(encoding="utf-8").splitlines()
    blocks: list[list[str]] = []
    cur: list[str] | None = None
    for line in lines:
        if re.match(r"\s*-\s*service:\s*", line):
            cur = [line]
            blocks.append(cur)
        elif cur is not None:
            cur.append(line)

    for block in blocks:
        if block[0].split("service:", 1)[1].strip() != service:
            continue
        for i, line in enumerate(block):
            m = re.match(r"^(\s*)required_env:\s*>-\s*$", line)
            if not m:
                continue
            indent = len(m.group(1))
            keys: list[str] = []
            for nxt in block[i + 1:]:
                if not nxt.strip():
                    continue
                if len(nxt) - len(nxt.lstrip()) <= indent:
                    break          # 缩进回退 = 折叠标量结束
                keys.extend(nxt.split())
            return keys
    return []


def declared_sources() -> list[tuple[str, str]]:
    """workflow job env 里声明的 键 -> 来源通道（secrets.X || vars.X）。"""
    wf = WORKFLOW.read_text(encoding="utf-8")
    pairs: list[tuple[str, str]] = []
    for line in wf.splitlines():
        m = re.match(r"\s*([A-Z][A-Z0-9_]{2,}):\s*(\$\{\{\s*.*?\s*\}\})\s*$", line)
        if m:
            expr = m.group(2)
            chan = ("secrets" if "secrets." in expr else "vars" if "vars." in expr
                    else "other")
            pairs.append((m.group(1), chan))
    return pairs


def main() -> int:
    ok = True
    for service in ("jax-voice-api", "jax-voice-bridge"):
        try:
            s = serv(service)
        except Exception as exc:  # noqa: BLE001
            print(f"{service}: 读回失败 {type(exc).__name__}: {str(exc)[:160]}")
            ok = False
            continue

        env = s["env"]
        payload = json.dumps(env)                 # CI 实际提交的形态（整表覆盖）
        n = len(payload.encode("utf-8"))
        req = required_keys(service)
        missing = [k for k in req if k not in env or not str(env[k]).strip()]

        print(f"\n===== {service} =====")
        print(f"  线上镜像 tag  : {s['image'].rsplit(':', 1)[-1][:40] or '(未取到)'}")
        print(f"  EnvParam 键数 : {len(env)}")
        ratio = n / LIMIT_BYTES
        if n > LIMIT_BYTES:
            verdict = "❌ 超限：平台会拒绝本次 EnvParam 写入（报错原文「环境变量长度超长，最长支持5kb」）"
            ok = False
        elif ratio >= WARN_RATIO:
            verdict = (f"⚠️ 余量告急（仅剩 {LIMIT_BYTES - n} 字节）：再追加一个密钥，"
                       f"或把客户端证书换成更大的密钥，部署就会失败")
        else:
            verdict = "OK"
        print(f"  提交字节数    : {n} / 上限 {LIMIT_BYTES}"
              f"（余量 {LIMIT_BYTES - n}，占用 {ratio * 100:.0f}%）")
        print(f"  判定          : {verdict}")
        print(f"  CI 必填键 {len(req)} 个，缺失 {len(missing)}"
              f"{'：' + ' '.join(missing) if missing else '（齐备）'}")
        # 大体积键（PEM/DSN 等）：只报键名与长度
        big = sorted(((k, len(str(v))) for k, v in env.items()), key=lambda x: -x[1])[:8]
        print("  体积最大的键（前 8，只报长度）:")
        for k, ln in big:
            print(f"    {k:42} {ln:>6} chars")
        pem = [k for k, v in env.items() if "PRIVATE KEY" in str(v) or "CERTIFICATE" in str(v)]
        print(f"  含 PEM 材料的键 {len(pem)} 个: {' '.join(pem) if pem else '(无)'}")
        if missing:
            ok = False

    srcs = declared_sources()
    sec = [k for k, c in srcs if c == "secrets"]
    var = [k for k, c in srcs if c == "vars"]
    print(f"\n===== CI 声明的配置来源（需在 GitHub 侧确认，共 {len(srcs)} 项）=====")
    print(f"  走 secrets.*: {len(sec)} 项 -> {' '.join(sec) if sec else '(无)'}")
    print(f"  走 vars.*   : {len(var)} 项 -> {' '.join(var) if var else '(无)'}")
    print("\n⚠️ 注意：VOICE_DATABASE_URL 含 PG 口令、QWEN_REALTIME_API_KEY / "
          "TRTC_SECRETKEY 是明文机密 —— 放 vars.* 会在 Actions 日志与 API 里明文回显，"
          "必须放 secrets.*。上面列出的通道需要人工在 GitHub 侧核对。")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
