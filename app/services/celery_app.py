from celery import Celery
from app.config import settings

celery_app = Celery(
    "milevault",
    broker=settings.CELERY_BROKER_URL,
    backend=settings.CELERY_RESULT_BACKEND,
    include=["app.services.tasks"],
)

celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="UTC",
    enable_utc=True,
    task_track_started=True,
    task_acks_late=True,        # Acknowledge after completion (prevents loss on crash)
    worker_prefetch_multiplier=1,
    task_routes={
        "app.services.tasks.watermark_image": {"queue": "media"},
        "app.services.tasks.watermark_video": {"queue": "media"},
        "app.services.tasks.send_notification_email": {"queue": "notifications"},
        "app.services.tasks.process_kyc_document": {"queue": "kyc"},
        "app.services.tasks.process_refund": {"queue": "payments"},
    },
    beat_schedule={},
)
