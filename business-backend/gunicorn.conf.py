import os
import multiprocessing

bind = os.getenv("APP_BIND", "0.0.0.0:8000")
workers = int(os.getenv("APP_WORKERS", multiprocessing.cpu_count() * 2 + 1))
worker_class = "uvicorn.workers.UvicornWorker"
worker_connections = 1000
keepalive = 5
timeout = 120
graceful_timeout = 30
accesslog = "-"
errorlog = "-"
loglevel = os.getenv("APP_LOG_LEVEL", "info")
preload_app = False
