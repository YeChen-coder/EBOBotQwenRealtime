FROM python:3.11-slim

WORKDIR /app

# 安装系统依赖
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    g++ \
    && rm -rf /var/lib/apt/lists/*

ENV PYTHONUNBUFFERED=1 HF_HUB_DISABLE_SYMLINKS_WARNING=1

# 复制依赖文件
COPY requirements.txt .

# 安装 Python 依赖
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# 复制应用代码
COPY . .

# 创建数据目录
RUN mkdir -p /data/replies /data/memory-v2

# 暴露端口
EXPOSE 8099

# 运行应用
CMD ["python", "app.py"]
