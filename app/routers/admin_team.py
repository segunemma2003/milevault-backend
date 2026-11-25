"""
Admin team: role-specific admins.

  super   — everything (fees & settings, gateways, admin team, force status)
  finance — withdrawals, refunds, wallet freezes, transfer reversals, currencies & rates,
            agent payouts, crypto deposits/withdrawals (+ read-only overviews)
  support — KYC review, support inbox, disputes, agents & agent requests, account
            deactivation, blog & careers (+ read-only overviews)

Only a super admin manages the team. A new admin gets an email with a link to set their
password; the same link is returned to the super admin so it can be shared another way if
that mailbox doesn't receive mail yet.
"""
import secrets
import uuid
from datetime import datetime, timedelta

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from pydantic import BaseModel, EmailStr
from sqlalchemy.orm import Session

from app.config import settings
from app.database import get_db
from app.dependencies import ADMIN_ROLES, ROLE_AREAS, admin_role_of, get_current_admin, require_admin
from app.models.user import User

router = APIRouter(prefix="/admin/team", tags=["Admin — Team"])

INVITE_TTL = timedelta(hours=48)
ROLE_LABEL = {"super": "Super admin", "finance": "Finance admin", "support": "Support admin"}


class TeamMemberIn(BaseModel):
    email: EmailStr
    role: str
    first_name: str = ""
    last_name: str = ""


class RoleIn(BaseModel):
    role: str


def _role_or_422(role: str) -> str:
    r = (role or "").strip().lower()
    if r not in ADMIN_ROLES:
        raise HTTPException(status_code=422, detail={"error": "INVALID_ROLE", "message": f"Role must be one of {', '.join(ADMIN_ROLES)}."})
    return r


def _member(u: User) -> dict:
    return {"id": u.id, "email": u.email, "first_name": u.first_name, "last_name": u.last_name,
            "role": admin_role_of(u), "is_active": u.is_active, "last_login": getattr(u, "last_login", None),
            "created_at": u.created_at}


def _supers(db: Session) -> int:
    return sum(1 for u in db.query(User).filter(User.is_admin.is_(True), User.is_active.is_(True)).all()
               if admin_role_of(u) == "super")


def _send_invite(to_email: str, first_name: str, role: str, link: str) -> None:
    import html
    from app.routers.auth import _send_email_direct
    from app.services.email_template import button
    body = (f"<h2>You've been added to the Bondrova admin team</h2>"
            f"<p>Hi {html.escape(first_name or 'there')},</p>"
            f"<p>You now have <strong>{ROLE_LABEL.get(role, role)}</strong> access to Bondrova.</p>"
            f"<p>{button(link, 'Set your password')}</p>"
            f"<p>This link expires in 48 hours. Then sign in at {settings.FRONTEND_URL}/admin/login.</p>")
    _send_email_direct(to_email, "Your Bondrova admin access", body)


@router.get("/me", summary="My admin role and what it can access")
def my_role(current_user: User = Depends(get_current_admin)):
    role = admin_role_of(current_user)
    return {"role": role, "areas": sorted(ROLE_AREAS.get(role, set()))}


@router.get("", summary="List admins")
def list_team(db: Session = Depends(get_db), _: User = Depends(require_admin("settings"))):
    admins = db.query(User).filter(User.is_admin.is_(True)).order_by(User.created_at).all()
    return [_member(u) for u in admins]


@router.post("", status_code=201, summary="Add an admin (or give an existing user an admin role)")
def add_member(body: TeamMemberIn, background: BackgroundTasks, db: Session = Depends(get_db),
               _: User = Depends(require_admin("settings"))):
    from app.routers.auth import _hash_email_token
    role = _role_or_422(body.role)
    email = body.email.lower().strip()
    user = db.query(User).filter(User.email == email).first()
    created = False
    if not user:
        from app.services.auth_service import hash_password
        user = User(id=str(uuid.uuid4()), email=email, first_name=body.first_name.strip() or role.capitalize(),
                    last_name=body.last_name.strip() or "Admin", role="buyer",
                    hashed_password=hash_password(secrets.token_urlsafe(24)),  # unusable until they set one
                    is_active=True, is_email_verified=True, country_code="NG")
        db.add(user)
        db.flush()
        from app.models.user import UserSettings
        db.add(UserSettings(user_id=user.id))
        created = True
    user.is_admin, user.admin_role, user.is_active = True, role, True
    raw = secrets.token_urlsafe(32)
    user.password_reset_token = _hash_email_token(raw)
    user.password_reset_expires_at = datetime.utcnow() + INVITE_TTL
    db.commit()
    db.refresh(user)
    link = f"{settings.FRONTEND_URL}/reset-password?token={raw}"
    background.add_task(_send_invite, user.email, user.first_name, role, link)
    return {**_member(user), "created": created, "setup_link": link,
            "message": f"{ROLE_LABEL[role]} access given to {user.email}. A set-password email is on its way."}


@router.put("/{user_id}", summary="Change an admin's role")
def change_role(user_id: str, body: RoleIn, db: Session = Depends(get_db),
                current: User = Depends(require_admin("settings"))):
    role = _role_or_422(body.role)
    user = db.get(User, user_id)
    if not user or not user.is_admin:
        raise HTTPException(status_code=404, detail={"error": "NOT_FOUND", "message": "Admin not found."})
    if admin_role_of(user) == "super" and role != "super" and _supers(db) <= 1:
        raise HTTPException(status_code=409, detail={"error": "LAST_SUPER_ADMIN", "message": "There must always be at least one super admin."})
    user.admin_role = role
    db.commit()
    return _member(user)


@router.delete("/{user_id}", summary="Remove admin access (the account itself stays)")
def remove_member(user_id: str, db: Session = Depends(get_db), current: User = Depends(require_admin("settings"))):
    user = db.get(User, user_id)
    if not user or not user.is_admin:
        raise HTTPException(status_code=404, detail={"error": "NOT_FOUND", "message": "Admin not found."})
    if user.id == current.id:
        raise HTTPException(status_code=409, detail={"error": "CANNOT_REMOVE_SELF", "message": "You can't remove your own admin access."})
    if admin_role_of(user) == "super" and _supers(db) <= 1:
        raise HTTPException(status_code=409, detail={"error": "LAST_SUPER_ADMIN", "message": "There must always be at least one super admin."})
    user.is_admin, user.admin_role = False, None
    db.commit()
    return {"message": f"Admin access removed from {user.email}."}
