from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
SCANNER = ROOT / "scripts" / "check-ui-p0.py"


def _run_scan(root: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCANNER), "--root", str(root)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )


def _write_policy_baseline(root: Path) -> None:
    web = root / "pet-ui" / "src"
    android = root / "mobile-app" / "app" / "src" / "main" / "res" / "values"
    web.mkdir(parents=True)
    android.mkdir(parents=True)
    (web / "design-tokens.css").write_text(
        ":root { --target-min: 44px; --accent: #2ba8e0; }\n"
        ":where(button):focus-visible { outline: 2px solid var(--accent); }\n"
        "@media (prefers-reduced-motion: reduce) { * { transition-duration: 1ms; } }\n",
        encoding="utf-8",
    )
    (android / "dimens.xml").write_text(
        '<resources><dimen name="jax_touch_target_min">44dp</dimen></resources>',
        encoding="utf-8",
    )
    (android / "colors.xml").write_text(
        '<resources><color name="jax_color_accent">#2BA8E0</color></resources>',
        encoding="utf-8",
    )


def test_scanner_exists_and_accepts_a_clean_tokenized_fixture(tmp_path: Path) -> None:
    assert SCANNER.is_file(), f"P0 scanner missing: {SCANNER}"
    _write_policy_baseline(tmp_path)
    component = tmp_path / "pet-ui" / "src" / "VoiceControl.tsx"
    component.write_text(
        'import { Mic } from "lucide-react";\n'
        'export function VoiceControl() { return <button aria-label="开始对话"><Mic size={20} /></button>; }\n',
        encoding="utf-8",
    )

    result = _run_scan(tmp_path)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "P0 UI scan passed" in result.stdout


@pytest.mark.parametrize(
    ("relative_path", "content", "rule"),
    [
        ("pet-ui/src/EmojiButton.tsx", 'export const X = () => <button>🚀</button>;\n', "emoji-icon"),
        (
            "pet-ui/src/PurplePink.css",
            ".x { background: linear-gradient(135deg, #6366f1, #ec4899); }\n",
            "purple-pink-gradient",
        ),
        ("pet-ui/src/HardColor.tsx", 'export const x = { color: "#123456" };\n', "hardcoded-color"),
        (
            "pet-ui/src/MixedIcons.tsx",
            'import { Mic } from "lucide-react";\nimport { Rocket } from "react-icons/fa";\n',
            "mixed-icon-library",
        ),
        ("pet-ui/src/EmptyCopy.tsx", 'export const X = () => <h1>Welcome to Our App</h1>;\n', "placeholder-copy"),
        (
            "pet-ui/src/Bounce.css",
            ".x { transition: all 200ms cubic-bezier(0.68, -0.55, 0.265, 1.55); }\n",
            "elastic-easing",
        ),
        ("pet-ui/src/Hero.tsx", 'export const Hero = () => <section className="hero">Sign up today</section>;\n', "fake-hero"),
    ],
)
def test_scanner_reports_each_locked_p0_violation(
    tmp_path: Path,
    relative_path: str,
    content: str,
    rule: str,
) -> None:
    _write_policy_baseline(tmp_path)
    target = tmp_path / relative_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")

    result = _run_scan(tmp_path)

    assert result.returncode == 1, result.stdout + result.stderr
    assert rule in result.stdout
    assert relative_path in result.stdout.replace("\\", "/")


def test_scanner_allows_ugc_or_comment_emoji_outside_function_icon_context(tmp_path: Path) -> None:
    _write_policy_baseline(tmp_path)
    component = tmp_path / "pet-ui" / "src" / "ChatMessage.tsx"
    component.write_text(
        '// 用户消息可包含 emoji\nexport const text = "用户说：今天真开心";\n',
        encoding="utf-8",
    )

    result = _run_scan(tmp_path)

    assert result.returncode == 0, result.stdout + result.stderr


def test_scanner_requires_accessibility_policy_tokens(tmp_path: Path) -> None:
    web = tmp_path / "pet-ui" / "src"
    web.mkdir(parents=True)
    (web / "VoiceControl.tsx").write_text(
        'import { Mic } from "lucide-react";\nexport const X = () => <button aria-label="开始对话"><Mic /></button>;\n',
        encoding="utf-8",
    )

    result = _run_scan(tmp_path)

    assert result.returncode == 1
    assert "touch-target-44" in result.stdout
    assert "focus-visible" in result.stdout
    assert "reduced-motion" in result.stdout
