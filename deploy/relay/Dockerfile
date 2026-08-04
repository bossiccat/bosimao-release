# 贾克斯模式 - 云端中继（M2，CloudBase CloudRun 容器型）
# 构建：CloudBase 控制台「云托管」上传本目录（deploy/relay/），容器监听 19090
# 镜像：python:3.11-slim
FROM python:3.11-slim

WORKDIR /app

# 自包含 relay 包（由 backend/relay/ 同步，构建快照）
COPY relay/ /app/relay/

# 运行依赖：fastapi（路由/WS）+ uvicorn[standard]（含 WS 支持）+ cryptography（AES-256-GCM E2EE）
RUN pip install --no-cache-dir \
    "fastapi>=0.110" \
    "uvicorn[standard]>=0.29" \
    "cryptography>=42.0" \
    "websockets>=12.0"

# 中继端口（与云托管服务端口配置一致：控制台/部署参数 Port=19090）
ENV RELAY_PORT=19090
EXPOSE 19090

# 入口：python -m relay.relay_server（uvicorn 0.0.0.0:19090，/relay/health /relay/ws）
CMD ["python", "-m", "relay.relay_server"]
