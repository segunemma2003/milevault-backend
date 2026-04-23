"""
Run once to create the platform superadmin account.
Usage: python scripts/seed_admin.py
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(os.path.dirname(__file__)), ".env"))

from app.database import SessionLocal
from app.models.user import User
from passlib.context import CryptContext

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

ADMIN_EMAIL    = "admin@milevault.ng"
ADMIN_PASSWORD = "Nigeria@60"
ADMIN_FNAME    = "MileVault"
ADMIN_LNAME    = "Admin"


def seed():
    db = SessionLocal()
    try:
        existing = db.query(User).filter(User.email == ADMIN_EMAIL).first()
        if existing:
            if not existing.is_admin:
                existing.is_admin = True
                db.commit()
                print(f"[updated] {ADMIN_EMAIL} — is_admin set to True.")
            else:
                print(f"[skip] Admin {ADMIN_EMAIL} already exists.")
            return

        admin = User(
            first_name=ADMIN_FNAME,
            last_name=ADMIN_LNAME,
            email=ADMIN_EMAIL,
            hashed_password=pwd_context.hash(ADMIN_PASSWORD),
            role="buyer",          # role field; is_admin flag controls access
            is_admin=True,
            is_active=True,
            is_kyc_verified=True,
            country_code="NG",
        )
        db.add(admin)
        db.commit()
        print(f"[created] Admin: {ADMIN_EMAIL} / {ADMIN_PASSWORD}")
    except Exception as e:
        db.rollback()
        print(f"[error] {e}")
    finally:
        db.close()


if __name__ == "__main__":
    seed()
