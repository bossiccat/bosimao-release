# O-018 切片 3 设计：sidecar credential「launcher → 后端」自动同步编排

> 日期：2026-09-03 13:30
> 前置证据：`outputs/rp07-s102-e2e-provision-20260903.md`（演练 PASS，缺口 #2 闭合路径）
> 范围：**设计稿，未实施**。实施须另行走 TDD RED→GREEN 流程。

## 1. 问题陈述

真实链路已验证：launcher（CSPRNG）→ 真管道 → provisioner → CM 三槽。但 secret 写入后端受保护配置（`.env` `VOICE_SIDECAR_CREDENTIAL`）无自动编排——每台新装机需手工桥接（读 CM → 写 .env → 重启 backend）。fresh install 的 sidecar 仍无法完成首次受保护 pending 请求（O-018 核心 blocking）。

## 2. 部署形态与方案取舍

当前生产形态 = **同机 all-in-one**（jax-backend.exe 读项目根 `.env`）。据此：

| 方案 | 说明 | 取舍 |
|------|------|------|
| A. 启动脚本编排（jax-services.ps1 / dev.ps1） | 在 owner credential provision 之后、backend 启动之前插入 `Invoke-SidecarCredentialProvision`：跑真 launcher → 读 CM active → 原子写 .env → 值变化则重启 backend | ✅ **推荐切片 3**。与 ADR-022 owner 同款模式，零新进程形态，TDD 面小 |
| B. NSIS custom action | 安装器内编排 | ❌ 切片 2 曾实证 NSIS System 插件风险（"不冒险出半成品"）；留待商业安装包阶段 |
| C. pet-ui Tauri 启动编排 | app setup 里 orchestrator + 写后端配置 | ❌ app 与 backend .env 位置耦合，违反分层；商用远端 backend 形态下不成立 |

**边界声明**：切片 3 只服务同机部署形态；backend 远端化（受保护配置 API / 部署通道）属未来独立决策，本切片不预设。

## 3. 编排语义（方案 A 详设）

```
Invoke-SidecarCredentialProvision（插入点：Start-BackendService 内 Invoke-OwnerCredentialProvision 之后）
  1. launcher 存在性检查（release 优先 debug 兜底，同 owner 模式）；缺失 → 警告 + 返回 $false（fail-closed，同 ADR-022）
  2. 记录 pre 状态：CM active 是否存在 + .env 当前值 hash（不落值）
  3. Start-Process -Wait 跑真 launcher（GUI 子系统，-Wait -PassThru 取退出码）
     exit != 0 → fail-closed 返回 $false
  4. 读 CM active（PowerShell CredReadW 或复用演练读数器 python 单文件工具）
     absent → fail-closed
  5. 与 .env 现值比对：
     - 相同 → 幂等跳过（写日志"[sidecar-credential][ok] 已同值，幂等跳过"）
     - 不同 → .env 备份（.env.backup-pre-sidecarprov-<ts>）+ 单键原子替换（临时文件 + move）+ 校验
  6. 若 .env 发生变化且 backend 已在运行 → 触发温和重启（复用 Stop-BackendProcesses + Start 流程）；
     backend 未运行 → 交给后续正常启动路径（新进程自然吃新值）
  7. 返回 $true
```

**fail-closed 原则**：任何一步失败 → 中止启动（与 owner credential 同级），绝不带不一致状态放行 backend。

## 4. TDD 计划

| 测试 | 层级 | 手段 |
|------|------|------|
| 源码完整性守卫（仿 jax-watchdog-sidecar-ownership.test.js 模式） | js | 断言 jax-services.ps1/dev.ps1 含 `Invoke-SidecarCredentialProvision` 调用点、fail-closed 分支、备份逻辑、幂等分支的源码特征 |
| .env 单键原子替换 | pytest（scripts 工具函数抽成 python 或 ps1+Pester） | 正向/键缺失/文件损坏/权限错误 |
| 幂等语义 | 单测 | 同值 → 不写盘不重启（mtime 断言） |
| E2E 演练 | 真机 | 重复本次 §10.2 演练步骤，但桥接步骤由编排自动完成；六槽快照夹逼 + 三组对照请求复跑 |

**RED 先行**：先写守卫测试断言编排存在 → 确认 RED 原因 → 最小 GREEN（编排函数）→ 全量回归。

## 5. 风险与边界

1. **旧值消费者**：已由本次演练核实无（CM 旧值被换后无组件报错；真机审批流尚未跑 pending 链，场景 6 验收时一并覆盖）。
2. **.env 并发写**：单机单编排入口（jax-services 串行），无并发；临时文件 + move 保证原子。
3. **值泄露面**：编排过程值只经内存/CM/.env；日志只落 hash 前缀与长度。
4. **回滚**：.env 备份 + CM revoke 幂等可重跑；launcher 每次生成新值（重跑=轮换，安全）。
5. **不碰**：NSIS 打包（切片 2 边界维持）、backend 远端形态、owner credential 链。

## 6. 验收标准（EARS）

- AC-1：When fresh machine 首次 Start-BackendService，系统必须自动完成 launcher→CM→.env 同值链，backend 启动后 pending 认证可用
- AC-2：If launcher 退出码非 0，系统必须中止 backend 启动并返回稳定失败（不得半配置放行）
- AC-3：If CM 值与 .env 已同值，系统必须幂等跳过且不重启 backend
- AC-4：When .env 值被编排更新，系统必须先落备份再原子替换，且日志中不得出现 secret 明文
- AC-5：If 编排任一步失败，系统必须输出 SIDECARPROV_* 诊断码（仿 SIDECAR_* 惯例）
