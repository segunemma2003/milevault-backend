from sqlalchemy import create_engine
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker
from app.config import settings

# Railway Postgres requires SSL; add sslmode=require if not already present
_db_url = settings.DATABASE_URL
if "railway" in _db_url and "sslmode" not in _db_url:
    _db_url += "?sslmode=require"

engine = create_engine(_db_url)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def create_tables():
    from app.models import user, transaction, wallet, dispute, message, kyc, notification  # noqa
    Base.metadata.create_all(bind=engine)
