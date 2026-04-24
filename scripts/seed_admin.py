"""
Creates the platform superadmin account on first run.
Usage: python scripts/seed_admin.py
Reads: ADMIN_EMAIL, ADMIN_PASSWORD, ADMIN_SECRET from environment (or .env).
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(os.path.dirname(__file__)), ".env"))

from app.config import settings
from app.database import SessionLocal, create_tables
from app.models.user import User
from passlib.context import CryptContext

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

ADMIN_EMAIL    = settings.ADMIN_EMAIL or "admin@milevault.com"
ADMIN_PASSWORD = settings.ADMIN_PASSWORD
ADMIN_SECRET   = settings.ADMIN_SECRET


def seed() -> None:
    if not ADMIN_PASSWORD:
        print("[error] ADMIN_PASSWORD is not set. Set it as an environment variable and re-run.")
        sys.exit(1)

    create_tables()

    db = SessionLocal()
    try:
        existing = db.query(User).filter(User.email == ADMIN_EMAIL).first()
        if existing:
            changed = False
            if not existing.is_admin:
                existing.is_admin = True
                changed = True
            if ADMIN_PASSWORD:
                existing.hashed_password = pwd_context.hash(ADMIN_PASSWORD)
                changed = True
            if changed:
                db.commit()
                print(f"[updated] {ADMIN_EMAIL} — admin flag and/or password updated.")
            else:
                print(f"[skip] Admin {ADMIN_EMAIL} already exists and is up to date.")
            return

        admin = User(
            first_name="MileVault",
            last_name="Admin",
            email=ADMIN_EMAIL,
            hashed_password=pwd_context.hash(ADMIN_PASSWORD),
            role="buyer",
            is_admin=True,
            is_active=True,
            is_kyc_verified=True,
            country_code="NG",
        )
        db.add(admin)
        db.commit()
        print(f"[created] Admin: {ADMIN_EMAIL}")
    except Exception as exc:
        db.rollback()
        print(f"[error] {exc}")
        sys.exit(1)
    finally:
        db.close()


if __name__ == "__main__":
    seed()
