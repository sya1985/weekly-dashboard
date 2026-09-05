# ================= 周报工作台 Docker 镜像 =================
# 说明: 纯 Python 标准库（http.server + sqlite3），零第三方依赖，
#       因此基础镜像用 python:3.12-slim 即可，镜像小而安全。
# 数据: SQLite 文件经环境变量 DB_PATH 指向 /data，运行时挂载卷持久化，
#       容器重建不丢数据（见 docker-compose.yml 或 docker run -v）。
# 端口: 默认 7878，可用 -e PORT=xxxx 覆盖。
# ------------------------------------------------------------
FROM python:3.12-slim

# 避免 Python 写 __pycache__，减少容器内磁盘写放大
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=7878 \
    DB_PATH=/data/weekly.db

WORKDIR /app

# 运行文件（server.py + 两个 HTML）由 docker-compose.yml 的 `- .:/app` 挂载提供，
# 不在镜像内 COPY——保证本地改动即时生效，也避免镜像封存过期副本。
# 数据卷：宿主挂载到此目录，weekly.db 落在这里
VOLUME /data

EXPOSE 7878

# 健康检查：确认服务进程活着且能响应
HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:'+__import__('os').environ.get('PORT','7878')+'/api/data', timeout=2)" || exit 1

CMD ["python", "server.py"]
