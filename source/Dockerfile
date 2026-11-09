FROM python:3.11-slim

# 设置工作目录
WORKDIR /app

# 安装系统依赖
RUN apt-get update && apt-get install -y --no-install-recommends \
    tzdata \
    && rm -rf /var/lib/apt/lists/*

# 设置时区为中国标准时间
ENV TZ=Asia/Shanghai
RUN ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && echo $TZ > /etc/timezone

# 复制依赖并安装
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 复制应用代码
COPY app.py .

# 创建数据目录
RUN mkdir -p /app/data

# 声明端口：6060(Web), 25(SMTP)
EXPOSE 6060 25

# 环境变量默认值
ENV DB_FILE=/app/data/temp_mail.db \
    APP_HOST=0.0.0.0 \
    APP_PORT=6060 \
    SMTP_HOST=0.0.0.0 \
    SMTP_PORT=25 \
    FRONT_RETENTION_MINUTES=10 \
    BACKEND_RETENTION_DAYS=7 \
    MAILBOX_VALIDITY_HOURS=24

# 启动命令
CMD ["python", "app.py"]
