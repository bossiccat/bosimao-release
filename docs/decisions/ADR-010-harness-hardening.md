# ADR-010: Harness 自升级 — 六风险治理（跑偏/失败/翻车/裸奔/文件堆积/越跑越笨）

- 状态：已接受
- 日期：2026-08-03
- 决策者：项目总监（经用户质疑触发——"开始项目前不信任原 harness，须开到完整能开工的极致"）
- 触发背景：用户指出 harness 会"跑偏、失败、翻车、裸奔、文件堆积、越跑越笨"，要求先升级 harness 再开工

## 背景

项目进入 V1/V1.5 开发前，需先审视编排系统（专家团 SOP + 记忆系统 + 项目治理）本身是否严谨。六类风险逐一需要**可执行的机制**，不是口号。

## 决策：六风险 × 机制 × 落地文件

| # | 风险 | 根因 | 对策机制 | 落地（已执行/待执行） |
|---|---|---|---|---|
| 1 | **跑偏** | 需求漂移/范围蔓延/专家自说自话 | Spec-as-contract 锁定范围；需求变更必须走变更流程（小改更新变更记录/大改回 Phase 0）；RoleVerdict 结构化回传（blocking 只标三类） | ✅ docs/SPEC.md 已含变更流程；✅ RoleVerdict 已在全部专家回传执行 |
| 2 | **失败** | 任务失败无兜底/静默吞错 | 每任务 B 计划前置（PoC 已示范）；失败回传必须含 evidence；同一专家连续 3 轮无进展 → 升级用户或停下（Bounded 规则） | ✅ PoC 三份报告均含 B 计划；✅ 专家 spawn 均要求"失败输出发总监" |
| 3 | **翻车** | 不可回退/改动无基线 | git 基线先行（每里程碑提交）；开发前必须先提交当前基线；部署可回滚（配置备份 + git tag） | ✅ git init + 基线 2c7660a/5f1b41f；⚠️ 后续每里程碑强制提交 |
| 4 | **裸奔** | 无测试/无验证/硬件路径假绿 | QA 门禁前置（先红后绿 TDD）；**硬件路径必须真机实测**（B2 教训：mock 测不出 on_closed/monitor_index 真 API 坑）；P0 缺陷归零才交付 | ✅ 变异 4/4 杀 + config 红绿门；✅ B2 实测发现并修 2 个真 API bug；⚠️ 每模块提交前跑全量 pytest |
| 5 | **文件堆积** | 临时文件无限累积（WGC 帧文件 frame_{n}.png、探针、日志） | 清理规则：① tmp/captures 帧文件**每会话上限 200 帧**，超限删最旧；② 会话结束清理 tmp；③ backend/.audit_tmp 探针保留 7 天后归档；④ logs/ 日志按天滚动（保留 14 天） | ⚠️ **待后端落地**（任务已列：backend/app/capture/wgc_capture.py 加帧文件清理 + utils/logger.py 滚动日志）；.venv_old 待用户删 |
| 6 | **越跑越笨** | 上下文膨胀/记忆污染/踩坑不沉淀 | 三通道记忆：① pitfalls.jsonl 踩坑自学习（同一签名只追加计数，300 条上限，技术栈指纹召回）；② 增量记忆纪律（禁止整篇重写，只追加具体条目）；③ **STATUS.md 恢复点**（不依赖对话上下文，新会话读文件即恢复） | ✅ STATUS.md 已建；⚠️ pitfalls.jsonl 待后端落地（backend/.workbuddy/memory/pitfalls.jsonl）；已踩坑（on_closed/monitor_index/代理 7890/TRAE SOLO CN 进程名）需落条 |

## 后果

- 正面：六风险均有可执行机制；项目状态可跨会话恢复；踩坑经验可复用（如 windows-capture 2.0.0 的 on_closed/monitor_index 坑）
- 负面：多一道提交/清理纪律，节奏略慢；但防的是"翻车返工"的更大成本
- 需用户配合：手动删 .venv_old；装 rust（解锁 tauri build）；提供企微 webhook

## Related ADRs

- ADR-006（后端架构）、ADR-008（监控策略）、ADR-009（HomeRail 评估）、OPEN-DECISIONS O-011/012/013

## 已踩坑登记（pitfalls 种子，待落 jsonl）

1. windows-capture 2.0.0：`on_closed` 事件必填否则 start() 抛异常（family: dependency）
2. windows-capture 2.0.0：`display=0` 已废弃，用 `monitor_index=1`（1-based）（family: dependency）
3. 沙箱代理 127.0.0.1:7890：pip/spawn 网络失败需清 HTTP(S)_PROXY（family: runtime）
4. Trae 进程名是 `TRAE SOLO CN.exe`（含空格大小写），非 trae.exe（family: config）
5. Codex 桌面版：codex.exe 是无窗口后台进程，主窗口是 ChatGPT.exe（family: config）
6. pip 自升级被批量删除拦截：脚本需 SKIP_PIP_UPGRADE 逃生口（family: build）
7. WinPS5.1 解析 .ps1 无 BOM 按 GBK 炸中文：须 UTF-8 BOM（family: build）
8. pydantic v2 model_validate 默认 extra=ignore：yaml 包层会导致配置静默失效（family: config）
