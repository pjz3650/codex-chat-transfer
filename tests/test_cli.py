import json
import socket
import threading
import time
from pathlib import Path

import uvicorn
from conftest import ADMIN
from typer.testing import CliRunner

from codex_vault.cli import app as cli


def test_cli_from_pairing_to_export(app, tmp_path, monkeypatch):
    demo = Path(__file__).resolve().parents[1] / "examples" / "sample.json"
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()

    def invoke(*args, input=None):
        result = runner.invoke(cli, args, input=input)
        assert result.exit_code == 0, (result.output, result.exception)
        return result.output

    assert "Configuração criada" in invoke("init")
    assert (tmp_path / ".env").is_file()
    assert runner.invoke(cli, ["init"]).exit_code != 0

    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
        url = f"http://127.0.0.1:{port}"
        monkeypatch.setenv("VAULT_PUBLIC_URL", url)
        monkeypatch.setenv("VAULT_ADMIN_TOKEN", ADMIN)
        server = uvicorn.Server(uvicorn.Config(app, log_level="warning", access_log=False))
        thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
        thread.start()
        try:
            deadline = time.monotonic() + 15
            while not server.started and thread.is_alive() and time.monotonic() < deadline:
                time.sleep(0.05)
            assert server.started
            code = json.loads(invoke("pairing", "notebook"))["code"]
            invoke("pair", "--server", url, input=f"{code}\n")
            result = json.loads(invoke("agent", "--source", str(demo), "--once", "--sync-every", "30"))
            assert result["threads_sent"] == 1
            job = json.loads(invoke("pull", "--device", "notebook"))["jobs"][0]
            assert json.loads(invoke("pull-all"))["jobs"][0]["job_id"] == job["job_id"]
            assert json.loads(invoke("agent", "--source", str(demo), "--once"))["threads_sent"] == 0
            assert json.loads(invoke("job-status", job["job_id"]))["state"] == "complete"
            device = json.loads(invoke("devices"))["devices"][0]
            assert device["online"] and device["last_sync"]
            project = json.loads(invoke("list"))["projects"][0]
            chat = json.loads(invoke("chats", "--project", project["id"]))["data"][0]
            assert json.loads(invoke("search", "autenticação"))["data"][0]["chat_id"] == chat["id"]
            assert json.loads(invoke("read", chat["id"], "--limit", "1"))["next_offset"] == 1
            assert "Pendência" in invoke("continue-context", chat["id"])
            paths = [Path(path) for path in invoke("export", project["id"]).splitlines()]
            assert len(paths) == 2 and all(path.is_file() for path in paths)
            assert (
                json.loads(invoke("reconstruct", str(paths[0])))["projects"][0]["chats"][0]["id"]
                == chat["id"]
            )
            assert json.loads(invoke("revoke-device", device["id"]))["revoked"]
        finally:
            server.should_exit = True
            thread.join(timeout=10)
            assert not thread.is_alive()
