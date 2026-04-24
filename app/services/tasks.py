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
    default_retry_delay=300,
    name="app.services.tasks.auto_release_milestone",
)
def auto_release_milestone(self, milestone_id: str) -> dict:
    """
    Auto-release escrow to seller if buyer hasn't approved within AUTO_RELEASE_DAYS.
    Called after delivery is submitted. Skips if milestone already completed/disputed.
    """
    from app.database import SessionLocal
    db = SessionLocal()
    try:
        from datetime import datetime
        from app.models.transaction import Milestone, Transaction
        milestone = db.query(Milestone).filter(Milestone.id == milestone_id).first()
        if not milestone:
            return {"status": "not_found"}
        if milestone.status in ("completed", "disputed", "revision_requested"):
            return {"status": "skipped", "reason": milestone.status}
        if milestone.status not in ("under_review", "delivered"):
            return {"status": "skipped", "reason": f"status={milestone.status}"}
        # Check auto_release_at
        if milestone.auto_release_at and datetime.utcnow() < milestone.auto_release_at:
            return {"status": "not_yet_due"}

        tx = db.query(Transaction).filter(Transaction.id == milestone.transaction_id).first()
        if not tx:
            return {"status": "tx_not_found"}

        from app.routers.transactions import _release_milestone_escrow
        _release_milestone_escrow(db, tx, milestone, feedback="Auto-released after buyer inactivity period.")
        db.commit()
        logger.info(f"Auto-released milestone {milestone_id}")
        return {"status": "released", "milestone_id": milestone_id}
    except Exception as exc:
        db.rollback()
        raise self.retry(exc=exc)
    finally:
        db.close()


@celery_app.task(
    bind=True,
    max_retries=2,
    name="app.services.tasks.cancel_unfunded_transaction",
)
def cancel_unfunded_transaction(self, transaction_id: str) -> dict:
    """
    Auto-cancel a transaction if no milestone is funded by the funding deadline.
    Returns any partially-deposited escrow to buyer available balance.
    """
    from app.database import SessionLocal
    db = SessionLocal()
    try:
        from app.models.transaction import Transaction, Milestone
        from app.models.wallet import WalletBalance, WalletTransaction, LedgerEntry
        from app.services.notification_service import create_notification

        tx = db.query(Transaction).filter(Transaction.id == transaction_id).first()
        if not tx:
            return {"status": "not_found"}
        if tx.status in ("completed", "cancelled", "refunded"):
            return {"status": "skipped"}
        if (tx.funded_milestones or 0) > 0:
            return {"status": "has_funded_milestones"}

        # Refund any partial escrow (edge case: partial fund that never completed a milestone)
        currency = tx.currency
        buyer_balance = db.query(WalletBalance).filter(
            WalletBalance.user_id == tx.buyer_id,
            WalletBalance.currency == currency,
        ).first()
        if buyer_balance and (buyer_balance.escrow_amount or 0) > 0:
            refund_amount = buyer_balance.escrow_amount
            buyer_balance.amount += refund_amount
            buyer_balance.escrow_amount = 0
            db.add(LedgerEntry(
                debit_user_id=None, credit_user_id=tx.buyer_id,
                debit_account="escrow", credit_account="available",
                amount=refund_amount, currency=currency,
                reference_type="refund", reference_id=transaction_id,
                description="Auto-refund: transaction cancelled due to funding deadline",
            ))

        tx.status = "cancelled"
        create_notification(
            db, tx.buyer_id,
            "Transaction Auto-Cancelled",
            f"'{tx.title}' was cancelled because no milestones were funded by the deadline. Any held funds have been returned.",
            "transaction", related_item_id=tx.id, related_item_type="transaction",
        )
        if tx.seller_id:
            create_notification(
                db, tx.seller_id,
                "Transaction Cancelled",
                f"'{tx.title}' was cancelled because the buyer did not fund any milestones.",
                "transaction", related_item_id=tx.id, related_item_type="transaction",
            )
        db.commit()
        logger.info(f"Auto-cancelled unfunded transaction {transaction_id}")
        return {"status": "cancelled", "transaction_id": transaction_id}
    except Exception as exc:
        db.rollback()
        raise self.retry(exc=exc)
    finally:
        db.close()


@celery_app.task(
    bind=True,
    max_retries=2,
    name="app.services.tasks.enforce_funding_deadline",
)
def enforce_funding_deadline(self, transaction_id: str) -> dict:
    """
    At transaction funding_deadline:
    - Case A: no milestone fully funded → cancel tx, refund any locked partial funds to buyer available.
    - Case B/C: at least one funded → refund only incomplete-milestone (partial) locks; funded milestones proceed.
    """
    from app.database import SessionLocal
    from datetime import datetime
    from app.models.transaction import Transaction, Milestone
    from app.models.wallet import WalletBalance, WalletTransaction, LedgerEntry
    from app.services.notification_service import create_notification

    db = SessionLocal()
    try:
        tx = db.query(Transaction).filter(Transaction.id == transaction_id).first()
        if not tx:
            return {"status": "not_found"}
        if tx.status in ("completed", "cancelled", "refunded"):
            return {"status": "skipped", "reason": tx.status}
        if not tx.funding_deadline or datetime.utcnow() < tx.funding_deadline:
            return {"status": "not_yet_due"}

        milestones = db.query(Milestone).filter(Milestone.transaction_id == tx.id).all()
        any_fully_funded = any(m.is_funded for m in milestones)

        def refund_milestone_escrow_to_buyer(m: Milestone) -> float:
            amt = float(m.funded_amount or 0)
            if amt <= 0:
                return 0.0
            currency = m.currency or tx.currency
            buyer_balance = db.query(WalletBalance).filter(
                WalletBalance.user_id == tx.buyer_id,
                WalletBalance.currency == currency,
            ).first()
            if buyer_balance:
                buyer_balance.escrow_amount = max(0.0, round((buyer_balance.escrow_amount or 0) - amt, 8))
                buyer_balance.amount = round((buyer_balance.amount or 0) + amt, 8)
            db.add(
                LedgerEntry(
                    debit_user_id=tx.buyer_id,
                    credit_user_id=tx.buyer_id,
                    debit_account="escrow",
                    credit_account="available",
                    amount=amt,
                    currency=currency,
                    reference_type="refund",
                    reference_id=m.id,
                    description=f"Funding deadline refund for milestone '{m.title}'",
                )
            )
            db.add(
                WalletTransaction(
                    user_id=tx.buyer_id,
                    type="escrow_refund",
                    amount=amt,
                    currency=currency,
                    status="completed",
                    transaction_id=tx.id,
                    milestone_id=m.id,
                    description=f"Deadline refund: {m.title}",
                )
            )
            m.funded_amount = 0.0
            m.is_funded = False
            m.status = "pending"
            return amt

        if not any_fully_funded:
            refunded = 0.0
            for m in milestones:
                refunded += refund_milestone_escrow_to_buyer(m)
            tx.funded_milestones = 0
            tx.status = "cancelled"
            tx.funding_deadline = None
            create_notification(
                db,
                tx.buyer_id,
                "Funding deadline — transaction cancelled",
                f"'{tx.title}' had no fully funded milestones by the deadline. Held funds were returned to your wallet.",
                "transaction",
                related_item_id=tx.id,
                related_item_type="transaction",
            )
            if tx.seller_id:
                create_notification(
                    db,
                    tx.seller_id,
                    "Transaction cancelled (funding deadline)",
                    f"'{tx.title}' was cancelled because no milestone was fully funded in time.",
                    "transaction",
                    related_item_id=tx.id,
                    related_item_type="transaction",
                )
            from app.services.agent_fee_service import refund_held_agent_fees_for_transaction

            refund_held_agent_fees_for_transaction(db, tx)
            db.commit()
            return {"status": "cancelled", "refunded": refunded}

        # At least one funded milestone: refund partials on incomplete milestones only
        for m in milestones:
            if not m.is_funded and (m.funded_amount or 0) > 0:
                refund_milestone_escrow_to_buyer(m)
        tx.funding_deadline = None
        create_notification(
            db,
            tx.buyer_id,
            "Funding deadline — partial refunds",
            f"Unfunded portions on '{tx.title}' were returned to your wallet. Funded milestones are unchanged.",
            "transaction",
            related_item_id=tx.id,
            related_item_type="transaction",
        )
        db.commit()
        return {"status": "partial_refunds", "transaction_id": transaction_id}
    except Exception as exc:
        db.rollback()
        raise self.retry(exc=exc)
    finally:
        db.close()


@celery_app.task(
    bind=True,
    max_retries=2,
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


@celery_app.task(
    bind=True,
    max_retries=2,
    name="app.services.tasks.expire_stale_invitations",
)
def expire_stale_invitations(self) -> dict:
    """
    Auto-cancel pending_approval transactions where the invitee never accepted
    within invite_expiry_days. Notifies buyer and seller.
    """
    from datetime import datetime, timedelta
    from app.database import SessionLocal
    from app.models.transaction import Transaction
    from app.services.platform_timeline import get_invite_expiry_days
    from app.services.notification_service import create_notification
    from app.services.agent_fee_service import refund_held_agent_fees_for_transaction

    db = SessionLocal()
    try:
        days = get_invite_expiry_days(db)
        cutoff = datetime.utcnow() - timedelta(days=days)
        txs = (
            db.query(Transaction)
            .filter(Transaction.status == "pending_approval", Transaction.created_at < cutoff)
            .all()
        )
        n = 0
        for tx in txs:
            refund_held_agent_fees_for_transaction(db, tx)
            tx.status = "cancelled"
            create_notification(
                db,
                str(tx.buyer_id),
                "Invitation expired",
                f"'{tx.title}' was cancelled after {days} days with no acceptance.",
                "transaction",
                related_item_id=str(tx.id),
                related_item_type="transaction",
            )
            if tx.seller_id:
                create_notification(
                    db,
                    str(tx.seller_id),
                    "Invitation expired",
                    f"'{tx.title}' was cancelled after {days} days with no acceptance.",
                    "transaction",
                    related_item_id=str(tx.id),
                    related_item_type="transaction",
                )
            n += 1
        db.commit()
        return {"status": "ok", "expired_count": n}
    except Exception as exc:
        db.rollback()
        raise self.retry(exc=exc)
    finally:
        db.close()


@celery_app.task(
    bind=True,
    max_retries=2,
    name="app.services.tasks.stale_deal_activity_warnings",
)
def stale_deal_activity_warnings(self) -> dict:
    """
    One-time notification when active/in_progress deals have had no updates
    for stale_activity_warn_days (clears clutter / prompts parties to act).
    """
    from datetime import datetime, timedelta
    from app.database import SessionLocal
    from app.models.transaction import Transaction
    from app.services.platform_timeline import get_stale_activity_warn_days
    from app.services.notification_service import create_notification

    db = SessionLocal()
    try:
        days = get_stale_activity_warn_days(db)
        cutoff = datetime.utcnow() - timedelta(days=days)
        q = db.query(Transaction).filter(
            Transaction.status.in_(("active", "in_progress")),
            Transaction.updated_at < cutoff,
            Transaction.stale_activity_warn_sent_at.is_(None),
        )
        txs = q.all()
        n = 0
        for tx in txs:
            msg = (
                f"No milestone activity has been recorded on '{tx.title}' for {days}+ days. "
                "Please update the deal, complete milestones, or cancel if the work is abandoned."
            )
            create_notification(
                db,
                str(tx.buyer_id),
                "Stale transaction reminder",
                msg,
                "transaction",
                related_item_id=str(tx.id),
                related_item_type="transaction",
            )
            if tx.seller_id:
                create_notification(
                    db,
                    str(tx.seller_id),
                    "Stale transaction reminder",
                    msg,
                    "transaction",
                    related_item_id=str(tx.id),
                    related_item_type="transaction",
                )
            tx.stale_activity_warn_sent_at = datetime.utcnow()
            n += 1
        db.commit()
        return {"status": "ok", "warned_count": n}
    except Exception as exc:
        db.rollback()
        raise self.retry(exc=exc)
    finally:
        db.close()
