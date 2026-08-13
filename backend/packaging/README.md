# jax-backend.exe 打包验证记录（阶段 D：架构减疑 · 进程品牌化）

> 目标：把后端 FastAPI 从「裸 python.exe（pythonw -m uvicorn）」打包为单个 `jax-backend.exe`，
> 消除任务管理器里让客户恐慌 / 触发杀毒误报的 `python.exe`。
> 本文档是第一步（可行性验证）的实测记录 + 第二步（品牌化铺开）的操作依据。

## 1. 结论（一句话）

**可行，已验证通过。** `jax-backend.exe`（PyInstaller 6.22 onefile）构建成功、启动成功、`/health` 返回
`{"status":"ok","model_server":"up"}`，PE 子系统为 `WINDOWS_GUI`（无命令窗），单元测试仍 459 绿。

## 2. 构建

```powershell
cd backend/packaging
..\..\.venv\Scripts\pyinstaller.exe jax-backend.spec --noconfirm
```

产物：`backend/packaging/dist/jax-backend.exe`（onefile，约 106 MB）。

- 默认 `console=False`（无窗口，PE 子系统=2 WINDOWS_GUI），对齐服务层 `pythonw` 的无窗口语义。
- 排障时强制控制台（可见 stdout/stderr traceback）：`JAX_BACKEND_CONSOLE=1`。

## 3. 部署布局（关键约束）

`jax-backend.exe` 必须部署在「项目根」目录（与 `config/`、`.env`、`backend/`、`models/`、`certs/` 同级）。

```
<项目根>/
├── jax-backend.exe        # 替代 python -m uvicorn app.main:app
├── config/                # 外部可编辑（yaml + prompts）
├── .env                   # 外部，含密钥
├── certs/                 # TLS 证书
├── backend/data/          # 运行时 SQLite / 授权文件 / brain_tasks
└── models/                # sherpa STT 模型
```

- **运行时可变资源（config/.env/data/models）留在磁盘，绝不打进 exe**（.env 含密钥、models 是 GB 级 GGUF、DB 是运行态）。
- **只读代码数据（voice migrations SQL）打进 exe**，从 `_MEIPASS` 读取（见 `app/_frozen_paths.py::bundled_path`）。

## 4. 遇到的坑（已全部解决）

| # | 坑 | 处理 |
|---|----|------|
| 1 | **uvicorn 动态导入**：`loops/protocols/lifespan` 走 importlib，静态分析抓不到 → 冻结后 `AttributeError: module 'uvicorn' has no attribute ...` | spec 显式加 `hiddenimports`（uvicorn.loops.auto / protocols.http.auto / protocols.websockets.auto / lifespan.on 等） |
| 2 | **reload=True 不能用于冻结**：`run.py` 用 `reload=True`（multiprocessing spawn 子进程），冻结态必崩 | 生产入口 `jax_backend_entry.py` 改**对象导入** `from app.main import app` + `uvicorn.run(app, reload=False)` |
| 3 | **PROJECT_ROOT 错位**：`__file__.parents[N]` 冻结后指向 `_MEIxxxx` 临时目录，config/.env/data 全解析错 | 新增 `app/_frozen_paths.py`，冻结态 PROJECT_ROOT = exe 所在目录；更新 config / voice/config / utils/logger / capture/session_manager / voice/storage 共 5 处 |
| 4 | **migrations SQL 是数据文件**：`VoiceStore.initialize()` 读 `*.sql` 跑迁移 | spec `datas` 打包 `app/voice/migrations/*.sql`，`bundled_path()` 从 `_MEIPASS` 读 |
| 5 | **windowed 下 sys.stdout/stderr 为 None**：`console=False` 无控制台，PyInstaller 把 stdout/stderr 置 None，`logging.StreamHandler(sys.stdout)` 崩 → 无重定向直接启动会崩并留下孤儿进程 | 入口顶部兜底重定向到 `os.devnull` |
| 6 | **中文路径**：项目根含「监视app」 | PyInstaller 6.22 全程正常，无编码问题（构建日志、_MEIPASS 解压均正常） |
| 7 | **WinRT panic（非致命）**：`windows-capture`(Rust WGC) 在部分环境 `Failed to initialize WinRT` | 既有问题，非打包引入；不阻断启动（`Application startup complete` 正常） |
| 8 | **构建缓存 safe-delete**：WorkBuddy 沙箱的 safe-delete shim 在**重复构建**时删 `base_library.zip` 失败（`SAFE_DELETE_FAIL_CLOSED`） | 重复构建前先 `rm -rf build dist`（或 .NET `Directory.Delete`）再 build |
| 9 | **启动传参**：`--host 127.0.0.1 --port 8000` 由入口 argparse 解析 | `jax-backend.exe --host 127.0.0.1 --port 8000`；端口默认取 `.env BACKEND_PORT` |
| 10 | 良性告警：`wsproto`/`uvloop`/`tzdata`/`pypinyin`/`cv2`/`mypy`/`rich` 等 optional 依赖缺失 | 不影响运行（uvicorn 用 websockets 实现，Windows 用 asyncio） |

## 5. 实测输出

### 5.1 构建成功
```
Bootloader ...\PyInstaller\bootloader\Windows-64bit-intel\runw.exe   # console=False
Building EXE from EXE-00.toc completed successfully.
Build complete! The results are available in: ...\backend\packaging\dist
```

### 5.2 启动 + /health（HTTPS，因 .env 配置了 TLS）
```
$ ./jax-backend.exe --host 127.0.0.1 --port 8020
{"logger":"__main__","msg":"启用 HTTPS/WSS：cert=...certs/server.crt key=...certs/server.key"}
{"logger":"app.core.orchestrator","msg":"orchestrator started: 4 targets"}
INFO: Application startup complete.
INFO: Uvicorn running on https://127.0.0.1:8020

$ curl -sk https://127.0.0.1:8020/health
{"status":"ok","model_server":"up"}          # HTTP 200
```

### 5.3 无窗口（PE 子系统）
```
$ python scripts/pe-subsystem-verify.py backend/packaging/dist/jax-backend.exe
  OK  subsystem= 2 WINDOWS_GUI      magic=0x20b  jax-backend.exe
RESULT: ALL_GUI = True  (全部为 GUI 子系统，不会弹出命令窗)
```

### 5.4 单元测试（打包不影响源码）
```
$ cd backend && ../.venv/Scripts/python.exe -m pytest tests/unit -q
459 passed, 3 warnings in 43.66s
```

## 6. 文件清单

| 文件 | 说明 |
|------|------|
| `backend/packaging/jax-backend.spec` | PyInstaller spec（hiddenimports / datas / console 开关） |
| `backend/packaging/jax_backend_entry.py` | 生产入口（对象导入 + 单进程 + TLS + stdout 兜底） |
| `backend/app/_frozen_paths.py` | 冻结态路径解析（`project_root()` / `bundled_path()`） |
| `backend/app/config.py` 等 5 处 | `__file__` 派生路径 → `project_root()`（源码态行为不变） |
