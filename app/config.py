from pydantic_settings import BaseSettings
from typing import List
import json


class Settings(BaseSettings):
    # Core
    DATABASE_URL: str = "postgresql://postgres:password@localhost:5432/milevault"
    SECRET_KEY: str = "change-this-secret-key-in-production-min-32-chars!!"
    ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 30
    REFRESH_TOKEN_EXPIRE_DAYS: int = 30
    UPLOAD_DIR: str = "./uploads"
    CORS_ORIGINS: str = '["http://localhost:5173","http://localhost:3000"]'
    ENVIRONMENT: str = "development"  # development | production

    # Cookie settings
    COOKIE_SECURE: bool = False        # True in production (HTTPS only)
    COOKIE_SAMESITE: str = "lax"       # lax | strict | none
    COOKIE_DOMAIN: str = ""            # Set in production e.g. ".milevault.com"

    # AWS S3
    AWS_ACCESS_KEY_ID: str = ""
    AWS_SECRET_ACCESS_KEY: str = ""
    AWS_REGION: str = "us-east-1"
    S3_BUCKET_NAME: str = "milevault-uploads"
    S3_PRESIGNED_EXPIRY: int = 3600    # 1 hour for presigned URLs
    CDN_BASE_URL: str = ""             # Optional CloudFront URL

    # Redis
    REDIS_URL: str = "redis://localhost:6379/0"
    REDIS_CELERY_DB: int = 1           # Celery uses DB 1
    IDEMPOTENCY_TTL: int = 86400       # 24 hours

    # Celery
    CELERY_BROKER_URL: str = "redis://localhost:6379/1"
    CELERY_RESULT_BACKEND: str = "redis://localhost:6379/2"

    # Payment gateways
    PAYSTACK_SECRET_KEY: str = ""      # sk_live_xxx or sk_test_xxx
    PAYSTACK_PUBLIC_KEY: str = ""
    STRIPE_SECRET_KEY: str = ""        # sk_live_xxx or sk_test_xxx
    STRIPE_WEBHOOK_SECRET: str = ""
    FLUTTERWAVE_SECRET_KEY: str = ""   # FLWSECK_xxx
    FLUTTERWAVE_WEBHOOK_SECRET: str = ""

    # Frontend
    FRONTEND_URL: str = "http://localhost:5173"

    # Admin
    ADMIN_EMAIL: str = "admin@milevault.com"
    ADMIN_SECRET: str = "change-this-admin-bootstrap-secret"

    # Rate limiting (requests per window)
    RATE_LIMIT_AUTH: str = "10/minute"
    RATE_LIMIT_API: str = "100/minute"
    RATE_LIMIT_UPLOAD: str = "20/minute"

    # Watermarking
    WATERMARK_TEXT: str = "MileVault"

    @property
    def cors_origins_list(self) -> List[str]:
        try:
            return json.loads(self.CORS_ORIGINS)
        except Exception:
            return ["http://localhost:5173"]

    @property
    def is_production(self) -> bool:
        return self.ENVIRONMENT == "production"

    @property
    def s3_enabled(self) -> bool:
        return bool(self.AWS_ACCESS_KEY_ID and self.AWS_SECRET_ACCESS_KEY)

    class Config:
        env_file = ".env"


settings = Settings()
