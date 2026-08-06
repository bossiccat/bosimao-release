"""脱敏工具（backend-brain-spec §4.3）：上传 DeepSeek 前必经，纯函数可单测

五类规则（按顺序执行，防互相吞并）：
1. 邮箱/手机号        → [联系方式]
2. 文件/目录路径      → [路径]
3. API key/token     → [密钥]
4. 连续无空格 60+ 字  → [长文本]
5. 连续代码块(≥40字)  → [代码片段]
"""
from __future__ import annotations

import re

_PATH_RE = re.compile(
    r"(?i)(?:[A-Za-z]:[\\/][^\s;,\"'()<>]+|"
    r"/(?:home|Users|c|tmp|var|opt|etc|usr)/[^\s;,\"'()<>]+|"
    r"\\\\[^\s;,\"'()<>]+)"
)
_KEY_RE = re.compile(
    r"(?i)\b(?:sk-[A-Za-z0-9_\-]{8,}|ghp_[A-Za-z0-9]{20,}|"
    r"AKIA[A-Z0-9]{16}|[A-Za-z0-9_\-]{32,})\b"
)
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
_PHONE_RE = re.compile(r"(?<!\d)(?:\+?86[\- ]?)?1[3-9]\d{9}(?!\d)")
_LONG_TOKEN_RE = re.compile(r"\S{60,}")

# 代码启发式：强标记命中即算代码行；弱标记需 ≥2 个
_STRONG_CODE_MARKERS = (
    "def ", "class ", "import ", "return ", "=>", "```", "function ", "async ",
    "await ", "const ", "let ", "var ", "interface ", "type ", "print(", "SELECT ",
)
_WEAK_CODE_MARKERS = ("{", "}", ";", "(", ")", "=", "===", "!==", "->", "::")
_CODE_MIN_RUN_CHARS = 40


def _looks_code_line(line: str) -> bool:
    s = line.strip()
    if not s:
        return False
    if any(m in s for m in _STRONG_CODE_MARKERS):
        return True
    return sum(1 for m in _WEAK_CODE_MARKERS if m in s) >= 2


def _sanitize_contacts(text: str) -> str:
    text = _EMAIL_RE.sub("[联系方式]", text)
    return _PHONE_RE.sub("[联系方式]", text)


def _sanitize_paths(text: str) -> str:
    return _PATH_RE.sub("[路径]", text)


def _sanitize_keys(text: str) -> str:
    return _KEY_RE.sub("[密钥]", text)


def _sanitize_long_tokens(text: str) -> str:
    return _LONG_TOKEN_RE.sub("[长文本]", text)


def _sanitize_code(text: str) -> str:
    """连续代码行（启发式）合并为一段，整段 ≥40 字符才替换"""
    lines = text.split("\n")
    out: list[str] = []
    i = 0
    while i < len(lines):
        if _looks_code_line(lines[i]):
            buf: list[str] = []
            total = 0
            while i < len(lines) and _looks_code_line(lines[i]):
                buf.append(lines[i])
                total += len(lines[i]) + 1
                i += 1
            if total >= _CODE_MIN_RUN_CHARS:
                out.append("[代码片段]")
            else:
                out.extend(buf)
        else:
            out.append(lines[i])
            i += 1
    return "\n".join(out)


def sanitize(text: str) -> str:
    """五类脱敏（纯函数）：输出不含原文路径/key/邮箱/长 token/代码片段"""
    if not text:
        return text
    out = _sanitize_contacts(text)
    out = _sanitize_paths(out)
    out = _sanitize_keys(out)
    out = _sanitize_long_tokens(out)
    out = _sanitize_code(out)
    return out


def truncate_head_tail(text: str, max_chars: int) -> str:
    """超长截断：头 70% + 尾 30%，中间省略（spec §3.4 输入裁剪），总长 ≤ max_chars"""
    if len(text) <= max_chars:
        return text
    marker = "…[中间省略]…"
    remain = max_chars - len(marker)
    head_len = int(remain * 0.7)
    tail_len = remain - head_len
    head = text[:head_len].rstrip()
    tail = text[-tail_len:].lstrip()
    return f"{head}{marker}{tail}"


def preview(text: str, max_chars: int = 60) -> str:
    """审计/推送用短预览（不含敏感全文）"""
    if not text:
        return ""
    return text.replace("\n", " ")[:max_chars]
