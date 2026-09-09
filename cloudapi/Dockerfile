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

# 正式持久化 = CloudBase PostgreSQL（cloudbase/migrations 版本化迁移）。
# 容器内不挂任何本地数据卷：容器文件系统视为易失，无状态可水平扩容。
ENV VOICE_STORAGE_BACKEND=postgresql

EXPOSE 9000
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "9000"]
