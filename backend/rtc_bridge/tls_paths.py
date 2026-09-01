"""控制面/Brain 的 TLS 凭据路径解析（mojibake-proof）。

背景（现场实锤 2026-09-01 rtc_bridge.log.err）：`.env` 为 UTF-8 无 BOM，
其中 4 条证书路径含中文段（SSL_CERT_FILE / RTC_BRIDGE_CONTROL_PLANE_CA_FILE /
RTC_BRIDGE_CLIENT_CERT_FILE / RTC_BRIDGE_CLIENT_KEY_FILE）。Windows PS5.1 的
Load-Env 按 GBK 解码，路径中的非 ASCII 段被误解码（"监视app" → "鐩戣…"），
env/cfg 里的路径字符串因此指向磁盘上不存在的目录。

根因已由 640726d 修好（Load-Env 显式 -Encoding UTF8），但注入链路上的任何
一次编码回退都会让路径再次失效，且失效形态是"字符串非空但文件不存在"——
`if not value` 这类守卫查不出来，只会在 ssl context 构建阶段炸
（FileNotFoundError）或被上层 except 吞掉静默降级。

统一契约（三段语义，与 244552c 的 `_resolve_brain_ca_file` 一致）：
    ① 调用方给出的候选路径（来自 env 或 cfg）依次优先；
    ② 逐个做存在性校验（`Path.is_file()`），不存在即跳过（OSError 亦跳过）；
    ③ 全部失效 → 回退仓库相对 `certs/<default_filename>`（相对 `__file__`
       解析，天然免疫编码问题）。

只依赖 stdlib，供 rtc_bridge 内所有 TLS 客户端（Brain 回调 / ack_reporter /
redemption）共用，避免三处各写一版兜底逻辑。
"""
from __future__ import annotations

from pathlib import Path

# backend/rtc_bridge/tls_paths.py → parents[0]=rtc_bridge, [1]=backend, [2]=仓库根
REPO_ROOT = Path(__file__).resolve().parents[2]
REPO_CERTS_DIR = REPO_ROOT / "certs"


def resolve_tls_file(default_filename: str, *candidates: str) -> str:
    """解析一个 TLS 凭据文件的真实路径。

    `candidates` 为调用方按优先级给出的候选路径（通常是 env/cfg 的裸值，
    可能为空或乱码）；`default_filename` 是仓库 `certs/` 下的兜底文件名。

    返回首个存在的路径；全部不可得时返回空串（由调用方决定降级或报错）。
    """
    ordered = [c for c in candidates if c and c.strip()]
    ordered.append(str(REPO_CERTS_DIR / default_filename))
    for candidate in ordered:
        try:
            if Path(candidate).is_file():
                return candidate
        except OSError:
            continue
    return ""
