import asyncio
import os
import sys
from pathlib import Path

import pytest

from codex_vault.collector import CodexClient, CodexSource, Selection
from codex_vault.security import canonical_repo, redact


async def test_codex_protocol_with_subprocess(monkeypatch):
    original = asyncio.create_subprocess_exec
    fixture = Path(__file__).with_name("fake_codex.py")

    async def spawn(_executable, *_args, **kwargs):
        return await original(sys.executable, str(fixture), **kwargs)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    source = CodexSource(sys.executable, "workspace-test", Selection(exclude=["/private"]))
    rows = [snapshot async for snapshot in source.snapshots()]
    assert [r.header.source_id for r in rows] == ["a", "legacy"]
    assert rows[0].header.repository == "github.com/demo/repo"
    assert rows[0].items[0].timestamp == "2024-07-03T09:46:40+00:00"
    assert [i.text for i in rows[0].items] == ["Olá", "Resposta"]
    assert rows[1].header.archived and rows[1].items[0].text == "Legado"
    assert source.excluded == 1


def test_secret_redaction_and_repo_identity():
    remote = "https://someone:secret@GITHUB.COM/org/repo.git?hidden=credential"
    assert canonical_repo(remote) == canonical_repo("git@github.com:org/repo.git") == "github.com/org/repo"
    assert canonical_repo("github.com/org/repo") == "github.com/org/repo"
    text = "PASSWORD=private\nBearer private123\nhttps://user:secret@host.com/path"
    clean = redact({"text": text, "refresh_token": "hidden", "nested": [{"cookie": "another-secret"}]})
    assert clean["refresh_token"] == "[REDACTED]"
    assert "private" not in clean["text"] and "secret" not in clean["text"]
    assert clean == redact(clean)


@pytest.mark.skipif(not os.getenv("VAULT_TEST_REAL_CODEX"), reason="smoke opcional do executável instalado")
async def test_real_codex_handshake(tmp_path):
    async with CodexClient() as client:
        response = await client.call(
            "thread/list", {"cwd": str(tmp_path), "limit": 1, "useStateDbOnly": True}
        )
        assert response["data"] == []
