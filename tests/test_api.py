"""Tests for the FastAPI server endpoints."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import lmdb
import msgpack
import pytest
from fastapi.testclient import TestClient

from docdb_id.api.server import app
from docdb_id.store.schema import ALIAS_DB_NAME, DOCS_DB_NAME, META_DB_NAME, META_KEY_CORE_LAST_UPDATED


def _seed(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    env = lmdb.open(str(path), map_size=64 * 1024 * 1024, subdir=True, max_dbs=3)
    docs_db = env.open_db(DOCS_DB_NAME)
    alias_db = env.open_db(ALIAS_DB_NAME)
    meta_db = env.open_db(META_DB_NAME)
    with env.begin(write=True) as txn:
        txn.put(
            b"US8000000",
            msgpack.packb([["US8000000B2", "KREITER", "20100531", "123"]], use_bin_type=True),
            db=docs_db,
        )
        txn.put(b"US8888881", b"US8888888", db=alias_db)
        txn.put(
            b"US8888888",
            msgpack.packb([["US8888888A1", "INVENTOR", "20200101", "456"]], use_bin_type=True),
            db=docs_db,
        )
        txn.put(META_KEY_CORE_LAST_UPDATED, b"2026-06-01T00:00:00+00:00", db=meta_db)
    env.close()


@pytest.fixture()
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    lmdb_path = tmp_path / "test.lmdb"
    _seed(lmdb_path)
    monkeypatch.setenv("DOCDB_LMDB_PATH", str(lmdb_path))
    with TestClient(app) as c:
        yield c


def test_health(client: TestClient):
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_health_fails_when_canary_record_missing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    lmdb_path = tmp_path / "test.lmdb"
    lmdb_path.mkdir(parents=True, exist_ok=True)
    env = lmdb.open(str(lmdb_path), map_size=64 * 1024 * 1024, subdir=True, max_dbs=3)
    env.open_db(DOCS_DB_NAME)
    env.open_db(ALIAS_DB_NAME)
    env.open_db(META_DB_NAME)
    env.close()
    monkeypatch.setenv("DOCDB_LMDB_PATH", str(lmdb_path))

    with TestClient(app) as client:
        resp = client.get("/health")
        assert resp.status_code == 503
        assert "US8000000B2" in resp.json()["detail"]


def test_stats(client: TestClient):
    resp = client.get("/stats")
    assert resp.status_code == 200
    data = resp.json()
    assert data["key_count"] == 2
    assert data["last_updated"] == "2026-06-01T00:00:00+00:00"


def test_query_direct_hit(client: TestClient):
    resp = client.get("/query", params={"cc": "US", "number": "8000000"})
    assert resp.status_code == 200
    records = resp.json()
    assert len(records) == 1
    assert records[0]["docdb_id"] == "US8000000B2"
    assert records[0]["inventor"] == "KREITER"
    assert records[0]["date_publ"] == "20100531"
    assert records[0]["family_id"] == "123"


def test_query_no_hit(client: TestClient):
    resp = client.get("/query", params={"cc": "US", "number": "9999999"})
    assert resp.status_code == 200
    assert resp.json() == []


def test_query_alias_hit(client: TestClient):
    resp = client.get("/query", params={"cc": "US", "number": "8888881"})
    assert resp.status_code == 200
    assert resp.json()[0]["docdb_id"] == "US8888888A1"


def test_query_invalid_cc(client: TestClient):
    resp = client.get("/query", params={"cc": "XX", "number": "8000000"})
    assert resp.status_code == 422
    assert resp.json()["detail"] == "cc_does_not_exist"


def test_query_invalid_number(client: TestClient):
    resp = client.get("/query", params={"cc": "US", "number": "123-456"})
    assert resp.status_code == 422
    assert resp.json()["detail"] == "number_is_not_alnum"


def test_query_cc_wrong_length(client: TestClient):
    resp = client.get("/query", params={"cc": "USA", "number": "8000000"})
    assert resp.status_code == 422


def test_batch_mixed(client: TestClient):
    resp = client.post("/batch", json={"items": [
        {"cc": "US", "number": "8000000"},
        {"cc": "US", "number": "9999999"},
        {"cc": "XX", "number": "1234567"},
        {"cc": "US", "number": "123-456"},
    ]})
    assert resp.status_code == 200
    results = resp.json()
    assert results[0]["results"][0]["docdb_id"] == "US8000000B2"
    assert results[0]["error"] is None
    assert results[1]["results"] == []
    assert results[1]["error"] is None
    assert results[2]["error"] == "cc_does_not_exist"
    assert results[3]["error"] == "number_is_not_alnum"


def test_batch_too_many_items(client: TestClient):
    items = [{"cc": "US", "number": "1234567"}] * 10_001
    resp = client.post("/batch", json={"items": items})
    assert resp.status_code == 422


def test_query_survives_external_growth_past_default_map_size(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Regression test for MDB_MAP_RESIZED: the api's LMDB env is opened once
    at process startup and held open. If it doesn't reserve a map_size big
    enough upfront, a separate writer process growing the DB afterwards
    (e.g. `make apply-frontfile` on the host) breaks every subsequent read
    until the api process restarts."""
    lmdb_path = tmp_path / "test.lmdb"
    _seed(lmdb_path)
    monkeypatch.setenv("DOCDB_LMDB_PATH", str(lmdb_path))

    with TestClient(app) as client:
        resp = client.get("/query", params={"cc": "US", "number": "8000000"})
        assert resp.status_code == 200

        # Simulate a separate writer process (e.g. `make apply-frontfile` on
        # the host) growing the DB well past LMDB's small default reader
        # reservation, while this client's env stays open. Must be a real
        # subprocess: python-lmdb refuses to open the same path twice
        # in-process, which would mask the actual multi-process bug.
        writer_script = (
            "import lmdb\n"
            f"env = lmdb.open({str(lmdb_path)!r}, map_size=200 * 1024 * 1024, subdir=True, max_dbs=3)\n"
            "db = env.open_db(b'docs')\n"
            "record = b'x' * 200\n"
            "with env.begin(write=True) as txn:\n"
            "    for i in range(200_000):\n"
            "        txn.put(f'GROWTH{i}'.encode(), record, db=db)\n"
            "env.close()\n"
        )
        subprocess.run([sys.executable, "-c", writer_script], check=True)

        resp = client.get("/query", params={"cc": "US", "number": "8000000"})
        assert resp.status_code == 200
        assert resp.json()[0]["docdb_id"] == "US8000000B2"
