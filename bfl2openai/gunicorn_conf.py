"""gunicorn 配置（uvicorn worker）。

worker 数：
  * 默认 3（GUNICORN_WORKERS 可调）。任务表/上传句柄/组合态已外置
    store.py（REDIS_URL，compose 里随 redis:7-alpine 服务），多 worker
    天然共享状态；REDIS_URL 未设时回落进程内存 —— 那样请保持 1 worker。
"""
import os

bind = "0.0.0.0:8000"
workers = int(os.environ.get("GUNICORN_WORKERS", "3"))
worker_class = "uvicorn.workers.UvicornWorker"
# 生成是长请求（1k≈30s，4k 复合 2-3 分钟）：超时必须盖过最慢路径
timeout = 300
graceful_timeout = 90
keepalive = 5
accesslog = "-"    # 走容器 stdout，docker logs 可查
errorlog = "-"
