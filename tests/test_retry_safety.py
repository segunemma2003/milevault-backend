"""
A request whose data is already committed must not come back as an error — the user (or the web
app) would retry it and create the same thing twice.
"""
from app.models.currency import Refund
from app.routers import admin as A
from app.services import tasks
from tests.test_deal_rules import db, deal, mk  # noqa: F401 — db is a fixture


class _BrokerDown:
    def __init__(self):
        self.ran_inline = []

    def delay(self, *_a, **_k):
        raise ConnectionError("Redis connection reset")

    def __call__(self, refund_id):
        self.ran_inline.append(refund_id)


def test_refund_is_created_once_even_when_the_job_queue_is_down(db, monkeypatch):
    b, s = mk(db, "buyer@x.com"), mk(db, "seller@x.com")
    admin = mk(db, "admin@x.com")
    tx, _ = deal(db, b, s, 50)
    fake = _BrokerDown()
    monkeypatch.setattr(tasks, "process_refund", fake)

    out = A.create_refund(transaction_id=tx.id, dispute_id=None, milestone_id=None, refund_to_user_id=b.id,
                          amount=10, currency="USD", reason="Dispute outcome", admin_notes=None, db=db, admin=admin)

    assert out["refund_id"]                         # success response, not a 500
    assert db.query(Refund).count() == 1
    assert fake.ran_inline == [out["refund_id"]]    # processed inline instead of being lost
