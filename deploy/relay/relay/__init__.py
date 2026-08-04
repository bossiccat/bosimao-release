"""中继服务（M2）：backend/relay/ —— 独立于 app，可单独启动

- relay_protocol.py   帧编解码 + 配对帧 + AES-GCM E2EE（AAD 含 seq 防重放）
- relay_server.py     WebSocket 中继（端口 19090，纯透传 + 配对 + token 鉴权 + 心跳）
- relay_client.py     PC 侧客户端库（连中继 + 桥接本地 voice 网关）
- config.py           环境变量加载（RELAY_TOKEN / RELAY_E2EE_KEY）
"""
