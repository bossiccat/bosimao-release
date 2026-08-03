# ADR-005: 手机推送 — PushService 插件层（企业微信 + ntfy）

- 状态：已接受
- 日期：2026-08-03
- 决策者：架构师 高见远

## 背景

需要把 4 级提醒推送到用户手机。用户手机系统未知，需国内可达、免费、零部署的方案，且可扩展替换。

## 选项对比

| 方案 | 平台 | 成本 | 结论 |
|---|---|---|---|
| 企业微信机器人 webhook | 微信生态双端 | 免费零部署 | 主选（国内可达） |
| ntfy | iOS+Android 官方 App | 免费可自建（MIT） | 备选（支持发截图附件） |
| Bark | iOS 专属 | 免费可自建 | iOS 备选 |
| Server酱 | 微信 | 免费版限流 | 备选 |
| Telegram bot | TG | 免费 | 否决（国内不可达） |
| 自建 WebSocket | 任意 | 需自研 App | 否决（MVP 过重） |

## 决策

- `PushService` 抽象接口：`push(text: str, image: Path | None = None, title: str | None = None) -> PushResult`
- Provider 插件：`wecom.py`（企业微信 webhook，限频 ~20 条/分钟）+ `ntfy.py`（支持截图附件）
- 管理器：`manager.py` 路由 + 重试 + 熔断（企微失败→ntfy）+ 限频
- 密钥配置：`.env`（WECOM_WEBHOOK_URL / NTFY_TOPIC / NTFY_SERVER），不入库

## 后果

- 正面：国内可达、免费、插件化可扩展；企业微信在用户国内环境最可靠
- 负面：企业微信限频（~20/min）需节流；ntfy 需用户装 App 并订阅 topic
- 替代触发条件：用户手机为 iOS 且偏好 → 加 Bark Provider（5 行代码 + push.yaml 注册）
