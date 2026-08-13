# -*- mode: python ; coding: utf-8 -*-
# jax-backend.spec — 后端 FastAPI 打包为 jax-backend.exe（PyInstaller 6.x）
#
# 部署布局（重要）：
#   jax-backend.exe 部署在「项目根」目录，与 config/、.env、backend/、models/ 同级。
#   运行时可变资源（config、.env、data、models）留在磁盘；只读代码数据（voice migrations SQL）
#   打进 exe 并从 _MEIPASS 读取（见 app/_frozen_paths.py 的 bundled_path）。
#
# 构建：
#   cd backend/packaging
#   ../../.venv/Scripts/pyinstaller.exe jax-backend.spec --noconfirm
# 产物：
#   backend/packaging/dist/jax-backend.exe（onefile）
import glob
import os
from pathlib import Path

from PyInstaller.utils.hooks import (
    collect_data_files,
    collect_dynamic_libs,
    collect_submodules,
)

# SPECPATH 由 PyInstaller 注入，指向本 spec 所在目录（backend/packaging）
HERE = Path(os.path.abspath(SPECPATH))
BACKEND = HERE.parent          # backend/
PROJECT = BACKEND.parent       # 项目根

# 生产态默认 False（windowed 无窗口，对齐服务层 pythonw；PE 子系统=2 WINDOWS_GUI）。
# 排障时可用环境变量 JAX_BACKEND_CONSOLE=1 强制 console（子系统=3，可见 stdout/stderr traceback）。
CONSOLE = os.environ.get("JAX_BACKEND_CONSOLE", "0") == "1"

# ---------- 隐藏导入：uvicorn 动态加载的子模块（loop/protocol/lifespan 走 importlib，静态分析抓不到） ----------
hiddenimports = [
    "uvicorn.logging",
    "uvicorn.loops",
    "uvicorn.loops.auto",
    "uvicorn.loops.asyncio",
    "uvicorn.protocols",
    "uvicorn.protocols.http",
    "uvicorn.protocols.http.auto",
    "uvicorn.protocols.http.h11_impl",
    "uvicorn.protocols.http.httptools_impl",
    "uvicorn.protocols.websockets",
    "uvicorn.protocols.websockets.auto",
    "uvicorn.protocols.websockets.websockets_impl",
    "uvicorn.protocols.websockets.wsproto_impl",
    "uvicorn.lifespan",
    "uvicorn.lifespan.on",
]

binaries = []
datas = []

# ---------- 原生/数据依赖显式收集（避免漏 DLL / 模型数据） ----------
# sherpa-onnx：native 扩展 + 兄弟 DLL（onnxruntime/sherpa-onnx-c-api/cxx-api）
hiddenimports += collect_submodules("sherpa_onnx")
binaries += collect_dynamic_libs("sherpa_onnx")

# silero-vad：.onnx/.jit 模型数据
datas += collect_data_files("silero_vad")

# sounddevice（PortAudio DLL）/ soundfile（libsndfile DLL）
binaries += collect_dynamic_libs("_sounddevice_data")
binaries += collect_dynamic_libs("_soundfile_data")

# ---------- voice 迁移 SQL：只读代码数据打进 exe，从 _MEIPASS 读取 ----------
migration_sql = sorted(glob.glob(str(BACKEND / "app" / "voice" / "migrations" / "*.sql")))
datas += [(f, "backend/app/voice/migrations") for f in migration_sql]

a = Analysis(
    [str(HERE / "jax_backend_entry.py")],
    pathex=[str(BACKEND)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["pytest", "setuptools", "pip"],
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="jax-backend",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=CONSOLE,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
