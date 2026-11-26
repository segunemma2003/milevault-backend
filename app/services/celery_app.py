from celery import Celery
from celery.schedules import crontab
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
    broker_connection_retry_on_startup=True,
    # Nothing reads task results. Storing them made every send subscribe to the Redis result
    # channel, with its own ~20s retry loop when Redis is unreachable.
    task_ignore_result=True,
    # Unrouted tasks (auto-release, every beat job) must land on a queue the worker consumes.
    task_default_queue="default",
    task_routes={
        "app.services.tasks.watermark_image": {"queue": "media"},
        "app.services.tasks.watermark_video": {"queue": "media"},
        "app.services.tasks.send_notification_email": {"queue": "notifications"},
        "app.services.tasks.process_kyc_document": {"queue": "kyc"},
        "app.services.tasks.process_refund": {"queue": "payments"},
    },
    beat_schedule={
        "recompute-trust-scores-daily": {
            "task": "app.services.tasks.recompute_trust_scores",
            "schedule": crontab(hour=2, minute=30),
        },
        "expire-stale-invitations-daily": {
            "task": "app.services.tasks.expire_stale_invitations",
            "schedule": crontab(hour=3, minute=15),
        },
        "stale-deal-activity-warnings-daily": {
            "task": "app.services.tasks.stale_deal_activity_warnings",
            "schedule": crontab(hour=4, minute=15),
        },
        "scan-crypto-deposits-every-5min": {
            "task": "app.services.tasks.scan_crypto_deposits",
            "schedule": crontab(minute="*/5"),
        },
        "cleanup-unverified-accounts-every-5min": {
            "task": "app.services.tasks.cleanup_unverified_accounts",
            "schedule": crontab(minute="*/5"),
        },
        "poll-processing-payouts-every-10min": {
            "task": "app.services.tasks.poll_processing_payouts",
            "schedule": crontab(minute="*/10"),
        },
        "reconcile-ledger-nightly": {
            "task": "app.services.tasks.reconcile_ledger",
            "schedule": crontab(hour=2, minute=30),
        },
    },
)

import app.services.notification_dispatch  # noqa: E402,F401  (after-commit delivery for task notifications)


def fail_fast_publishing() -> None:
    """For the API process only: sending a job must never stall a web request. With Redis
    unreachable, kombu's default reconnect loop held each request ~19s; give up at once instead
    (the job is lost, so callers treat queueing as best-effort). The worker keeps the default,
    so it rides out a Redis restart."""
    celery_app.conf.broker_connection_max_retries = 1
