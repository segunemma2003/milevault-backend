import os
import uuid

os.environ.setdefault("DATABASE_URL", "sqlite://")

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import app.models  # noqa
import app.models.agent, app.models.currency, app.models.crypto, app.models.content, app.models.payments, app.models.support  # noqa
from app.database import Base
from app.models.notification import Notification
from app.models.user import User
from app.services import notification_dispatch as nd
from app.services.notification_service import create_notification


@pytest.fixture()
def db(monkeypatch):
    sent = []

    class _SyncPool:
        def submit(self, fn, n):
            sent.append(n)

    monkeypatch.setattr(nd, "_pool", _SyncPool())
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    s = sessionmaker(bind=engine)()
    s.sent = sent
    u = User(id=str(uuid.uuid4()), first_name="A", last_name="B", email="a@x.com", hashed_password="x", role="buyer")
    s.add(u)
    s.commit()
    sent.clear()
    s.uid = u.id
    yield s
    s.close()


def test_delivers_after_commit_from_any_path(db):
    create_notification(db, db.uid, "Deposit received", "NGN 500 added", "payment")
    db.add(Notification(user_id=db.uid, title="Account frozen", message="Contact support", type="admin"))  # _notify-style
    db.commit()
    assert [n["title"] for n in db.sent] == ["Deposit received", "Account frozen"]


def test_rollback_delivers_nothing(db):
    db.add(Notification(user_id=db.uid, title="Should not send", message="x", type="payment"))
    db.flush()
    db.rollback()
    db.commit()
    assert db.sent == []


def test_link_mapping():
    base = nd._link_for({"type": "payment"})
    assert base.endswith("/dashboard/wallet")
    assert nd._link_for({"type": "x", "related_item_type": "transaction", "related_item_id": "T1"}).endswith("/dashboard/transactions/T1")
