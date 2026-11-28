FROM python:3.12-slim

WORKDIR /app

# constraints.txt 锁住传递依赖：不锁的话新版 pydantic 与 mcp 1.9.2 不兼容会启动失败
COPY requirements.txt constraints.txt ./
RUN pip install --no-cache-dir -r requirements.txt -c constraints.txt

COPY . .

RUN mkdir -p /app/uploads /app/data

EXPOSE 8000

# 启动期每 2 秒查一次：Traefik 只给 healthy 的容器路由，查得慢上线后就会 404 几十秒
HEALTHCHECK --interval=30s --timeout=5s --retries=3 --start-period=60s --start-interval=2s \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=4)"

CMD ["uvicorn", "src.main:app", "--host", "0.0.0.0", "--port", "8000"]
