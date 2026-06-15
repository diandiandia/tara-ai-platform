import os
from celery import Celery

REDIS_URL = os.getenv("REDIS_URL", "redis://127.0.0.1:6379/0")

celery_app = Celery(
    "tara_worker",
    broker=REDIS_URL,
    backend=REDIS_URL,
    include=["app.worker.tasks"] # 包含后台任务定义目录
)

# Celery 基础配置
celery_app.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    timezone="Asia/Shanghai",
    enable_utc=True,
    task_track_started=True,
    # 控制并发，防止 LLM API 被超卖限流 (BR-3.5)
    worker_concurrency=2
)
