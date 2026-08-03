# 贾克斯模式 · 星核 Spark

你的 AI 编程 agent 们的贴身监工 + 副驾——Windows 桌面常驻的宠物形 AI 智能助手，
以本地 MiniCPM-O 4.5 多模态 9B 为大脑，实时截屏监控 Codex / Trae / Hermes 的工作进展，
跑偏/卡住/关键进展时主动用语音、桌宠动效和手机提醒你，并给出任务优化建议。

> 安全边界：只监控 + 提醒 + 建议，不直接操控被监控的智能体。

## 快速开始

```powershell
# 1. 环境初始化（venv + 依赖 + .env）
powershell -ExecutionPolicy Bypass -File scripts/setup_env.ps1

# 2. 下载模型（约 8.3GB，国内 ModelScope）
powershell -ExecutionPolicy Bypass -File scripts/download_model.ps1

# 3. 填写 .env（WECOM_WEBHOOK_URL / NTFY_TOPIC 推送配置）

# 4. 启动 Comni 桌面版（模型服务 :19080）→ 一键开发
powershell -ExecutionPolicy Bypass -File scripts/dev.ps1
```

## 目录导览

| 目录 | 说明 |
|---|---|
| `docs/` | 契约文档：PRD / ARCHITECTURE / DESIGN / SPEC / openapi / ADR-001~008 / POC 报告 |
| `backend/` | Python 3.11+ FastAPI 后端（截图调度 / 视觉判定 / 提醒 / 推送 / WS） |
| `pet-ui/` | Tauri v2 + React 桌宠（光球宠物 / 六态状态机 / 监控面板） |
| `config/` | 可热重载配置：监控目标 / 检测阈值 / 推送路由 / 宠物皮肤 |
| `scripts/` | 环境 / 下载 / 启动 / PoC 验证 / e2e 验收脚本 |

## 版本路线

- **V1**（监控闭环）：截屏监控 + 跑偏/卡住检测 + 四级提醒 + 手机推送 + 宠物光球
- **V1.1**（全双工语音）：原生全双工对话（类 GPT-Live）+ silero-vad 门控 + 兜底降级
- **V1.2+**：模型精度升级 / 皮肤扩展 / 任务优化建议 / 每日报告

## 技术栈（ADR 决策记录）

llama.cpp-omni + MiniCPM-o 4.5 GGUF Q4_K_M（RTX 3060 12G）· Windows.Graphics.Capture ·
FastAPI + asyncio · Tauri v2 + React + XState + Lucide · 企业微信 webhook + ntfy

详见 `docs/decisions/ADR-001~008.md`。

## 测试

```powershell
cd backend
..\.venv\Scripts\python -m pytest tests/ -v        # 单测 + 集成
python ..\scripts\e2e_verify.py                    # 端到端验收
```

## 已知坑

- Trae 是 Chromium GPU 窗口，必须用 WGC 捕获（PrintWindow 会黑屏）
- WGC 首次运行需对每个窗口授权一次（系统选择器）
- 12G 显存：禁止双模型实例；语音对话期监控自动降频
