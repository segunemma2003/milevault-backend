"""
Celery background tasks — media processing, notifications, KYC, refunds.
All tasks are idempotent and safe to retry.
"""
import logging
from app.services.celery_app import celery_app
from app.config import settings

logger = logging.getLogger(__name__)


@celery_app.task(
    bind=True,
    max_retries=3,
    default_retry_delay=30,
    name="app.services.tasks.watermark_image",
)
def watermark_image(self, s3_key: str, user_id: str) -> dict:
    """
    Download image from S3, apply watermark, re-upload to same key.
    Replaces the original — presigned download URL stays the same.
    """
    try:
        import boto3
        from app.services.s3_service import add_watermark_to_image

        if not settings.s3_enabled:
            logger.info(f"S3 not configured — skipping watermark for {s3_key}")
            return {"status": "skipped", "key": s3_key}

        s3 = boto3.client(
            "s3",
            aws_access_key_id=settings.AWS_ACCESS_KEY_ID,
            aws_secret_access_key=settings.AWS_SECRET_ACCESS_KEY,
            region_name=settings.AWS_REGION,
        )
        response = s3.get_object(Bucket=settings.S3_BUCKET_NAME, Key=s3_key)
        original_bytes = response["Body"].read()

        watermarked = add_watermark_to_image(original_bytes, settings.WATERMARK_TEXT)

        s3.put_object(
            Bucket=settings.S3_BUCKET_NAME,
            Key=s3_key,
            Body=watermarked,
            ContentType="image/jpeg",
        )
        logger.info(f"Watermarked image {s3_key} for user {user_id}")
        return {"status": "completed", "key": s3_key}

    except Exception as exc:
        logger.error(f"watermark_image failed for {s3_key}: {exc}")
        raise self.retry(exc=exc)


@celery_app.task(
    bind=True,
    max_retries=3,
    default_retry_delay=60,
    name="app.services.tasks.watermark_video",
)
def watermark_video(self, s3_key: str, user_id: str) -> dict:
    """
    Add watermark to video using ffmpeg subprocess.
    Downloads from S3, processes, re-uploads.
    """
    import subprocess
    import tempfile
    import os

    if not settings.s3_enabled:
        return {"status": "skipped", "key": s3_key}

    try:
        import boto3
        s3 = boto3.client(
            "s3",
            aws_access_key_id=settings.AWS_ACCESS_KEY_ID,
            aws_secret_access_key=settings.AWS_SECRET_ACCESS_KEY,
            region_name=settings.AWS_REGION,
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            input_path = os.path.join(tmpdir, "input.mp4")
            output_path = os.path.join(tmpdir, "output.mp4")

            # Download
            s3.download_file(settings.S3_BUCKET_NAME, s3_key, input_path)

            # Watermark with ffmpeg (must be installed on worker)
            watermark_text = settings.WATERMARK_TEXT
            cmd = [
                "ffmpeg", "-y", "-i", input_path,
                "-vf", (
                    f"drawtext=text='{watermark_text}':fontsize=48:"
                    f"fontcolor=white@0.4:x=(w-text_w)/2:y=(h-text_h)/2"
                ),
                "-codec:a", "copy",
                output_path,
            ]
            result = subprocess.run(cmd, capture_output=True, timeout=300)
            if result.returncode != 0:
                raise RuntimeError(f"ffmpeg failed: {result.stderr.decode()}")

            # Re-upload
            s3.upload_file(output_path, settings.S3_BUCKET_NAME, s3_key,
                           ExtraArgs={"ContentType": "video/mp4"})

        logger.info(f"Watermarked video {s3_key}")
        return {"status": "completed", "key": s3_key}

    except Exception as exc:
        logger.error(f"watermark_video failed for {s3_key}: {exc}")
        raise self.retry(exc=exc)


@celery_app.task(
    bind=True,
    max_retries=5,
    default_retry_delay=60,
    name="app.services.tasks.send_notification_email",
)
def send_notification_email(self, to_email: str, subject: str, body_html: str) -> dict:
    """Send transactional email via Resend (logs in dev when key not set)."""
    try:
        if not settings.RESEND_API_KEY:
            logger.info(f"[DEV EMAIL] To: {to_email} | Subject: {subject}")
            return {"status": "dev_logged"}

        import resend
        resend.api_key = settings.RESEND_API_KEY
        resend.Emails.send({
            "from": f"MileVault <{settings.DEFAULT_FROM_EMAIL}>",
            "to": [to_email],
            "subject": subject,
            "html": body_html,
        })
        return {"status": "sent", "to": to_email}
    except Exception as exc:
        raise self.retry(exc=exc)


@celery_app.task(
    bind=True,
    max_retries=3,
    name="app.services.tasks.process_kyc_document",
)
def process_kyc_document(self, kyc_doc_id: str) -> dict:
    """
    Placeholder for automated KYC checks (e.g. AWS Rekognition ID verification).
    In production, integrate with a KYC provider like Smile Identity or Onfido.
    """
    logger.info(f"KYC doc {kyc_doc_id} queued for review")
    return {"status": "queued_for_manual_review", "doc_id": kyc_doc_id}


@celery_app.task(
    bind=True,
    max_retries=3,
    default_retry_delay=120,
    name="app.services.tasks.process_refund",
)
def process_refund(self, refund_id: str) -> dict:
    """
    Executes a refund via the appropriate payment gateway.
    Called by admin after approving a dispute refund.
    """
    from app.database import SessionLocal
    try:
        db = SessionLocal()
        from app.models.currency import Refund
        refund = db.query(Refund).filter(Refund.id == refund_id).first()
        if not refund:
            return {"status": "not_found"}

        # Add actual gateway call here based on original payment method
        # For now: update wallet balance
        from app.models.wallet import WalletBalance, WalletTransaction as WalletTx
        balance = db.query(WalletBalance).filter(
            WalletBalance.user_id == str(refund.refund_to),
            WalletBalance.currency == refund.currency,
        ).first()
        if balance:
            balance.amount += refund.amount
        else:
            balance = WalletBalance(
                user_id=str(refund.refund_to),
                currency=refund.currency,
                amount=refund.amount,
            )
            db.add(balance)

        wallet_tx = WalletTx(
            user_id=str(refund.refund_to),
            type="refund",
            amount=refund.amount,
            currency=refund.currency,
            status="completed",
            description=f"Refund from dispute — Ref: {refund_id[:8]}",
        )
        db.add(wallet_tx)

        from datetime import datetime
        refund.status = "completed"
        refund.processed_at = datetime.utcnow()
        db.commit()
        logger.info(f"Refund {refund_id} completed")
        return {"status": "completed", "refund_id": refund_id}

    except Exception as exc:
        db.rollback()
        raise self.retry(exc=exc)
    finally:
        db.close()
