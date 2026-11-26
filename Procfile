web: uvicorn app.main:app --host 0.0.0.0 --port $PORT --timeout-keep-alive 75
worker: celery -A app.services.celery_app worker -B --loglevel=info --concurrency=2 -Q default,media,notifications,kyc,payments --schedule /tmp/celerybeat-schedule
