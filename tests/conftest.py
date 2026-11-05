import os
import uuid
from urllib.parse import parse_qs, urlencode, urlsplit, urlunsplit

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.schema import CreateSchema, DropSchema

from codex_vault.config import Settings
from codex_vault.models import Header, Item, Snapshot
from codex_vault.server import create_app

ADMIN = "local-test-owner-key-" + "a" * 40
OWNER = {"Authorization": f"Bearer {ADMIN}"}
ORIGIN = "http://127.0.0.1:8765"


@pytest.fixture
def database_url(tmp_path):
    postgres = os.getenv("VAULT_TEST_POSTGRES_URL")
    if not postgres:
        yield f"sqlite:///{(tmp_path / 'vault.db').as_posix()}"
        return
    schema = f"vault_test_{uuid.uuid4().hex}"
    engine = create_engine(postgres)
    with engine.begin() as connection:
        connection.execute(CreateSchema(schema))
    url = urlsplit(postgres)
    query = parse_qs(url.query)
    query["options"] = [f"-csearch_path={schema}"]
    try:
        yield urlunsplit(url._replace(query=urlencode(query, doseq=True)))
    finally:
        with engine.begin() as connection:
            connection.execute(DropSchema(schema, cascade=True))
        engine.dispose()


@pytest.fixture
def app(database_url):
    return create_app(Settings(database_url=database_url, admin_token=ADMIN, public_url=ORIGIN))


@pytest.fixture
def client(app):
    with TestClient(app, base_url=ORIGIN) as connection:
        yield connection


def paired(client, name):
    code = client.post("/api/pairings", headers=OWNER, json={"name": name}).json()["code"]
    response = client.post("/api/agent/pair", json={"code": code})
    assert response.status_code == 201
    device = response.json()
    return device, {"Authorization": f"Bearer {device['token']}"}


def sample(cwd="C:/repos/orbita", source_id="chat-1", extra=None, archived=False):
    records = [
        Item(
            id="m1",
            role="user",
            text="Qual é a decisão sobre autenticação?",
            timestamp="2026-09-25T14:30:00+00:00",
        ),
        Item(id="m2", role="assistant", text="Decidimos adotar OAuth. Pendente: validar a renovação."),
        Item(
            id="m3",
            role="tool",
            kind="commandExecution",
            text="Bearer should-be-redacted-12345",
            payload={"access_token": "private-value", "command": "git status"},
        ),
    ]
    if extra:
        records.append(Item(id="m4", role="assistant", text=extra))
    return Snapshot(
        header=Header(
            source_id=source_id,
            title="Autenticação",
            cwd=cwd,
            workspace="personal",
            repository="git@github.com:example/orbita.git",
            archived=archived,
            forked_from_id="older-thread",
        ),
        items=records,
    )
