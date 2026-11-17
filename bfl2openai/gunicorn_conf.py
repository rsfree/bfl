"""gunicorn 配置（uvicorn worker）。

worker 数：
  * 默认 6（GUNICORN_WORKERS 可调）。任务表/上传句柄/组合态已外置
    store.py（REDIS_URL，compose 里随 redis:7-alpine 服务），多 worker
    天然共享状态；REDIS_URL 未设时回落进程内存 —— 那样请保持 1 worker。
  * 2026-11-17 由 3 提到 6：不是为了吞吐（实测 3 worker 时在途仅 3~4 个、
    CPU 12%，瓶颈在上游配额），而是买突发并行度 + 图片归一化这类 CPU 活
    的并行。每个 worker RSS 约 640MB（依赖只占 102MB，其余是大图处理后的
    分配器高水位），6 个约 3.8GB，compose 的 mem_limit 相应提到 5g。
"""
import os

bind = "0.0.0.0:8000"
workers = int(os.environ.get("GUNICORN_WORKERS", "6"))
worker_class = "uvicorn.workers.UvicornWorker"
# 生成是长请求（1k≈30s，4k 复合 2-3 分钟）：超时必须盖过最慢路径
timeout = 300
graceful_timeout = 90
keepalive = 5
# 处理过大图（PIL 缩放 + base64）的 worker 会留下几百 MB 不归还 OS 的堆，
# 只涨不降。定期平滑回收：gunicorn 会在当前请求结束后才换进程，不打断在途生成。
# 按日均 4000 单 / 6 worker ≈ 667 单，500 上下时一天换 1~2 次。
max_requests = int(os.environ.get("GUNICORN_MAX_REQUESTS", "500"))
max_requests_jitter = int(os.environ.get("GUNICORN_MAX_REQUESTS_JITTER", "100"))
accesslog = "-"    # 走容器 stdout，docker logs 可查
errorlog = "-"
