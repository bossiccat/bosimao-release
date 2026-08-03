# ADR-006: 后台服务架构 — Python FastAPI + asyncio 单进程

- 状态：已接受
- 日期：2026-08-03
- 决策者：架构师 高见远

## 背景

后台需编排：窗口截屏调度、模型推理（视觉 + 语音）、语音管线、推送服务、WebSocket 状态推送。技术栈需与 AI/ML 生态（windows-capture / silero-vad / sherpa-onnx）直接绑定。

## 选项对比

| 方案 | AI/ML 生态 | 截屏/语音绑定 | WebSocket | 结论 |
|---|---|---|---|---|
| Python 3.11+ FastAPI | 最强（全 Python 库直接调用） | 直接 | 原生 async WS | 选中 |
| Node.js | 弱（需 child_process 调 CLI） | 间接 | 好 | 否决 |
| Go | 无 ML 生态 | 间接 | 好 | 否决 |

## 决策

- **Python 3.11+ FastAPI + uvicorn + asyncio 单进程**
- llama.cpp-omni 为**独立子进程**（:19080），Python 经 HTTP 调用
- 进程编排：单 Python 进程 + asyncio 任务（截图调度器 / 推理队列 / 语音会话 / 推送 / WS 广播）
- LLM 调用串行化（asyncio 锁）——显存时分复用硬约束
- 依赖管理：`pyproject.toml` + `requirements.txt` 锁定版本

## 后果

- 正面：全栈 Python，截屏/语音/ML 库直接 import；asyncio 天然适配事件驱动
- 负面：单进程需防 CPU/GPU 阻塞（重推理放线程池/子进程）；长期运行需防显存泄漏
- 替代触发条件：性能压测不达标 → 拆双进程（capture/engine 分离）
