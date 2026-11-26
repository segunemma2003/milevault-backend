"""Idempotency-Key: one action → one effect, even with double clicks and retries."""
import os
import threading
import time

os.environ.setdefault("DATABASE_URL", "sqlite://")

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import app.models  # noqa
import app.models.agent, app.models.currency, app.models.crypto, app.models.content, app.models.payments, app.models.support  # noqa
from app.database import Base
from app.middleware.idempotency import IdempotencyMiddleware


@pytest.fixture()
def client(monkeypatch, tmp_path):
    # A file database: each request gets its own connection, like PostgreSQL in production.
    engine = create_engine(f"sqlite:///{tmp_path}/idem.db", connect_args={"check_same_thread": False, "timeout": 10})
    Base.metadata.create_all(engine)
    import app.database
    monkeypatch.setattr(app.database, "SessionLocal", sessionmaker(bind=engine))
    api = FastAPI()
    api.state.calls = []

    @api.post("/api/v1/payments/withdraw")
    def withdraw(body: dict):
        api.state.calls.append(body)
        if body.get("slow"):
            time.sleep(0.5)
        if body.get("fail"):
            from fastapi import HTTPException
            raise HTTPException(status_code=422, detail="Insufficient balance")
        return {"id": f"w{len(api.state.calls)}", "amount": body["amount"]}

    api.add_middleware(IdempotencyMiddleware)
    return TestClient(api), api


def test_repeat_returns_the_first_result_without_running_again(client):
    c, api = client
    h = {"Idempotency-Key": "key-0000000001"}
    a = c.post("/api/v1/payments/withdraw", json={"amount": 100}, headers=h)
    b = c.post("/api/v1/payments/withdraw", json={"amount": 100}, headers=h)
    assert a.json() == b.json() == {"id": "w1", "amount": 100}
    assert b.headers.get("X-Idempotency-Replayed") == "true" and len(api.state.calls) == 1
    other = c.post("/api/v1/payments/withdraw", json={"amount": 100}, headers={"Idempotency-Key": "key-0000000002"})
    assert other.json()["id"] == "w2"                                   # a new action runs


def test_simultaneous_double_click_runs_once(client):
    c, api = client
    h = {"Idempotency-Key": "key-dbl-click-1"}
    results, errors = [], []

    def click():
        try:
            with TestClient(api) as own:  # each "click" on its own connection
                results.append(own.post("/api/v1/payments/withdraw", json={"amount": 50, "slow": True}, headers=h).status_code)
        except Exception as e:  # surface thread errors
            errors.append(repr(e))

    threads = [threading.Thread(target=click) for _ in range(2)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert not errors, errors
    assert sorted(results) == [200, 409] and len(api.state.calls) == 1


def test_same_key_with_different_details_is_refused(client):
    c, _ = client
    h = {"Idempotency-Key": "key-0000000003"}
    c.post("/api/v1/payments/withdraw", json={"amount": 100}, headers=h)
    r = c.post("/api/v1/payments/withdraw", json={"amount": 999}, headers=h)
    assert r.status_code == 422 and r.json()["detail"]["error"] == "IDEMPOTENCY_KEY_REUSED"


def test_failure_can_be_retried_with_the_same_key(client):
    c, api = client
    h = {"Idempotency-Key": "key-0000000004"}
    assert c.post("/api/v1/payments/withdraw", json={"amount": 100, "fail": True}, headers=h).status_code == 422
    assert c.post("/api/v1/payments/withdraw", json={"amount": 100, "fail": True}, headers=h).status_code == 422
    assert len(api.state.calls) == 2                                     # errors aren't cached


def test_requests_without_a_key_are_untouched(client):
    c, api = client
    c.post("/api/v1/payments/withdraw", json={"amount": 1})
    c.post("/api/v1/payments/withdraw", json={"amount": 1})
    assert len(api.state.calls) == 2
