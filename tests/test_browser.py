import json
import os
from pathlib import Path

import pytest
from conftest import OWNER, paired, sample
from test_mcp import mcp_request, oauth_login

from codex_vault.mcp_app import widget_html
from codex_vault.models import make_patch
from codex_vault.reconstruct import execute_plan, make_plan
from codex_vault.security import sanitize

pytestmark = pytest.mark.skipif(
    not os.getenv("VAULT_TEST_BROWSER"), reason="defina VAULT_TEST_BROWSER para testar Chromium"
)


@pytest.fixture
def page():
    playwright = pytest.importorskip("playwright.sync_api")
    path = os.environ["VAULT_TEST_BROWSER"]
    with playwright.sync_playwright() as runtime:
        browser = runtime.chromium.launch(executable_path=None if path == "1" else path, headless=True)
        try:
            page = browser.new_page(viewport={"width": 1100, "height": 800})
            page.set_default_timeout(10000)
            yield page
        finally:
            browser.close()


def seed(client):
    _, device = paired(client, "notebook")
    job = client.post("/api/pull/notebook", headers=OWNER).json()["jobs"][0]
    lease = client.post("/api/agent/claim", headers=device).json()["job"]["lease_token"]
    url = f"/api/agent/jobs/{job['job_id']}"
    for snapshot in [sample(), sample(source_id="chat-2", extra="Conferir a navegação")]:
        result = client.post(
            f"{url}/ingest",
            headers=device,
            json={
                "patch": make_patch(sanitize(snapshot), None).model_dump(),
                "lease_token": lease,
            },
        )
        assert result.is_success
    assert client.post(f"{url}/finish", headers=device, json={"lease_token": lease, "result": {}}).is_success
    return client.get("/api/projects", headers=OWNER).json()["projects"][0]["id"]


HOST = """<!doctype html><title>Test host</title>
<iframe src="/widget" title="Vault" style="border:0;width:100%;height:780px"></iframe>
<script>
window.sent = [];
window.addEventListener('message', async (e) => {
  if (e.source !== document.querySelector('iframe').contentWindow || !e.data?.id) return;
  const {id, method, params} = e.data;
  let result = {};
  if (method === 'ui/initialize') result = {hostCapabilities:{experimental:{'openai/message':{}}}};
  else if (method === 'tools/call') {
    result = await fetch('/call', {method:'POST', body:JSON.stringify(params)}).then(r => r.json());
  } else window.sent.push({method, params});
  e.source.postMessage({jsonrpc:'2.0', id, result}, e.origin);
});
</script>"""


def test_mcp_widget_navigation_with_real_tools(page, client):
    seed(client)
    _, tokens = oauth_login(client)

    def serve(route):
        path = route.request.url.split("vault-preview.test", 1)[1]
        if path == "/call":
            response = mcp_request(client, tokens["access_token"], "tools/call", route.request.post_data_json)
            route.fulfill(json=response.json()["result"])
        elif path == "/widget":
            route.fulfill(content_type="text/html", body=widget_html())
        else:
            route.fulfill(content_type="text/html", body=HOST)

    page.route("https://vault-preview.test/**", serve)
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.goto("https://vault-preview.test/")
    frame = page.frame_locator("iframe")
    frame.get_by_role("button", name="orbita", exact=False).click()
    frame.get_by_role("button", name="Exportar", exact=True).click()
    frame.get_by_role("button", name="Baixar JSON", exact=True).click()
    assert page.evaluate(
        "window.sent.some(x => x.method === 'ui/open-link' && x.params.url.includes('/download/'))"
    )
    frame.get_by_role("searchbox").fill("autenticação")
    frame.get_by_role("button", name="Buscar", exact=True).click()
    frame.get_by_role("button", name="Autenticação", exact=False).first.click()
    frame.locator("li.highlight").get_by_text("Qual é a decisão sobre autenticação?", exact=True).wait_for()
    assert frame.locator("li.highlight").count() == 1
    frame.get_by_role("checkbox", name="Incluir mensagem de Codex no contexto").first.check()
    frame.get_by_role("button", name="Continuar no ChatGPT", exact=True).click()
    assert "Pendente" in frame.get_by_role("textbox", name="Contexto para a nova conversa").input_value()
    frame.get_by_role("button", name="Abrir rascunho", exact=True).click()
    frame.get_by_text("Rascunho solicitado ao ChatGPT.", exact=True).wait_for()
    draft = page.evaluate("window.sent.find(x => x.method === 'ui/message').params")
    assert draft["_meta"]["openai/message"] == {"target": "new", "send": False}
    assert "#m2" in draft["content"][0]["text"] and "#m1" not in draft["content"][0]["text"]
    frame.get_by_role("button", name="Dispositivos", exact=True).click()
    frame.get_by_role("button", name="Sincronizar", exact=True).click()
    frame.get_by_role("button", name="Ver andamento", exact=True).click()
    frame.get_by_text("notebook · na fila", exact=True).wait_for()
    frame.get_by_role("button", name="Sincronizar todos", exact=True).click()
    frame.get_by_text("Sincronização solicitada.", exact=True).wait_for()
    assert not errors
    if capture := os.getenv("VAULT_TEST_SCREENSHOT"):
        path = Path(capture)
        path.parent.mkdir(parents=True, exist_ok=True)
        frame.locator("#projects-tab").click()
        frame.get_by_role("button", name="orbita", exact=False).click()
        frame.get_by_role("button", name="Autenticação", exact=False).first.click()
        frame.get_by_text("Qual é a decisão sobre autenticação?", exact=True).wait_for()
        page.screenshot(path=path)
        page.set_viewport_size({"width": 390, "height": 760})
        page.screenshot(path=path.with_name("preview-mobile.png"))


PROJECT_HTML = """<!doctype html><title>Fixture project</title>
<button id="new-chat" onclick="document.querySelector('form').hidden=false">Nova conversa</button>
<form hidden><textarea id="composer"></textarea><input type="file" id="attachment"
onchange="document.getElementById('ready').hidden=false"><span id="ready" hidden>Anexo carregado</span>
<button type="button" id="send">Enviar</button></form>
<script>
document.querySelector('#send').onclick = async () => {
  const transcript = await document.querySelector('#attachment').files[0].text();
  const created = JSON.parse(sessionStorage.getItem('created') || '[]');
  created.push({transcript, prompt: document.querySelector('#composer').value});
  sessionStorage.setItem('created', JSON.stringify(created));
  location.href = '/projects/fixture/chats/' + created.length;
};
</script>"""

SELECTORS = {
    "new_project": "#new-project",
    "project_name": "#name",
    "create_project": "#create",
    "project_url_pattern": r"/projects/fixture(?:\?.*)?$",
    "new_chat": "#new-chat",
    "composer": "#composer",
    "attachment_input": "#attachment",
    "attachment_ready": "#ready",
    "send": "#send",
    "chat_url_pattern": r"/projects/fixture/chats/\d+$",
}


def fake_projects(page):
    def serve(route):
        path = route.request.url.split("fixture.test", 1)[1]
        if path.startswith("/projects/fixture/chats/"):
            body = "<h1>Conversa criada</h1>"
        elif path.startswith("/projects/fixture"):
            body = PROJECT_HTML
        else:
            body = """<button id="new-project" onclick="document.querySelector('form').hidden=false">Projeto</button>
            <form action="/projects/fixture" hidden><input id="name" required><button id="create">Criar</button></form>"""
        route.fulfill(content_type="text/html", body=body)

    page.route("https://fixture.test/**", serve)


def test_native_reconstruction_and_resume_with_browser_fixture(page, client, tmp_path):
    project_id = seed(client)
    archive = client.get(f"/api/projects/{project_id}/export/json", headers=OWNER).json()
    plan = make_plan(archive)
    assert len(plan["chats"]) == 2
    fake_projects(page)
    journal = tmp_path / "journal.json"
    result = execute_plan(page, plan, SELECTORS, journal, "https://fixture.test/")
    assert {item["status"] for item in result["chats"].values()} == {"complete"}
    entries = page.evaluate("JSON.parse(sessionStorage.getItem('created'))")
    assert len(entries) == 2
    assert all(
        "Qual é a decisão" in entry["transcript"] and "pendências" in entry["prompt"] for entry in entries
    )
    assert execute_plan(page, plan, SELECTORS, journal, "https://fixture.test/") == result
    assert page.evaluate("JSON.parse(sessionStorage.getItem('created')).length") == 2
    result["chats"][plan["chats"][0]["id"]]["status"] = "sending"
    journal.write_text(json.dumps(result), encoding="utf-8")
    with pytest.raises(ValueError, match="operação sem confirmação"):
        execute_plan(page, plan, SELECTORS, journal, "https://fixture.test/")
