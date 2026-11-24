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
