"""Role-specific admins: super / finance / support."""
import os
import uuid

os.environ.setdefault("DATABASE_URL", "sqlite://")

import pytest
from fastapi import BackgroundTasks, HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import app.models  # noqa
import app.models.agent, app.models.currency, app.models.crypto, app.models.content, app.models.payments, app.models.support  # noqa
from app.database import Base
from app.dependencies import admin_can, admin_role_of, require_admin
from app.models.user import User
from app.routers import admin_team


@pytest.fixture()
def db():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    s = sessionmaker(bind=engine)()
    yield s
    s.close()


def mk(db, email, role=None, admin=True):
    u = User(id=str(uuid.uuid4()), first_name="A", last_name="B", email=email, hashed_password="x",
             role="buyer", is_admin=admin, admin_role=role, is_active=True)
    db.add(u)
    db.commit()
    return u


def test_role_areas(db):
    legacy, fin, sup, user = mk(db, "l@x.com"), mk(db, "f@x.com", "finance"), mk(db, "s@x.com", "support"), mk(db, "u@x.com", admin=False)
    assert admin_role_of(legacy) == "super" and admin_role_of(user) is None
    assert admin_can(legacy, "settings") and admin_can(legacy, "finance")
    assert admin_can(fin, "finance") and admin_can(fin, "view")
    assert not admin_can(fin, "support") and not admin_can(fin, "settings") and not admin_can(fin, "content")
    assert admin_can(sup, "support") and admin_can(sup, "content") and admin_can(sup, "view")
    assert not admin_can(sup, "finance") and not admin_can(sup, "settings")


def test_require_admin_blocks_the_wrong_role(db):
    fin, sup = mk(db, "f@x.com", "finance"), mk(db, "s@x.com", "support")
    assert require_admin("finance")(current_user=fin) is fin
    with pytest.raises(HTTPException) as e:
        require_admin("finance")(current_user=sup)   # support can't approve withdrawals
    assert e.value.status_code == 403 and e.value.detail["error"] == "ADMIN_ROLE_FORBIDDEN"
    with pytest.raises(HTTPException):
        require_admin("settings")(current_user=fin)  # finance can't change fees


def test_withdrawal_approval_is_finance_only():
    """The approve/reject withdrawal routes require the finance area (super passes too)."""
    from app.routers import admin
    routes = {(m, r.path): r for r in admin.router.routes if hasattr(r, "methods") for m in r.methods}
    for path in ("/admin/withdrawals/{txn_id}/approve", "/admin/withdrawals/{txn_id}/reject"):
        r = routes[("PUT", path)]
        deps = [d.call for d in r.dependant.dependencies]
        guard = [d for d in deps if getattr(d, "__qualname__", "").startswith("require_admin")]
        assert guard, path
        assert guard[0].__closure__[0].cell_contents == "finance", path


def test_add_change_and_remove_team_members(db, monkeypatch):
    monkeypatch.setattr(admin_team, "_send_invite", lambda *a, **k: None)
    boss = mk(db, "boss@x.com", "super")
    bg = BackgroundTasks()
    out = admin_team.add_member(admin_team.TeamMemberIn(email="Finance@Bondrova.com", role="finance"), bg, db, boss)
    assert out["created"] and out["role"] == "finance" and out["email"] == "finance@bondrova.com"
    assert "/reset-password?token=" in out["setup_link"] and len(bg.tasks) == 1
    fin = db.query(User).filter_by(email="finance@bondrova.com").one()
    assert fin.is_admin and fin.is_email_verified and fin.password_reset_token

    assert admin_team.change_role(fin.id, admin_team.RoleIn(role="support"), db, boss)["role"] == "support"
    with pytest.raises(HTTPException):
        admin_team.change_role(fin.id, admin_team.RoleIn(role="owner"), db, boss)
    # the only super admin can't be demoted or removed, nor remove themselves
    with pytest.raises(HTTPException) as e:
        admin_team.change_role(boss.id, admin_team.RoleIn(role="finance"), db, boss)
    assert e.value.status_code == 409
    with pytest.raises(HTTPException):
        admin_team.remove_member(boss.id, db, boss)
    assert "removed" in admin_team.remove_member(fin.id, db, boss)["message"]
    db.refresh(fin)
    assert not fin.is_admin and fin.admin_role is None


def test_verified_name_is_locked_and_support_can_correct_it(db):
    from app.routers import users, admin_kyc
    from app.schemas.user import UpdateProfileRequest
    u = mk(db, "u@x.com", admin=False)
    u.first_name, u.last_name = "Segun", "Test"
    db.commit()
    users.update_profile(UpdateProfileRequest(first_name="Seg"), u, db)          # not verified yet: editable
    assert u.first_name == "Seg"
    u.is_kyc_verified = True
    db.commit()
    with pytest.raises(HTTPException) as e:
        users.update_profile(UpdateProfileRequest(last_name="Bamidele"), u, db)
    assert e.value.detail["error"] == "NAME_LOCKED"
    users.update_profile(UpdateProfileRequest(first_name="Seg", bio="hi"), u, db)  # unchanged name + other fields ok
    sup = mk(db, "s@x.com", "support")
    admin_kyc.set_legal_name(u.id, admin_kyc.LegalNameIn(first_name="Segun", last_name="Bamidele"), db, sup)
    assert (u.first_name, u.last_name) == ("Segun", "Bamidele")
    from app.services.payment_flows import names_match
    assert names_match(u.first_name, u.last_name, "SEGUN EMMANUEL BAMIDELE")


# ─── BVN via PayHub ──────────────────────────────────────────────────────────

def test_bvn_flow_verified_duplicate_and_kyc_requirement(db, monkeypatch):
    from app.routers import kyc, admin_kyc
    from app.services import payhub
    from app.models.kyc import KycDocument
    monkeypatch.setattr(payhub, "enabled", lambda: True)
    calls = []
    monkeypatch.setattr(payhub, "verify_bvn", lambda **k: calls.append(k) or {"id": f"chk-{len(calls)}", "status": "pending"})
    a = mk(db, "a@x.com", admin=False); a.first_name, a.last_name = "Ada", "Obi"
    b = mk(db, "b@x.com", admin=False); b.first_name, b.last_name = "Bo", "Ade"
    db.commit()

    with pytest.raises(HTTPException):
        kyc.submit_bvn(kyc.BvnIn(bvn="123", bank_code="058", account_number="0123456789"), a, db)
    out = kyc.submit_bvn(kyc.BvnIn(bvn="222-3334-4455", bank_code="058", account_number="0123456789"), a, db)
    assert out["bvn"]["status"] == "pending" and calls[0]["bvn"] == "22233344455" and a.bvn_last4 == "4455"

    # Documents approved but BVN still pending → not verified yet (Nigerian user)
    for t in ("passport", "address_proof", "liveness"):
        db.add(KycDocument(user_id=a.id, type=t, file_url="s3:k", status="verified"))
    db.commit()
    admin_kyc._recompute(db, a)
    assert not a.is_kyc_verified

    assert kyc.handle_identity_event(db, "identity.verified", {"id": "chk-1", "external_ref": a.id, "bvn_fingerprint": "fp1"}) == "verified"
    db.refresh(a)
    assert a.bvn_status == "verified" and a.is_kyc_verified   # BVN completed the KYC

    # Same BVN on a second account is refused
    kyc.submit_bvn(kyc.BvnIn(bvn="22233344455", bank_code="058", account_number="0123456780"), b, db)
    assert kyc.handle_identity_event(db, "identity.verified", {"id": "chk-2", "external_ref": b.id, "bvn_fingerprint": "fp1"}) == "failed"
    db.refresh(b)
    assert "another Bondrova account" in b.bvn_failure_reason
    # A failed check can be retried; a stale result for an old check is ignored
    kyc.submit_bvn(kyc.BvnIn(bvn="99988877766", bank_code="058", account_number="0123456780"), b, db)
    assert kyc.handle_identity_event(db, "identity.failed", {"id": "chk-2", "external_ref": b.id}) == "stale_check"


def test_receipt_copy_goes_to_the_company_inbox(db, monkeypatch):
    from app.services import receipts
    from app.models.currency import PlatformSettings
    db.add(PlatformSettings(id="default", support_email="support@bondrova.com", receipts_copy_email="finance@bondrova.com"))
    db.commit()
    sent = []
    import app.routers.auth as auth
    monkeypatch.setattr(auth, "_send_email_direct", lambda to, subject, body: sent.append((to, subject)))
    monkeypatch.setattr(receipts.threading, "Thread", lambda target, daemon: type("T", (), {"start": lambda self: target()})())
    receipts.send_copy(db, kind="Deposit", user_name="Ada Obi", user_email="a@x.com",
                       lines=[("Added to wallet", "NGN 100,000.00"), ("Reference", "phb_1")])
    assert sent and sent[0][0] == "finance@bondrova.com" and "Deposit" in sent[0][1]
