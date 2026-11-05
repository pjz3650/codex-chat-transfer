import base64
import hashlib
from urllib.parse import parse_qs, urlsplit

from conftest import ADMIN, ORIGIN, paired, sample

from codex_vault.mcp_app import WIDGET_URI
from codex_vault.models import make_patch
from codex_vault.security import sanitize

ACCEPT = "application/json, text/event-stream"


def mcp_request(client, token, method, params=None):
    return client.post(
        "/mcp",
        headers={"Authorization": f"Bearer {token}", "Accept": ACCEPT},
        json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}},
    )


def oauth_login(client):
    callback = "http://127.0.0.1:3000/callback"
    registration = client.post(
        "/register",
        json={"redirect_uris": [callback], "token_endpoint_auth_method": "none", "client_name": "Tests"},
    )
    assert registration.status_code == 201, registration.text
    client_id = registration.json()["client_id"]
    verifier = "test-code-verifier-" + "a" * 32
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    authorization = client.get(
        "/authorize",
        params={
            "client_id": client_id,
            "response_type": "code",
            "redirect_uri": callback,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "scope": "vault",
            "state": "test-state",
            "resource": f"{ORIGIN}/mcp",
        },
        follow_redirects=False,
    )
    assert authorization.is_redirect, authorization.text
    consent = urlsplit(authorization.headers["location"]).path
    assert "Chave do acervo" in client.get(consent).text
    assert client.post(consent, data={"admin_token": "incorrect"}).status_code == 401
    approved = client.post(consent, data={"admin_token": ADMIN}, follow_redirects=False)
    assert approved.status_code == 303
    assert client.post(consent, data={"admin_token": ADMIN}, follow_redirects=False).status_code == 404
    callback_query = parse_qs(urlsplit(approved.headers["location"]).query)
    assert callback_query["state"] == ["test-state"]
    payload = {
        "grant_type": "authorization_code",
        "client_id": client_id,
        "code": callback_query["code"][0],
        "redirect_uri": callback,
        "code_verifier": verifier,
    }
    assert client.post("/token", data={**payload, "code_verifier": "X" * 43}).status_code == 400
    response = client.post("/token", data=payload)
    assert response.is_success, response.text
    assert client.post("/token", data=payload).status_code == 400
    return client_id, response.json()


def test_mcp_tools_and_oauth(client):
    unauthorized = mcp_request(client, "not-authorized", "tools/list")
    assert unauthorized.status_code == 401
    assert "oauth-protected-resource/mcp" in unauthorized.headers["www-authenticate"]
    assert client.get("/.well-known/oauth-protected-resource/mcp").json()["resource"] == f"{ORIGIN}/mcp"
    assert (
        client.post("/register", json={"redirect_uris": ["https://attacker.example/cb"]}).status_code == 400
    )
    client_id, tokens = oauth_login(client)
    secret = tokens["access_token"]
    assert (
        mcp_request(
            client,
            secret,
            "initialize",
            {
                "protocolVersion": "2025-11-25",
                "capabilities": {},
                "clientInfo": {"name": "tests", "version": "1"},
            },
        ).json()["result"]["serverInfo"]["name"]
        == "Codex Vault"
    )
    listing = mcp_request(client, secret, "tools/list").json()["result"]["tools"]
    names = {tool["name"]: tool for tool in listing}
    assert names["pull"]["annotations"]["readOnlyHint"] is False
    assert names["open_library"]["_meta"]["ui"]["resourceUri"] == WIDGET_URI
    resource = mcp_request(client, secret, "resources/read", {"uri": WIDGET_URI}).json()["result"][
        "contents"
    ][0]
    assert resource["mimeType"] == "text/html;profile=mcp-app"
    assert "Continuar no ChatGPT" in resource["text"]

    def call(name, **arguments):
        response = mcp_request(client, secret, "tools/call", {"name": name, "arguments": arguments})
        result = response.json()["result"]
        assert not result.get("isError"), result
        return result["structuredContent"]

    assert call("open_library")["message"]
    assert not call("devices")["devices"]
    assert not call("list")["projects"]
    _, agent_headers = paired(client, "notebook")
    job = call("pull", device="notebook")["jobs"][0]
    assert call("pull_all")["jobs"][0]["job_id"] == job["job_id"]
    lease = client.post("/api/agent/claim", headers=agent_headers).json()["job"]["lease_token"]
    url = f"/api/agent/jobs/{job['job_id']}"
    receipt = client.post(
        f"{url}/ingest",
        headers=agent_headers,
        json={
            "lease_token": lease,
            "patch": make_patch(sanitize(sample()), None).model_dump(),
        },
    )
    assert receipt.is_success, receipt.text
    chat_id = receipt.json()["chat_id"]
    client.post(
        f"{url}/finish", headers=agent_headers, json={"lease_token": lease, "result": {"threads_sent": 1}}
    )
    assert call("job_status", job_id=job["job_id"])["state"] == "complete"
    assert call("devices")["devices"][0]["last_sync"]
    project_id = call("list")["projects"][0]["id"]
    assert call("chats", project_id=project_id)["data"][0]["id"] == chat_id
    assert call("search", query="autenticação")["data"][0]["chat_id"] == chat_id
    message = call("read", chat_id=chat_id, limit=1)["items"][0]
    assert message["id"] == "m1" and "payload" not in message
    assert call("continue_context", chat_id=chat_id, message_ids=["m2"])["messages"] == 1
    exports = call("export", project_id=project_id)["downloads"]
    assert client.get(urlsplit(exports["json"]).path).json()["format"] == "codex-vault/1"
    assert "Transcrições" in client.get(urlsplit(exports["markdown"]).path).text
    assert mcp_request(
        client, secret, "tools/call", {"name": "read", "arguments": {"chat_id": chat_id, "limit": -1}}
    ).json()["result"]["isError"]

    refresh = {
        "grant_type": "refresh_token",
        "client_id": client_id,
        "refresh_token": tokens["refresh_token"],
    }
    rotated = client.post("/token", data=refresh)
    assert rotated.is_success, rotated.text
    assert client.post("/token", data=refresh).status_code == 400
    fresh = rotated.json()["access_token"]
    assert mcp_request(client, fresh, "tools/list").is_success
    revoked = client.post("/revoke", data={"client_id": client_id, "token": fresh})
    assert revoked.is_success, revoked.text
    assert mcp_request(client, fresh, "tools/list").status_code == 401
