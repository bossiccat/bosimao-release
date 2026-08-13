# ADR-018: SQLite 只保存最小本地隐私数据

## Status: Accepted (2026-08-07)

## Background

单用户桌面 MVP 不需要 PostgreSQL、Redis 或远端分析库，但需要设备凭证哈希、配对码单次消费、撤销传播、最小审计和用户可选的本地转写。原始音频、截图、代码和长期凭证明文会扩大隐私与泄漏风险。

## Decision

使用本机 SQLite 保存 Spec v1.1 锁定的 9 张最小业务表：settings、device_credentials、pairing_codes、revoked_sessions、session_events、transcripts、privacy_audit_events、consumed_nonces、rate_limit_buckets；另设 1 张迁移基础设施元数据表 `schema_migrations`，只记录 schema 版本与校验信息，不计入业务表契约。物理 schema 共 10 张表。详细 DDL、索引、外键与事务语义以 `docs/commercial-voice-sqlite-architecture.md` 为唯一架构细化契约。

- pairing_code、nonce、device credential 只保存哈希；Secret 只展示一次。
- 原始音频、截图、代码、TRTC SecretKey、userSig 明文和第三方 token 不落库。
- 转写默认不持久化；用户显式开启后才以 Windows DPAPI 或等价 OS-bound key 加密保存。
- 删除转写不在审计、备份或诊断中保留正文副本。
- 诊断采用字段 allowlist，只导出脱敏状态、错误、延迟、帧和队列指标。
- 四类隐私设置由同一 service 编排 SQLite 写入与运行时动作；动作失败必须回滚设置值。
- 配对消费、设备创建和审计在一个 `BEGIN IMMEDIATE` 事务中完成；并发注册只有一个成功。

## Consequences

正面后果：单用户部署简单；安全状态可事务化；数据最小化边界可静态审计。

负面后果：需要迁移、TTL 清理、WAL checkpoint 和本地备份边界；跨进程 session 终止不能仅靠数据库事务完成，失败必须显式返回可重试状态。

## Alternatives

- PostgreSQL/Redis：拒绝进入当前 MVP，增加部署和运维成本。
- 保存原始音频用于诊断：拒绝，超出最小数据边界。
- 关闭隐私开关只更新 UI：拒绝，必须与运行时动作同一 service 编排并可回滚。

## Related ADRs

ADR-014、ADR-016。
