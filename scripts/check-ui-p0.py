from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass
from pathlib import Path

SOURCE_EXTENSIONS = {".tsx", ".jsx", ".vue", ".html", ".css", ".kt", ".xml"}
SCAN_ROOTS = (
    Path("pet-ui/src"),
    Path("mobile-app/app/src/main/java"),
    Path("mobile-app/app/src/main/res"),
)
TOKEN_FILES = {
    "design-tokens.css",
    "tokens.css",
    "design-tokens.json",
    "colors.xml",
}
EMOJI_RE = re.compile(
    "["
    "\U0001F000-\U0001FAFF"
    "\u2600-\u27BF"
    "\uFE00-\uFE0F"
    "\u200D\u20E3"
    "\U000E0020-\U000E007F"
    "]"
)
COMMENT_RE = re.compile(r"/\*[\s\S]*?\*/|//[^\n]*|<!--?[\s\S]*?-->")
ICON_CONTEXT_RE = re.compile(
    r"(?:<(?:button|a|svg|span|div)\b[^>]*>|android:(?:src|text)\s*=|content\s*:|aria-label\s*=)",
    re.IGNORECASE,
)
HEX_COLOR_RE = re.compile(r"(?<![\w-])#(?:[0-9a-fA-F]{3,4}|[0-9a-fA-F]{6}|[0-9a-fA-F]{8})(?![\w-])")
FUNCTION_COLOR_RE = re.compile(r"\b(?:rgb|rgba|hsl|hsla)\s*\(", re.IGNORECASE)
GRADIENT_RE = re.compile(r"(?:linear|radial|conic)-gradient\s*\(([^)]*)\)", re.IGNORECASE | re.DOTALL)
PURPLE_RE = re.compile(r"#(?:6366f1|4f46e5|7c3aed|8b5cf6|a855f7)|\b(?:indigo|purple|violet)\b", re.IGNORECASE)
PINK_RE = re.compile(r"#(?:ec4899|db2777|f472b6)|\b(?:pink|fuchsia)\b", re.IGNORECASE)
ELASTIC_RE = re.compile(
    r"cubic-bezier\s*\(\s*0\.68\s*,\s*-0\.55\s*,\s*0\.265\s*,\s*1\.55\s*\)",
    re.IGNORECASE,
)
PLACEHOLDER_RE = re.compile(
    r"\b(?:lorem ipsum|welcome to our app|sign up today)\b",
    re.IGNORECASE,
)
HERO_RE = re.compile(r"(?:class(?:Name)?\s*=\s*[\"'][^\"']*\bhero\b|\bfunction\s+Hero\b|\bconst\s+Hero\b)", re.IGNORECASE)
MARKETING_RE = re.compile(r"\b(?:sign up today|get started today|transform your|revolutionary|welcome to)\b", re.IGNORECASE)
IMPORT_RE = re.compile(r"(?:from\s+|require\s*\(\s*)[\"']([^\"']+)[\"']")
ICON_PACKAGES = (
    "lucide",
    "react-icons",
    "heroicons",
    "@heroicons",
    "@fortawesome",
    "fontawesome",
    "material-icons",
    "@mui/icons-material",
    "phosphor",
    "tabler-icons",
)


@dataclass(frozen=True)
class Finding:
    rule: str
    path: Path
    line: int
    detail: str


def source_files(root: Path) -> list[Path]:
    files: list[Path] = []
    for relative in SCAN_ROOTS:
        base = root / relative
        if not base.is_dir():
            continue
        files.extend(path for path in base.rglob("*") if path.is_file() and path.suffix.lower() in SOURCE_EXTENSIONS)
    return sorted(set(files))


def line_number(text: str, offset: int) -> int:
    return text.count("\n", 0, offset) + 1


def add_matches(findings: list[Finding], rule: str, path: Path, text: str, pattern: re.Pattern[str], detail: str) -> None:
    for match in pattern.finditer(text):
        findings.append(Finding(rule, path, line_number(text, match.start()), detail))


def functional_emoji_matches(text: str) -> list[re.Match[str]]:
    comment_spans = [match.span() for match in COMMENT_RE.finditer(text)]
    matches: list[re.Match[str]] = []
    for match in EMOJI_RE.finditer(text):
        if any(start <= match.start() < end for start, end in comment_spans):
            continue
        line_start = text.rfind("\n", 0, match.start()) + 1
        line_end = text.find("\n", match.end())
        if line_end < 0:
            line_end = len(text)
        line = text[line_start:line_end]
        if ICON_CONTEXT_RE.search(line):
            matches.append(match)
    return matches


def is_allowed_color(value: str) -> bool:
    normalized = value.lower()
    return normalized in {"#fff", "#ffffff", "#000", "#000000"}


def icon_package(specifier: str) -> str | None:
    lowered = specifier.lower()
    if lowered == "lucide-react" or lowered.startswith("lucide-react/"):
        return "lucide"
    for candidate in ICON_PACKAGES:
        if lowered == candidate or lowered.startswith(candidate + "/"):
            return candidate
    return None


def scan_file(root: Path, path: Path, findings: list[Finding], icon_sources: dict[str, Path]) -> None:
    relative = path.relative_to(root)
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        findings.append(Finding("utf8-source", relative, 1, "source file is not valid UTF-8"))
        return

    for match in functional_emoji_matches(text):
        findings.append(
            Finding(
                "emoji-icon",
                relative,
                line_number(text, match.start()),
                "emoji is forbidden as a functional icon",
            )
        )
    add_matches(findings, "elastic-easing", relative, text, ELASTIC_RE, "elastic or bouncing easing is forbidden")
    add_matches(findings, "placeholder-copy", relative, text, PLACEHOLDER_RE, "placeholder marketing copy is forbidden")

    for match in GRADIENT_RE.finditer(text):
        body = match.group(1)
        if PURPLE_RE.search(body) and PINK_RE.search(body):
            findings.append(
                Finding(
                    "purple-pink-gradient",
                    relative,
                    line_number(text, match.start()),
                    "purple or indigo to pink gradient is forbidden",
                )
            )

    if HERO_RE.search(text) and MARKETING_RE.search(text):
        hero = HERO_RE.search(text)
        assert hero is not None
        findings.append(
            Finding(
                "fake-hero",
                relative,
                line_number(text, hero.start()),
                "marketing hero is forbidden; show real product controls",
            )
        )

    for match in IMPORT_RE.finditer(text):
        package = icon_package(match.group(1))
        if package is not None:
            icon_sources.setdefault(package, relative)

    if path.name not in TOKEN_FILES:
        for match in HEX_COLOR_RE.finditer(text):
            if not is_allowed_color(match.group(0)):
                findings.append(
                    Finding(
                        "hardcoded-color",
                        relative,
                        line_number(text, match.start()),
                        "color literal must be referenced through a design token",
                    )
                )
        add_matches(
            findings,
            "hardcoded-color",
            relative,
            text,
            FUNCTION_COLOR_RE,
            "functional color must be referenced through a design token",
        )


def policy_findings(root: Path, files: list[Path]) -> list[Finding]:
    findings: list[Finding] = []
    combined = "\n".join(path.read_text(encoding="utf-8", errors="replace") for path in files)
    policy_path = Path("<policy>")
    if not re.search(r"(?:--target-min\s*:\s*44px|jax_touch_target_min[^>]*>\s*44dp)", combined, re.IGNORECASE):
        findings.append(Finding("touch-target-44", policy_path, 1, "44px or 44dp minimum interaction target is missing"))
    if "focus-visible" not in combined:
        findings.append(Finding("focus-visible", policy_path, 1, "keyboard focus-visible policy is missing"))
    if not re.search(r"prefers-reduced-motion\s*:\s*reduce", combined, re.IGNORECASE):
        findings.append(Finding("reduced-motion", policy_path, 1, "reduced-motion policy is missing"))
    return findings


def scan(root: Path) -> list[Finding]:
    files = source_files(root)
    findings: list[Finding] = []
    icon_sources: dict[str, Path] = {}
    for path in files:
        scan_file(root, path, findings, icon_sources)
    if len(icon_sources) > 1 or (icon_sources and set(icon_sources) != {"lucide"}):
        offending = next(
            (icon_sources[name] for name in sorted(icon_sources) if name != "lucide"),
            Path("<imports>"),
        )
        findings.append(
            Finding(
                "mixed-icon-library",
                offending,
                1,
                "one icon source is allowed; found " + ", ".join(sorted(icon_sources)),
            )
        )
    findings.extend(policy_findings(root, files))
    return sorted(findings, key=lambda item: (item.path.as_posix(), item.line, item.rule))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Check locked commercial voice UI P0 rules")
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    root = args.root.resolve()
    findings = scan(root)
    if findings:
        for finding in findings:
            print(f"{finding.path.as_posix()}:{finding.line}: [{finding.rule}] {finding.detail}")
        print(f"P0 UI scan failed: {len(findings)} finding(s)")
        return 1
    print("P0 UI scan passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
