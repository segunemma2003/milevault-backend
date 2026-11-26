FROM python:3.11-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc libpq-dev ffmpeg curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# --timeout-keep-alive 75: keep idle connections open longer than Railway's proxy does. With uvicorn's
# default (5s) the proxy could send the first request after a pause down a connection uvicorn had just
# closed → 502 "Failed to fetch" on the first click, success on the second.

# Verify all app imports resolve at build time so errors appear in build logs, not healthcheck.
RUN python -c "from app.main import app; print('Import check passed')"

EXPOSE 8000

CMD ["sh", "-c", "python -c 'from app.database import create_tables; create_tables(); print(\"DB tables ready\")' && uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000} --timeout-keep-alive 75"]
