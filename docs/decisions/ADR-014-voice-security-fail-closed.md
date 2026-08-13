# ADR-014: 语音控制面采用生产 fail-closed 安全边界

## Status: Accepted (2026-08-07)

## Background

现有签发路径可在开发配置下放宽 token 和加密要求，且客户端可自报设备身份。商业版需要区分 Windows owner、Android device 与 Windows sidecar 三类主体，并防止凭证伪造、nonce 重放、越权签发和撤销后继续使用。

## Decision

生产缺以下任一项时，服务必须拒绝启动或拒绝全部会话：设备凭证验证器、独立 sidecar credential、nonce 哈希存储、防重放、device/IP 限流、TLS、TRTC SDKAppID/SecretKey。禁止匿名 WS、明文 relay 和客户端自报特权身份。

- owner credential 仅生成 pairing_code、列出和撤销设备。
- pairing_code 是 register 的一次性 bootstrap 主体，TTL 不超过 300 秒，只存哈希，在数据库事务内原子消费。
- credential_secret 仅在注册成功响应展示一次；服务端只保存抗离线攻击的哈希。
- device credential 只调用 Android 会话签发；sidecar credential 独立且只调用 session/sign。
- 有副作用请求必须带与主体绑定的 `X-Request-Nonce`；nonce 只存哈希并原子消费。
- userSig TTL 不超过 600 秒，绑定 session/device/user/room。
- 撤销必须立即拒绝 credential、登记未过期 userSig 指纹、终止活动 session 并写脱敏审计。
- 所有 HTTP 端点使用 `/api/v1/` 与统一 `{code,data,message}` 响应。

## Consequences

正面后果：身份边界可审计；重放和越权签发有确定错误码；生产缺配置不会静默退回开发模式。

负面后果：需要凭证轮换、nonce TTL 清理、限流桶维护和撤销传播；跨进程终止失败必须显式返回错误，不能报告虚假成功。

## Alternatives

- 复用一个共享 token：拒绝，不能隔离 owner/device/sidecar 权限。
- 只依赖短期 userSig：拒绝，签发入口本身仍可能被滥用。
- 生产自动回退匿名本地模式：拒绝，违反 fail-closed。

## Related ADRs

ADR-013、ADR-018。
