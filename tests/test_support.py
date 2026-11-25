import os
import uuid

os.environ.setdefault("DATABASE_URL", "sqlite://")

import pytest
from fastapi import BackgroundTasks
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import app.models  # noqa
import app.models.agent, app.models.currency, app.models.crypto, app.models.content, app.models.payments, app.models.support  # noqa
from app.database import Base
from app.models.support import SupportReply, SupportTicket
from app.models.user import User
from app.routers import contact


@pytest.fixture()
def db():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    s = sessionmaker(bind=engine)()
    yield s
    s.close()


def test_contact_creates_ticket_and_queues_emails(db):
    bg = BackgroundTasks()
    out = contact.submit_contact(contact.ContactRequest(name="Ada Obi", email="Ada@Example.com", subject="Help",
                                                        message="I need help with my escrow <b>now</b>"), bg, db)
    t = db.query(SupportTicket).one()
    assert t.reference == out["reference"] and t.email == "ada@example.com" and t.status == "open"
    assert len(bg.tasks) == 2  # support inbox + customer acknowledgement


def test_admin_reply_marks_answered(db):
    admin = User(id=str(uuid.uuid4()), first_name="A", last_name="D", email="a@x.com", hashed_password="x",
                 role="buyer", is_admin=True)
    db.add(admin)
    db.commit()
    contact.submit_contact(contact.ContactRequest(name="Ada Obi", email="ada@x.com", subject="Hi",
                                                  message="Question about fees please"), BackgroundTasks(), db)
    t = db.query(SupportTicket).one()
    bg = BackgroundTasks()
    contact.reply(t.id, bg, "Fees are 2.5%.", False, db, admin)
    db.refresh(t)
    assert t.status == "answered" and db.query(SupportReply).count() == 1 and len(bg.tasks) == 1


# ─── Inbound email & compose ──────────────────────────────────────────────────

import asyncio
from email.message import EmailMessage

from fastapi import HTTPException

from app.config import settings


class _Req:
    def __init__(self, raw: bytes):
        self._raw = raw

    async def body(self):
        return self._raw


def _mail(frm, subject, text):
    m = EmailMessage()
    m["From"], m["To"], m["Subject"] = frm, "support@bondrova.com", subject
    m.set_content(text)
    return m.as_bytes()


def _inbound(db, raw, secret="s3cret"):
    return asyncio.run(contact.inbound_email(_Req(raw), db, secret))


def test_inbound_new_email_creates_ticket_and_reply_threads(db, monkeypatch):
    monkeypatch.setattr(settings, "SUPPORT_INBOUND_SECRET", "s3cret")
    out = _inbound(db, _mail("Ada Obi <ada@gmail.com>", "Where is my money?", "Hello, I funded a deal yesterday."))
    assert out["outcome"] == "ticket_created"
    t = db.query(SupportTicket).one()
    assert t.email == "ada@gmail.com" and t.name == "Ada Obi" and "funded a deal" in t.message

    reply = _mail("ada@gmail.com", f"Re: [{t.reference}] Where is my money?",
                  "Thanks, got it now.\n\nOn Tue, 24 Nov 2026 Bondrova Support wrote:\n> old text")
    assert _inbound(db, reply)["outcome"] == "reply_added"
    r = db.query(SupportReply).one()
    assert r.author_role == "customer" and r.body == "Thanks, got it now."


def test_inbound_ignores_own_domain_and_bad_secret(db, monkeypatch):
    monkeypatch.setattr(settings, "SUPPORT_INBOUND_SECRET", "s3cret")
    assert _inbound(db, _mail("noreply@bondrova.com", "[BDR-AAAAAA] alert", "x"))["outcome"] == "ignored_own_domain"
    with pytest.raises(HTTPException):
        _inbound(db, _mail("a@b.com", "hi", "x"), secret="wrong")
    assert db.query(SupportTicket).count() == 0


def test_compose_creates_outbound_ticket(db):
    admin = User(id=str(uuid.uuid4()), first_name="A", last_name="D", email="adm@x.com", hashed_password="x",
                 role="buyer", is_admin=True)
    db.add(admin)
    db.commit()
    bg = BackgroundTasks()
    out = contact.compose(contact.ComposeRequest(to="Client@Example.com", subject="Your KYC", body="Please re-upload."), bg, db, admin)
    t = db.query(SupportTicket).one()
    assert t.reference == out["reference"] and t.email == "client@example.com" and t.status == "answered"
    assert db.query(SupportReply).count() == 1 and len(bg.tasks) == 1
