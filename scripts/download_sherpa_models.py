#!/usr/bin/env python3
"""下载 sherpa-onnx 中文流式模型（mobile-voice-spec §8.3 / ADR-003）

目标模型：sherpa-onnx-streaming-zipformer-zh-14M-2023-02-23（wenetspeech 中文流式）
默认目标目录：models/sherpa/wenetspeech-streaming（与 config/voice.yaml 对齐）

用法：
  python scripts/download_sherpa_models.py [--model-dir DIR] [--mirror modelscope|github]

说明：
  - 优先 ModelScope（国内可达）；失败回退 GitHub release（k2-fsa/sherpa-onnx）。
  - 网络受限导致下载失败 → 退出码 1 并打印明确提示；不阻塞网关（STT 用 mock 占位跑通协议）。
"""
from __future__ import annotations

import argparse
import shutil
import sys
import urllib.request
import zipfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DIR = PROJECT_ROOT / "models" / "sherpa" / "wenetspeech-streaming"

# k2-fsa/sherpa-onnx 官方 release 中的中文流式模型包
MODEL_BASENAME = "sherpa-onnx-streaming-zipformer-zh-14M-2023-02-23"
MIRRORS = {
    "modelscope": (
        "https://modelscope.cn/models/csukuangfj/"
        f"{MODEL_BASENAME}/resolve/master/{MODEL_BASENAME}.tar.bz2"
    ),
    "github": (
        "https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/"
        f"{MODEL_BASENAME}.tar.bz2"
    ),
}


def download(url: str, dest: Path, timeout: int = 120) -> None:
    print(f"下载模型: {url}")
    tmp = Path(str(dest) + ".tmp")  # 追加后缀，保留 .tar.bz2 语义
    req = urllib.request.Request(url, headers={"User-Agent": "jax-mode/0.1"})
    with urllib.request.urlopen(req, timeout=timeout) as resp, tmp.open("wb") as f:
        shutil.copyfileobj(resp, f, length=1024 * 1024)
    print(f"下载完成: {tmp} ({tmp.stat().st_size / 1024 / 1024:.1f} MB)")


def extract_tar_bz2(archive: Path, dest: Path) -> None:
    """解压 .tar.bz2（含顶层目录），把模型文件平铺进 dest"""
    import bz2
    import tarfile

    dest.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive, "r:bz2") as tar:
        members = tar.getmembers()
        top = members[0].name.split("/")[0] if members else MODEL_BASENAME
        for m in members:
            if not m.isfile():
                continue
            rel = Path(m.name).relative_to(top)
            target = dest / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            src = tar.extractfile(m)
            if src is None:
                continue
            with target.open("wb") as f:
                shutil.copyfileobj(src, f)
    print(f"解压完成 → {dest}")
    try:
        archive.unlink(missing_ok=True)
    except OSError as e:  # 清理失败不影响结果
        print(f"警告: 清理下载包失败: {e}")


def main() -> int:
    parser = argparse.ArgumentParser(description="下载 sherpa-onnx 中文流式模型")
    parser.add_argument("--model-dir", default=str(DEFAULT_DIR))
    parser.add_argument("--mirror", default="modelscope", choices=list(MIRRORS))
    args = parser.parse_args()

    dest = Path(args.model_dir)
    if (dest / "encoder.onnx").exists():
        print(f"模型已存在: {dest}（跳过下载）")
        return 0

    dest.mkdir(parents=True, exist_ok=True)
    url = MIRRORS[args.mirror]
    archive = dest / f"{MODEL_BASENAME}.tar.bz2"
    try:
        download(url, archive)
        tmp = Path(str(archive) + ".tmp")
        tmp.rename(archive)
        extract_tar_bz2(archive, dest)
    except Exception as e:  # noqa: BLE001 - 下载失败不阻塞（STT mock 占位）
        try:
            archive.unlink(missing_ok=True)
        except OSError:  # noqa: BLE001 - 清理失败忽略
            pass
        print(
            f"[FAIL] 模型下载失败: {e}\n"
            f"提示: 网络受限时 STT 走 mock 占位跑通网关协议；可稍后重试或换 --mirror github。\n"
            f"目标目录: {dest}",
            file=sys.stderr,
        )
        return 1
    print(f"模型就绪: {dest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
