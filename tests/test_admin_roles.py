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
