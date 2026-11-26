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
        from app.services.s3_service import add_watermark_to_image, _get_s3_client

        if not settings.s3_enabled:
            logger.info(f"S3 not configured — skipping watermark for {s3_key}")
            return {"status": "skipped", "key": s3_key}

        s3 = _get_s3_client()
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
        from app.services.s3_service import _get_s3_client
        s3 = _get_s3_client()
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
        from app.services.email_template import render_email, sender as email_sender
        resend.api_key = settings.RESEND_API_KEY
        resend.Emails.send({
            "from": email_sender(),
            "to": [to_email],
            "subject": subject,
            "html": render_email(body_html),
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

        from app.services.delivery_validation import delivery_meets_auto_release_bar

        if not delivery_meets_auto_release_bar(milestone):
            logger.warning("auto_release_milestone skipped: delivery failed minimum validation for %s", milestone_id)
            return {"status": "skipped", "reason": "delivery_failed_auto_release_bar"}

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

        # Refund only THIS transaction's partially-funded milestones. (Previously this moved
        # the buyer's ENTIRE escrow balance — including money locked for other deals — back
        # to available.) Exactly once per transaction.
        from app.services import ledger
        currency = tx.currency
        partial = db.query(Milestone).filter(
            Milestone.transaction_id == tx.id,
            Milestone.status != "completed",
        ).with_for_update().all()
        refund_amount = sum(float(m.funded_amount or 0) for m in partial)
        if refund_amount > 0:
            applied = ledger.move(
                db, user_id=tx.buyer_id, currency=currency, amount=refund_amount,
                from_bucket="escrow", to_bucket="available",
                reference_type="funding_deadline_refund", reference_id=transaction_id,
                description="Auto-refund: transaction cancelled due to funding deadline",
                idempotency_key=transaction_id,
            )
            if applied:
                for m in partial:
                    m.funded_amount = 0.0
                    m.is_funded = False

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
    from app.services.funding_deadline_enforcement import enforce_funding_deadline_if_due

    db = SessionLocal()
    try:
        out = enforce_funding_deadline_if_due(db, transaction_id)
        if out.get("commit"):
            db.commit()
        return out
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

        if refund.status == "completed":
            return {"status": "already_completed", "refund_id": refund_id}

        # A refund returns money that is held in the BUYER'S ESCROW for this transaction.
        # (Previously it credited the recipient from nowhere and left escrow untouched,
        # so the same funds could be paid out twice.) Exactly once per refund id.
        from app.models.transaction import Transaction
        from app.models.wallet import WalletTransaction as WalletTx
        from app.services import ledger
        from app.services.ledger import InsufficientFunds

        tx = db.query(Transaction).filter(Transaction.id == refund.transaction_id).first()
        if not tx:
            refund.status = "failed"
            refund.admin_notes = ((refund.admin_notes or "") + " | Transaction not found").strip(" |")
            db.commit()
            return {"status": "failed", "reason": "transaction_not_found"}

        try:
            applied = ledger.transfer(
                db, currency=refund.currency, amount=refund.amount,
                from_user_id=str(tx.buyer_id), from_bucket="escrow",
                to_user_id=str(refund.refund_to), to_bucket="available",
                reference_type="refund", reference_id=str(refund.id),
                description=f"Dispute refund {str(refund.id)[:8]}",
                idempotency_key=str(refund.id),
            )
        except InsufficientFunds as e:
            db.rollback()
            refund = db.query(Refund).filter(Refund.id == refund_id).first()
            refund.status = "failed"
            refund.admin_notes = (
                (refund.admin_notes or "")
                + f" | Escrow shortfall: buyer escrow {e.have} < refund {e.need}. Not applied."
            ).strip(" |")
            db.commit()
            logger.error("Refund %s not applied: escrow shortfall (%s < %s)", refund_id, e.have, e.need)
            return {"status": "failed", "reason": "escrow_shortfall"}

        if not applied:
            refund.status = "completed"
            db.commit()
            return {"status": "already_completed", "refund_id": refund_id}

        wallet_tx = WalletTx(
            user_id=str(refund.refund_to),
            type="refund",
            amount=refund.amount,
            currency=refund.currency,
            status="completed",
            transaction_id=str(refund.transaction_id),
            milestone_id=str(refund.milestone_id) if getattr(refund, "milestone_id", None) else None,
            description=f"Refund from dispute — Ref: {refund_id[:8]}",
        )
        db.add(wallet_tx)

        from datetime import datetime
        refund.status = "completed"
        refund.processed_at = datetime.utcnow()
        from app.services.notification_service import create_notification
        create_notification(
            db, str(refund.refund_to), "Refund received",
            f"{refund.amount:,.2f} {refund.currency} from a resolved dispute has been returned to your wallet.",
            "payment", related_item_id=str(refund.transaction_id) if getattr(refund, "transaction_id", None) else None,
            related_item_type="transaction",
        )
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
            # Deterministic: pending_approval never had an active agent engagement on-platform;
            # always refund any stray held agent fee rows before cancel (no-op if none).
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
            Transaction.status.in_(("active", "in_progress", "awaiting_funding")),
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


@celery_app.task(
    bind=True,
    max_retries=1,
    name="app.services.tasks.scan_crypto_deposits",
)
def scan_crypto_deposits(self) -> dict:
    """
    Periodic task: poll each configured crypto deposit address for new incoming transactions.
    Detected transactions are saved as CryptoPendingDeposit records and admin is notified.
    Run every ~5 minutes via Celery beat.
    """
    from app.database import SessionLocal
    from app.models.crypto import CryptoDepositAddress, CryptoPendingDeposit, NETWORK_CURRENCIES
    from app.services.crypto_scanner import scan_network
    from app.services.notification_service import create_notification
    from app.config import settings
    from datetime import datetime

    db = SessionLocal()
    total_new = 0
    try:
        addresses = db.query(CryptoDepositAddress).filter(CryptoDepositAddress.is_active == True).all()
        scan_config = {
            "etherscan_key": getattr(settings, "ETHERSCAN_API_KEY", ""),
            "bscscan_key": getattr(settings, "BSCSCAN_API_KEY", ""),
        }

        for dep_addr in addresses:
            try:
                txs = scan_network(
                    dep_addr.network,
                    dep_addr.address,
                    dep_addr.last_scanned_cursor,
                    scan_config,
                )
            except Exception as e:
                logger.warning(f"scan_network({dep_addr.network}) failed: {e}")
                continue

            new_cursor = dep_addr.last_scanned_cursor
            for tx in txs:
                tx_hash = tx.get("tx_hash", "")
                if not tx_hash:
                    continue
                already = db.query(CryptoPendingDeposit).filter(
                    CryptoPendingDeposit.tx_hash == tx_hash
                ).first()
                if already:
                    continue

                deposit = CryptoPendingDeposit(
                    network=dep_addr.network,
                    tx_hash=tx_hash,
                    from_address=tx.get("from_address"),
                    to_address=dep_addr.address,
                    amount_crypto=tx.get("amount_crypto", 0),
                    currency=NETWORK_CURRENCIES.get(dep_addr.network, "CRYPTO"),
                    confirmations=tx.get("confirmations", 0),
                    block_number=tx.get("block_number", ""),
                    status="detected",
                )
                db.add(deposit)
                total_new += 1
                if new_cursor != tx_hash:
                    new_cursor = tx_hash
                logger.info(f"New crypto deposit detected: {tx_hash} ({dep_addr.network})")

            dep_addr.last_scanned_at = datetime.utcnow()
            if new_cursor:
                dep_addr.last_scanned_cursor = new_cursor

        db.commit()

        if total_new > 0:
            # Notify all admins
            from app.models.user import User
            admins = db.query(User).filter(User.is_admin == True, User.is_active == True).all()
            for admin in admins:
                create_notification(
                    db, admin.id,
                    "New Crypto Deposits Detected",
                    f"{total_new} new incoming crypto transaction(s) detected and awaiting approval.",
                    "payment",
                )
            db.commit()

        return {"status": "ok", "new_deposits": total_new}
    except Exception as exc:
        db.rollback()
        raise self.retry(exc=exc)
    finally:
        db.close()


@celery_app.task(
    bind=True,
    max_retries=2,
    default_retry_delay=60,
    name="app.services.tasks.cleanup_unverified_accounts",
)
def cleanup_unverified_accounts(self) -> dict:
    """
    Delete unverified accounts whose email verification window has expired.
    This enforces strict verification TTL even if users never return to the app.
    """
    from datetime import datetime
    from app.database import SessionLocal
    db = SessionLocal()
    try:
        from app.models.user import User
        from app.models.currency import PlatformSettings

        settings_row = db.query(PlatformSettings).filter(PlatformSettings.id == "default").first()
        if settings_row and not bool(getattr(settings_row, "require_email_verification", True)):
            return {"status": "skipped", "reason": "email_verification_disabled", "deleted": 0}

        expired = (
            db.query(User)
            .filter(
                User.is_email_verified == False,
                User.email_verification_expires_at.isnot(None),
                User.email_verification_expires_at < datetime.utcnow(),
            )
            .all()
        )
        count = len(expired)
        for u in expired:
            db.delete(u)
        db.commit()
        if count:
            logger.info("Deleted %s expired unverified accounts", count)
        return {"status": "ok", "deleted": count}
    except Exception as exc:
        db.rollback()
        raise self.retry(exc=exc)
    finally:
        db.close()


@celery_app.task(bind=True, max_retries=1, name="app.services.tasks.reconcile_ledger")
def reconcile_ledger(self) -> dict:
    """Nightly: every stored balance must equal what the ledger says. Alerts admins otherwise."""
    from app.database import SessionLocal
    from app.models.notification import Notification
    from app.models.user import User
    from app.services.reconciliation import compute_mismatches, negative_balances

    db = SessionLocal()
    try:
        mismatches = compute_mismatches(db)
        negatives = negative_balances(db)
        if mismatches or negatives:
            logger.error("LEDGER RECONCILIATION: %d mismatches, %d negative balances. Top: %s",
                         len(mismatches), len(negatives), (mismatches + negatives)[:5])
            msg = (f"{len(mismatches)} balance(s) don't match the ledger and {len(negatives)} are negative. "
                   "Review before processing withdrawals.")
            for admin in db.query(User).filter(User.is_admin.is_(True)).all():
                db.add(Notification(user_id=str(admin.id), title="Ledger reconciliation alert", message=msg, type="admin"))
            db.commit()
        else:
            logger.warning("LEDGER RECONCILIATION OK: all balances match the ledger.")
        return {"mismatches": len(mismatches), "negatives": len(negatives), "sample": mismatches[:10]}
    finally:
        db.close()


@celery_app.task(bind=True, max_retries=1, name="app.services.tasks.poll_processing_payouts")
def poll_processing_payouts(self) -> dict:
    """Every 10 min: settle in-flight payouts whose webhook we may have missed."""
    from app.database import SessionLocal
    from app.services.payment_flows import poll_processing_payouts as _poll
    db = SessionLocal()
    try:
        return _poll(db)
    finally:
        db.close()


@celery_app.task(name="app.services.tasks.recompute_trust_scores")
def recompute_trust_scores() -> dict:
    """Nightly: refresh every active user's Trust Score (account age, review smoothing, …)."""
    from app.database import SessionLocal
    from app.models.user import User
    from app.services import trust_score
    db = SessionLocal()
    n = 0
    try:
        for u in db.query(User).filter(User.is_active.is_(True)).all():
            trust_score.recompute(db, u, "Daily refresh")
            n += 1
            if n % 200 == 0:
                db.commit()
        db.commit()
        return {"updated": n}
    finally:
        db.close()


@celery_app.task(name="app.services.tasks.purge_idempotency_records")
def purge_idempotency_records() -> dict:
    from datetime import datetime, timedelta
    from app.database import SessionLocal
    from app.models.wallet import IdempotencyRecord
    db = SessionLocal()
    try:
        n = db.query(IdempotencyRecord).filter(IdempotencyRecord.created_at < datetime.utcnow() - timedelta(hours=48)).delete()
        db.commit()
        return {"deleted": n}
    finally:
        db.close()
