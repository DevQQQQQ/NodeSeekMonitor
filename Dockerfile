FROM python:3.12-slim

WORKDIR /app

# 系统依赖极小，仅 Python；时区偏移用代码常量实现，无需 tzdata
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

# 运行时持久化目录（也会被 docker-compose 挂载覆盖）
RUN mkdir -p /app/data /app/logs

# 密钥一律来自 env_file(.env)，不写进镜像
CMD ["python", "-m", "app.main"]
