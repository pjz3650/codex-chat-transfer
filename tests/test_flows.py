import json
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path

import httpx
import pytest
from conftest import ORIGIN, OWNER, paired, sample

from codex_vault.agent import Agent, Spool
from codex_vault.collector import JsonSource, Selection
from codex_vault.models import iso, make_patch, now
from codex_vault.security import sanitize
from codex_vault.store import Job


def source_file(path: Path, *snapshots):
    path.write_text(json.dumps([s.model_dump() for s in snapshots]), encoding="utf-8")


async def run_agent(app, headers, spool, path, selection=None, transport=None):
    selection = selection or Selection()
    async with httpx.AsyncClient(
        base_url=ORIGIN, headers=headers, transport=transport or httpx.ASGITransport(app=app)
    ) as connection:
        return await Agent(connection, Spool(spool), JsonSource(path, selection), selection).once()


async def test_two_device_flows(client, app, tmp_path):
    device_a, headers_a = paired(client, "notebook")
    device_b, headers_b = paired(client, "desktop")
    assert client.get("/api/devices").status_code == 401
    assert client.get("/api/projects", headers=headers_a).status_code == 401
    assert client.post("/api/agent/claim", headers=OWNER).status_code == 401
    assert len(client.get("/api/devices", headers=OWNER).json()["devices"]) == 2
    with app.state.db.session() as s:
        from codex_vault.store import Device

        s.get(Device, device_b["device_id"]).last_seen = iso(now() - timedelta(minutes=30))

    paths = [tmp_path / name for name in ("a.json", "b.json", "a.db", "b.db")]
    file_a, file_b, spool_a, spool_b = paths
    source_file(file_a, sample(), sample("C:/secret", "secret-chat"))
    source_file(file_b, sample("/work/orbita"))
    exclude = Selection(exclude=["*/secret"])
    all_jobs = client.post("/api/pull-all", headers=OWNER).json()["jobs"]
    assert {job["device"]: job["availability"] for job in all_jobs} == {
        "notebook": "online",
        "desktop": "pending_offline",
    }
    one = client.post("/api/pull/notebook", headers=OWNER).json()["jobs"][0]
    assert one["job_id"] == next(j["job_id"] for j in all_jobs if j["device"] == "notebook")
    first = await run_agent(app, headers_a, spool_a, file_a, exclude)
    assert first == {"error": None, "threads_seen": 1, "threads_sent": 1, "excluded": 1}
    assert client.get(f"/api/jobs/{one['job_id']}", headers=OWNER).json()["state"] == "complete"
    assert (await run_agent(app, headers_b, spool_b, file_b))["threads_sent"] == 1

    listing = client.get("/api/projects", headers=OWNER).json()
    assert len(listing["projects"]) == 2
    assert all(p["repository"] == "github.com/example/orbita" for p in listing["projects"])
    assert all(d["last_sync"] for d in listing["coverage"])
    chat_a = next(
        c for c in client.get("/api/chats", headers=OWNER).json()["data"] if c["device"] == "notebook"
    )
    chat_b = next(
        c for c in client.get("/api/chats", headers=OWNER).json()["data"] if c["device"] == "desktop"
    )
    ids = {"source_ids": [chat_b["project_id"]], "target_id": chat_a["project_id"]}
    assert (
        client.post("/api/projects/map", headers=OWNER, json={**ids, "confirmed": False}).status_code == 422
    )
    assert client.post("/api/projects/map", headers=OWNER, json={**ids, "confirmed": True}).is_success
    project = client.get("/api/projects", headers=OWNER).json()["projects"][0]
    assert (project["unique_chats"], project["copies"]) == (1, 2)

    search = client.get("/api/search", headers=OWNER, params={"q": "decisão autenticação", "limit": 1}).json()
    assert search["next_offset"] == 1
    assert search["data"][0]["date"] == "2026-09-25T14:30:00+00:00"
    assert search["data"][0]["reference"].endswith("#m1")
    assert client.get("/api/search", headers=OWNER, params={"q": "private-value"}).json()["data"] == []
    chat_id = chat_a["id"]
    page = client.get(f"/api/chats/{chat_id}", headers=OWNER, params={"limit": 1}).json()
    assert page["next_offset"] == 1
    assert page["header"]["forked_from_id"] == "older-thread"
    context = client.post(
        f"/api/chats/{chat_id}/continue", headers=OWNER, json={"message_ids": ["m2"]}
    ).json()
    assert context["messages"] == 1 and "Pendente" in context["prompt"] and "#m2" in context["prompt"]
    assert (
        client.post(f"/api/chats/{chat_id}/continue", headers=OWNER, json={"message_ids": ["m3"]}).status_code
        == 422
    )

    path = f"/api/projects/{project['id']}/export"
    json_export = client.get(f"{path}/json", headers=OWNER)
    md_export = client.get(f"{path}/markdown", headers=OWNER)
    assert json_export.json()["format"] == "codex-vault/1"
    assert len(json_export.json()["origins"]) == 2
    assert "[REDACTED]" in json_export.text and "private-value" not in json_export.text
    assert "Autenticação" in md_export.text and "vault://chat/" in md_export.text

    client.post("/api/pull/notebook", headers=OWNER)
    assert (await run_agent(app, headers_a, spool_a, file_a, exclude))["threads_sent"] == 0

    source_file(
        file_b,
        sample("/work/orbita", extra="Mudança divergente."),
        sample("/work/orbita", "chat-2", archived=True),
    )
    client.post("/api/pull/desktop", headers=OWNER)
    assert (await run_agent(app, headers_b, spool_b, file_b))["threads_sent"] == 2
    projects = client.get("/api/projects", headers=OWNER).json()["projects"]
    assert len(projects) == 1 and projects[0]["unique_chats"] == 3
    assert client.post(f"/api/devices/{device_a['device_id']}/revoke", headers=OWNER).is_success
    assert client.post("/api/agent/claim", headers=headers_a).status_code == 401


async def test_patch_retry_after_lost_ack(client, app, tmp_path):
    _, headers = paired(client, "notebook")
    path, spool = tmp_path / "source.json", tmp_path / "spool.db"
    source_file(path, sample())
    job = client.post("/api/pull/notebook", headers=OWNER).json()["jobs"][0]
    asgi = httpx.ASGITransport(app=app)

    async def lost_ack(request):
        response = await asgi.handle_async_request(request)
        if request.url.path.endswith("/ingest"):
            assert response.is_success
            raise httpx.ReadError("simulated network interruption", request=request)
        return response

    with pytest.raises(httpx.ReadError):
        await run_agent(app, headers, spool, path, transport=httpx.MockTransport(lost_ack))
    assert len(list(Spool(spool).pending())) == 1
    with app.state.db.session() as s:
        s.get(Job, job["job_id"]).lease_until = iso(now() - timedelta(seconds=1))
    result = await run_agent(app, headers, spool, path)
    assert result["threads_sent"] == 0
    assert not list(Spool(spool).pending())
    assert len(client.get("/api/chats", headers=OWNER).json()["data"]) == 1

    source_file(path, sample(extra="Nova mensagem"))
    client.post("/api/pull/notebook", headers=OWNER)
    uploads = []

    async def capture(request):
        if request.url.path.endswith("/ingest"):
            uploads.append(json.loads(request.content)["patch"])
        return await asgi.handle_async_request(request)

    await run_agent(app, headers, spool, path, transport=httpx.MockTransport(capture))
    assert len(uploads) == 1
    assert [i["id"] for i in uploads[0]["upserts"]] == ["m4"]

    # Losing local state can rebuild only this device's copy.
    source_file(path, sample(extra="Corrigida após reiniciar"))
    client.post("/api/pull/notebook", headers=OWNER)
    assert (await run_agent(app, headers, tmp_path / "fresh.db", path))["threads_sent"] == 1
    chat_id = client.get("/api/chats", headers=OWNER).json()["data"][0]["id"]
    assert (
        client.get(f"/api/chats/{chat_id}", headers=OWNER).json()["items"][-1]["text"]
        == "Corrigida após reiniciar"
    )


def test_job_lease_and_bad_payload(client, app):
    _, first = paired(client, "primeiro")
    _, second = paired(client, "segundo")
    first_id = client.post("/api/pull/primeiro", headers=OWNER).json()["jobs"][0]["job_id"]
    lease = client.post("/api/agent/claim", headers=first).json()["job"]["lease_token"]
    url = f"/api/agent/jobs/{first_id}"
    patch = make_patch(sanitize(sample()), None).model_dump()
    body = {"patch": patch, "lease_token": lease}
    assert client.post(f"{url}/ingest", headers=second, json=body).status_code == 404
    assert client.post(f"{url}/renew", headers=first, json={"lease_token": lease}).is_success
    invalid = {**patch, "checksum": "0" * 64}
    assert client.post(f"{url}/ingest", headers=first, json={**body, "patch": invalid}).status_code == 422
    assert client.get("/api/chats", headers=OWNER).json()["data"] == []
    with app.state.db.session() as s:
        s.get(Job, first_id).lease_until = iso(now() - timedelta(seconds=1))
    new_lease = client.post("/api/agent/claim", headers=first).json()["job"]["lease_token"]
    assert client.post(f"{url}/ingest", headers=first, json=body).status_code == 409
    assert client.post(f"{url}/ingest", headers=first, json={**body, "lease_token": new_lease}).is_success


def test_pairing_one_use(client):
    response = client.post("/api/pairings", headers=OWNER, json={"name": "pareado"})
    assert response.status_code == 201
    body = {"code": response.json()["code"]}
    assert client.post("/api/agent/pair", json=body).status_code == 201
    assert client.post("/api/agent/pair", json=body).status_code == 401
    assert client.post("/api/pairings", headers=OWNER, json={"name": "pareado"}).status_code == 409


def test_concurrent_sync_requests_share_one_job(client):
    paired(client, "notebook")

    def request_job(_):
        response = client.post("/api/pull/notebook", headers=OWNER)
        assert response.is_success
        return response.json()["jobs"][0]["job_id"]

    with ThreadPoolExecutor(max_workers=4) as pool:
        assert len(set(pool.map(request_job, range(8)))) == 1
