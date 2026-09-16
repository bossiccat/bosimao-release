"""契约：**CI 里不得关闭 TLS 校验** —— 因为 CI 连接上载着生产凭据。

为什么单独钉这条（2026-09-16 实测事故）
-------------------------------------
`.github/workflows/deploy-cloudrun.yml` 的产品自证脚本曾写：

```python
ctx = ssl.create_default_context()
ctx.check_hostname = False
ctx.verify_mode = ssl.CERT_NONE          # ← 关掉校验
...
urlopen(req, timeout=60, context=ctx)    # ← 所有请求都用这条连接
urlopen(..., headers={"Authorization": f"Bearer {owner}"})   # ← owner = 生产 owner 凭据
```

`VOICE_OWNER_CREDENTIAL` 是**全租户 admin**（可列/撤销全部设备、读会话）。
⇒ runner 出网链路上任何 MITM（恶意 DNS、被投毒的代理）都能拿到它；
而且这不只是"偷凭据"：**自证脚本的结论本身就是门禁**，中间人可以把它替换成 PASS，
于是**坏版本被判通过**。

恢复校验前已实测两个服务用默认校验握手均通过（DigiCert 链，剩余有效期 140 天），
所以这是纯粹的"白送风险"，不是"为了可用性做的取舍"。

判据：扫 `.github/workflows/` 下的文件，禁止出现常见的关闭校验写法。
CI 定义里**没有**任何正当理由关闭 TLS —— 需要自签链的场景应当显式带上锚证书并说明，
而不是 `CERT_NONE`。
"""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
WORKFLOWS = ROOT / ".github" / "workflows"

# 关闭 TLS 校验的常见写法（Python / Node / curl / 通用）
FORBIDDEN = (
    "CERT_NONE",
    "check_hostname = False",
    "check_hostname=False",
    "verify=False",
    "verify = False",
    "NODE_TLS_REJECT_UNAUTHORIZED",
    "rejectUnauthorized: false",
    "rejectUnauthorized:false",
    "InsecureSkipVerify",
    "--insecure",
)


def _workflow_files() -> list[Path]:
    assert WORKFLOWS.is_dir(), f"找不到 workflows 目录：{WORKFLOWS}"
    files = sorted(p for p in WORKFLOWS.iterdir()
                   if p.is_file() and p.suffix in (".yml", ".yaml"))
    assert files, "workflows 目录里没有任何 yml"
    return files


def _code_lines(text: str) -> str:
    """只取**非注释**行 —— 否则说明性注释会被判红。

    ⚠️ 这条是实测踩出来的（2026-09-16，就在本文件写完的当天）：
    初版直接对全文 `in` 匹配，而修复时写的解释性注释里正含
    ``check_hostname = False`` / ``CERT_NONE`` 两个被禁字面量 ⇒ **守卫对已修好的代码报红**，
    并且让紧接着的变异验证变成**无效**（变异前后都红，得不出任何结论）。
    这与同日审计里 sidecar 门禁"按字面量推断是否依赖真机"是同一个病：
    **约束的是措辞，不是事实。** 判据必须是代码，不是散文。
    """
    return "\n".join(ln for ln in text.splitlines() if not ln.lstrip().startswith("#"))


def test_no_tls_verification_is_disabled_in_ci() -> None:
    offenders = []
    for path in _workflow_files():
        code = _code_lines(path.read_text(encoding="utf-8"))
        for literal in FORBIDDEN:
            if literal in code:
                offenders.append(f"{path.name}: 出现 {literal!r}")
    assert not offenders, (
        "CI 定义里不得关闭 TLS 校验 —— 这些连接上载着生产凭据（owner bearer 是全租户 admin），"
        "且自证结论可被中间人替换。\n  - " + "\n  - ".join(offenders)
    )


def test_deploy_selfcheck_still_uses_a_verifying_context() -> None:
    """正例守卫：自证脚本必须保留默认（会校验的）context 构造。

    只删掉那两行也可能被误改成 `ssl._create_unverified_context()` 之类，
    所以这里正向要求 `ssl.create_default_context()` 仍在。
    """
    wf = (WORKFLOWS / "deploy-cloudrun.yml")
    assert wf.is_file(), "缺少 deploy-cloudrun.yml"
    text = wf.read_text(encoding="utf-8")
    assert "ssl.create_default_context()" in text, (
        "自证脚本必须用会校验的 context；若确实需要自签链，请显式加载锚证书并说明理由，"
        "不要退化成不校验。"
    )
