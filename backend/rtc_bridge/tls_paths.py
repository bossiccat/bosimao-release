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

import logging
from pathlib import Path

logger = logging.getLogger(__name__)

# backend/rtc_bridge/tls_paths.py → parents[0]=rtc_bridge, [1]=backend, [2]=仓库根
REPO_ROOT = Path(__file__).resolve().parents[2]
REPO_CERTS_DIR = REPO_ROOT / "certs"


def resolve_tls_file(default_filename: str, *candidates: str) -> str:
    """解析一个 TLS 凭据文件的真实路径。

    `candidates` 为调用方按优先级给出的候选路径（通常是 env/cfg 的裸值，
    可能为空或乱码）；`default_filename` 是仓库 `certs/` 下的兜底文件名。

    返回首个存在的路径；全部不可得时返回空串（由调用方决定降级或报错）——
    注意兜底文件本身不存在时同样返回空串，绝不把不存在的路径当有效值返回。

    告警策略（区分两个正交维度，勿混淆）：
      * 「配置了但全部失效」→ WARNING。这是异常（典型即 .env 注入被 GBK
        误解码成乱码，字符串非空但文件不存在），静默兜底会让运维误以为配置
        生效——这正是本 P1 难定位的根因。
      * 「根本没配（候选为空/空白）」→ 不告警。走仓库默认属设计预期，是正常
        启动路径；每次启动都刷 WARNING 等于没有告警（告警疲劳）。
    三类凭据（CA / 客户端证书 / 私钥）一视同仁，不做区别对待：
    mTLS 下静默换客户端证书同样会用非预期身份去握手，造成授权混淆。
    """
    configured = [c for c in candidates if c and c.strip()]
    fallback = str(REPO_CERTS_DIR / default_filename)

    def _is_file(path: str) -> bool:
        try:
            return Path(path).is_file()
        except OSError:
            return False

    unusable: list[str] = []
    for candidate in configured:
        if _is_file(candidate):
            return candidate
        unusable.append(candidate)

    if _is_file(fallback):
        if unusable:
            # 仅在确实跳过了不可用候选时告警。若配置值本身就等于兜底路径
            # （今天的实际形态：.env 直接指向 <repo>/certs/），则属原样命中，
            # 不得告警——否则每次启动都刷 WARNING，造成告警疲劳。
            logger.warning(
                "TLS credential path unusable: %s; falling back to repo default %s",
                ", ".join(unusable), fallback,
            )
        return fallback

    if unusable:
        logger.warning(
            "TLS credential path unusable: %s; repo default %s also missing, "
            "returning empty (caller decides degrade or fail)",
            ", ".join(unusable), fallback,
        )
    return ""
