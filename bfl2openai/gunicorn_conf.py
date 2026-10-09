"""gunicorn 配置（uvicorn worker）。

worker 数：
  * 1（默认）= 与单 uvicorn 等价的行为，native/async/uploads 的进程内
    状态（任务表/句柄表/组合态）天然一致；
  * N>1 = 吞吐/韧性提升，但上面这些内存态按 worker 分片 —— 跨 worker
    的 native 轮询、异步任务查询、上传句柄都会 404。要么前置粘性路由
    （nginx `ip_hash`，同一调用方固定打到同一 worker），要么把状态外置
    （Redis/SQLite）。生产要多 worker 前先做这两件事之一。
"""
import os

bind = "0.0.0.0:8000"
workers = int(os.environ.get("GUNICORN_WORKERS", "1"))
worker_class = "uvicorn.workers.UvicornWorker"
# 生成是长请求（1k≈30s，4k 复合 2-3 分钟）：超时必须盖过最慢路径
timeout = 300
graceful_timeout = 90
keepalive = 5
accesslog = "-"    # 走容器 stdout，docker logs 可查
errorlog = "-"
