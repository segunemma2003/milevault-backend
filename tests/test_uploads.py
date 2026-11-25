"""Uploads: confirm never fails an upload, only the owner can confirm, chat attachments are owned + viewable."""
import os
import uuid

os.environ.setdefault("DATABASE_URL", "sqlite://")

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import app.models  # noqa
import app.models.agent, app.models.currency, app.models.crypto, app.models.content, app.models.payments, app.models.support  # noqa
from app.database import Base
from app.models.transaction import Transaction
from app.models.user import User
from app.routers import messages, uploads
from app.schemas.message import ChatMessageCreate
from app.services import s3_service


@pytest.fixture()
def db():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    s = sessionmaker(bind=engine)()
    yield s
    s.close()


def user(db, email):
    u = User(id=str(uuid.uuid4()), first_name="A", last_name="B", email=email, hashed_password="x", role="buyer")
    db.add(u)
    db.commit()
    return u


def test_confirm_succeeds_even_when_the_queue_is_down(db, monkeypatch):
    u = user(db, "a@x.com")
    from app.services import tasks

    def boom(*a, **k):
        raise ConnectionError("Error 111 connecting to localhost:6379. Connection refused.")
    monkeypatch.setattr(tasks.watermark_image, "delay", boom)
    key = f"{s3_service.key_prefix()}transaction_docs/{u.id}/abc.png"
    out = uploads.confirm_upload(s3_key=key, content_type="image/png", current_user=u)
    assert out["s3_key"] == key and out["watermark_queued"] is False


def test_confirm_rejects_someone_elses_key(db):
    u, other = user(db, "a@x.com"), user(db, "b@x.com")
    with pytest.raises(HTTPException) as e:
        uploads.confirm_upload(s3_key=f"{s3_service.key_prefix()}kyc_documents/{other.id}/abc.png",
                               content_type="image/png", current_user=u)
    assert e.value.status_code == 403


def test_chat_attachments_must_be_the_senders_and_come_back_viewable(db, monkeypatch):
    buyer, seller = user(db, "b@x.com"), user(db, "s@x.com")
    tx = Transaction(id=str(uuid.uuid4()), title="Deal", amount=10, currency="USD", type="milestone",
                     status="approved", buyer_id=buyer.id, seller_id=seller.id)
    db.add(tx)
    db.commit()
    monkeypatch.setattr("app.websocket.manager.publish_channel_event", lambda *a, **k: None)
    monkeypatch.setattr(s3_service, "get_presigned_view_url", lambda k, expires=None: f"https://files.test/{k}?sig=1")

    foreign = f"{s3_service.key_prefix()}transaction_docs/{seller.id}/x.png"
    with pytest.raises(HTTPException) as e:
        messages.send_chat_message(tx.id, ChatMessageCreate(message="hi", attachments=[foreign]), buyer, db)
    assert e.value.status_code == 400

    mine = f"{s3_service.key_prefix()}transaction_docs/{buyer.id}/photo.jpg"
    doc = f"{s3_service.key_prefix()}transaction_docs/{buyer.id}/invoice.pdf"
    out = messages.send_chat_message(tx.id, ChatMessageCreate(message="see", attachments=[mine, doc]), buyer, db)
    a, d = out["attachments"]
    assert a["type"] == "image" and a["url"].startswith("https://files.test/") and a["key"] == mine
    assert d["type"] == "document" and d["name"].endswith(".pdf")
    # Seller reading the thread gets the same viewable attachments.
    thread = messages.get_chat_messages(tx.id, seller, db)
    assert thread[0]["attachments"][0]["url"].startswith("https://files.test/")


# ─── Live face check ──────────────────────────────────────────────────────────

def test_liveness_challenge_is_signed_per_user_and_expires(db, monkeypatch):
    from app.routers import kyc
    u, other = user(db, "a@x.com"), user(db, "b@x.com")
    ch = kyc.issue_liveness_challenge(u.id)
    assert len(ch["actions"]) == 3 and len(ch["code"]) == 4
    data = kyc.read_liveness_challenge(ch["token"], u.id)
    assert data["a"] == ch["actions"] and data["c"] == ch["code"]
    with pytest.raises(HTTPException):
        kyc.read_liveness_challenge(ch["token"], other.id)          # someone else's
    body, sig = ch["token"].rsplit(".", 1)
    with pytest.raises(HTTPException):
        kyc.read_liveness_challenge(body + "." + "0" * len(sig), u.id)  # forged
    import time as _t
    monkeypatch.setattr(_t, "time", lambda: 10 ** 10)
    with pytest.raises(HTTPException):
        kyc.read_liveness_challenge(ch["token"], u.id)              # expired


def test_kyc_needs_the_face_check_for_new_verifications(db):
    from app.models.kyc import KycDocument
    from app.routers.admin_kyc import _recompute
    u = user(db, "a@x.com")
    for t in ("passport", "address_proof"):
        db.add(KycDocument(user_id=u.id, type=t, file_url="s3:k", status="verified"))
    db.commit()
    assert _recompute(db, u) != "verified" and not u.is_kyc_verified
    db.add(KycDocument(user_id=u.id, type="liveness", file_url="s3:v.webm", status="verified"))
    db.commit()
    assert _recompute(db, u) == "verified" and u.is_kyc_verified


def test_kyc_files_are_never_watermarked(db, monkeypatch):
    u = user(db, "a@x.com")
    from app.services import tasks
    called = []
    monkeypatch.setattr(tasks.watermark_video, "delay", lambda *a, **k: called.append(a))
    key = f"{s3_service.key_prefix()}kyc_documents/{u.id}/face.webm"
    out = uploads.confirm_upload(s3_key=key, content_type="video/webm", current_user=u)
    assert out["s3_key"] == key and not called
