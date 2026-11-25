from pydantic_settings import BaseSettings
from pydantic import model_validator
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
    # S3-compatible providers (e.g. Backblaze B2: https://s3.us-east-005.backblazeb2.com). Empty = AWS.
    AWS_S3_ENDPOINT_URL: str = ""
    # Optional key prefix so several apps can share one bucket, e.g. "bondrova/"
    S3_KEY_PREFIX: str = ""

    # Converts money columns from double precision to exact NUMERIC(28,8) on startup.
    # Off until a database backup has been taken; then set MONEY_PRECISION_MIGRATION=true.
    MONEY_PRECISION_MIGRATION: bool = False
    CDN_BASE_URL: str = ""             # Optional CloudFront URL

    # Redis
    REDIS_URL: str = "redis://localhost:6379/0"
    REDIS_CELERY_DB: int = 1           # Celery uses DB 1
    IDEMPOTENCY_TTL: int = 86400       # 24 hours

    # Celery — leave blank in Railway; auto-derived from REDIS_URL below
    CELERY_BROKER_URL: str = ""
    CELERY_RESULT_BACKEND: str = ""

    # Admin seed
    ADMIN_PASSWORD: str = ""           # Used by seed_admin.py on first deploy

    # Blockchain explorer keys (optional — free-tier rate limits apply without them)
    ETHERSCAN_API_KEY: str = ""        # https://etherscan.io/apis
    BSCSCAN_API_KEY: str = ""          # https://bscscan.com/apis

    # Payment gateways
    PAYSTACK_SECRET_KEY: str = ""      # sk_live_xxx or sk_test_xxx
    PAYSTACK_PUBLIC_KEY: str = ""
    # Paystack Dedicated Virtual Accounts: "wema-bank" or "titan-paystack" ("test-bank" in test mode)
    PAYSTACK_DVA_PREFERRED_BANK: str = "wema-bank"

    # Naira rail: "payhub" (central gateway — one webhook for all products) or "paystack" (direct).
    NGN_PAYMENT_RAIL: str = "payhub"
    PAYHUB_BASE_URL: str = "https://payhub.compasse.net/api/v1"
    PAYHUB_API_KEY: str = ""          # Bondrova's company key from PayHub (sk_live_… / sk_test_…)
    PAYHUB_WEBHOOK_SECRET: str = ""   # whsec_… returned when Bondrova's webhook URL is registered
    # Provider inside PayHub for BOTH Bondrova deposits and payouts — must be the same so payouts
    # are paid from the balance deposits land in. Match PayHub's "Take payments with" setting.
    PAYHUB_PROVIDER: str = "flutterwave"
    PAYHUB_PASS_FEE_TO_PAYER: bool = True  # payer covers PayHub's service fee; full amount is credited

    # Quidax (crypto sub-accounts, deposit addresses, withdrawals). Off until the key is set.
    QUIDAX_SECRET_KEY: str = ""
    QUIDAX_WEBHOOK_SECRET: str = ""
    QUIDAX_BASE_URL: str = "https://www.quidax.com/api/v1"
    # Coins/networks offered for deposits & withdrawals: "CURRENCY:network,..."
    CRYPTO_RAILS: str = "USDT:trc20,USDT:bep20,USDC:bep20,BTC:btc,ETH:erc20"

    # Withdrawals at or below this amount (per currency) are paid out automatically once the
    # destination is usable; larger ones wait for admin approval. JSON; 0 / missing = always manual.
    # New payout destinations can't be used for this many hours after confirmation.
    PAYOUT_DESTINATION_COOLDOWN_HOURS: int = 24
    STRIPE_SECRET_KEY: str = ""        # sk_live_xxx or sk_test_xxx
    STRIPE_WEBHOOK_SECRET: str = ""
    FLUTTERWAVE_SECRET_KEY: str = ""   # FLWSECK_xxx
    FLUTTERWAVE_WEBHOOK_SECRET: str = ""

    # Transactional email (Resend)
    RESEND_API_KEY: str = ""
    DEFAULT_FROM_EMAIL: str = "noreply@bondrova.com"

    # Frontend
    FRONTEND_URL: str = "http://localhost:5173"

    # Admin
    ADMIN_EMAIL: str = "admin@milevault.com"
    ADMIN_SECRET: str = "change-this-admin-bootstrap-secret"
    # Where contact-form tickets are emailed (Cloudflare Email Routing can forward it to Gmail).
    SUPPORT_INBOX_EMAIL: str = "support@bondrova.com"
    # Address support replies/compose are sent FROM (domain must be verified in Resend).
    SUPPORT_FROM_EMAIL: str = "support@bondrova.com"
    # Shared secret between the Cloudflare Email Worker and POST /support/inbound.
    SUPPORT_INBOUND_SECRET: str = ""

    # Rate limiting (requests per window)
    RATE_LIMIT_AUTH: str = "10/minute"
    RATE_LIMIT_API: str = "100/minute"
    RATE_LIMIT_UPLOAD: str = "20/minute"

    # Watermarking
    WATERMARK_TEXT: str = "Bondrova"

    # Milestones / funding deadline (days after seller accepts before auto-cancel rules apply)
    FUNDING_DEADLINE_DAYS: int = 14
    # Buyer review window after delivery (must be 3–7 inclusive per policy)
    AUTO_RELEASE_DAYS: int = 5

    @model_validator(mode="after")
    def derive_celery_urls(self) -> "Settings":
        # When deploying to Railway, only REDIS_URL is injected by the plugin.
        # CELERY_BROKER_URL and CELERY_RESULT_BACKEND are derived here if blank.
        if not self.CELERY_BROKER_URL:
            base = self.REDIS_URL.rsplit("/", 1)[0] if self.REDIS_URL.count("/") > 2 else self.REDIS_URL
            self.CELERY_BROKER_URL = f"{base}/1"
        if not self.CELERY_RESULT_BACKEND:
            base = self.REDIS_URL.rsplit("/", 1)[0] if self.REDIS_URL.count("/") > 2 else self.REDIS_URL
            self.CELERY_RESULT_BACKEND = f"{base}/2"
        return self

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
    def auto_release_days_clamped(self) -> int:
        return max(3, min(7, int(self.AUTO_RELEASE_DAYS)))

    @property
    def s3_enabled(self) -> bool:
        return bool(self.AWS_ACCESS_KEY_ID and self.AWS_SECRET_ACCESS_KEY)

    class Config:
        env_file = ".env"


settings = Settings()
