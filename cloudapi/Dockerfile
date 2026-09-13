# jax-voice-api：云端语音控制面（B 方案）
# 构建上下文 = 仓库根（COPY backend/app 复用既有商业安全路由，不自造第二套）
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /srv

COPY cloudapi/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# 只拷贝需要的包：backend/app（纯 Python）+ 容器入口
COPY backend/app ./app
COPY cloudapi/main.py ./main.py

# 运行时配置（monitors/detection/voice/brain 等 YAML）。
# app/_frozen_paths.project_root() 源码态取 __file__.parents[2]，容器内
# /srv/app/... 上溯两级即 /，因此 CONFIG_DIR 落在 /config —— 必须与推导一致，
# 否则启动即崩（实测：FileNotFoundError: 配置文件缺失: /config/monitors.yaml）。
# config/ 只含非敏感配置（token 字段为空/注释），可安全进镜像；密钥一律走环境变量。
COPY config /config

# 正式持久化 = CloudBase PostgreSQL（cloudbase/migrations 版本化迁移）。
# 容器内不挂任何本地数据卷：容器文件系统视为易失，无状态可水平扩容。
ENV VOICE_STORAGE_BACKEND=postgresql

# 生产开关必须固化在镜像里。app/voice/store_factory.py:84-94 以
# settings.voice_production 分支：False → 容器内的本地文件夹具（数据易失，
# 与「云端无状态 + PG 持久化」目标架构不符），True → PostgresVoiceStore。
# 只靠云端 EnvParams 手工注入时，任何不带 EnvParams 的部署都会**静默回退本地夹具**。
# 缺 DSN 时 validate_voice_storage() 会在启动期 fail-closed 拒绝——这是预期行为，
# 不得绕过，也绝不允许这里写成 false。
ENV VOICE_PRODUCTION=true

EXPOSE 9000
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "9000"]
