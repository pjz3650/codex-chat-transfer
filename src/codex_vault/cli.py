from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path
from typing import Annotated
from urllib.parse import quote, urlsplit

import httpx
import typer
import uvicorn

from .agent import Agent, Spool
from .collector import CodexSource, CollectorError, JsonSource, Selection
from .config import Settings
from .models import digest
from .reconstruct import execute_plan, make_plan
from .security import token

app = typer.Typer(no_args_is_help=True, help="Acervo de conversas do Codex")
DeviceFile = Annotated[Path, typer.Option("--device-file")]
DEFAULT_DEVICE = Path(".local/device.json")


def show(data):
    typer.echo(json.dumps(data, ensure_ascii=False, indent=2))


def request(method: str, path: str, **kwargs) -> httpx.Response:
    settings = Settings()
    with httpx.Client(
        base_url=settings.public_url,
        timeout=60,
        follow_redirects=False,
        headers={"Authorization": f"Bearer {settings.admin_token.get_secret_value()}"},
    ) as client:
        try:
            response = client.request(method, path, **kwargs)
            response.raise_for_status()
            return response
        except httpx.HTTPStatusError as exc:
            typer.echo(
                f"Servidor rejeitou a operação (HTTP {exc.response.status_code}): {exc.response.text[:600]}",
                err=True,
            )
            raise typer.Exit(1) from exc
        except httpx.RequestError as exc:
            typer.echo(
                "Servidor indisponível. Confira VAULT_PUBLIC_URL e se o servidor está rodando.", err=True
            )
            raise typer.Exit(1) from exc


@app.command()
def init():
    """Crie a configuração de desenvolvimento e uma credencial privada."""
    env = Path(".env")
    try:
        fd = os.open(env, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as out:
            out.write(
                f"VAULT_ADMIN_TOKEN={token()}\nVAULT_PUBLIC_URL=http://127.0.0.1:8765\n"
                "VAULT_DATABASE_URL=sqlite:///.local/vault.db\n"
            )
    except FileExistsError as exc:
        raise typer.BadParameter("Já existe um arquivo .env") from exc
    typer.echo("Configuração criada em .env")


@app.command()
def serve(host: str = "127.0.0.1", port: int = 8765):
    """Inicie a API e o servidor MCP."""
    from .server import create_app

    uvicorn.run(create_app(), host=host, port=port, access_log=False)


@app.command("pairing")
def pairing(name: str):
    """Emita um código descartável para cadastrar uma máquina."""
    show(request("POST", "/api/pairings", json={"name": name}).json())


def local_or_tls(server: str) -> str:
    parsed = urlsplit(server)
    loopback = parsed.scheme == "http" and parsed.hostname in {"localhost", "127.0.0.1"}
    if (
        not parsed.hostname
        or (parsed.scheme != "https" and not loopback)
        or parsed.username
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise typer.BadParameter("Use a origem HTTPS do servidor ou HTTP em localhost")
    return server.rstrip("/")


@app.command()
def pair(server: str = "http://127.0.0.1:8765", device_file: DeviceFile = DEFAULT_DEVICE):
    """Resgate um código de pareamento na máquina de origem."""
    if device_file.exists():
        raise typer.BadParameter("Esse arquivo já tem um dispositivo. Selecione outro com --device-file.")
    server = local_or_tls(server)
    code = typer.prompt("Código de pareamento", hide_input=True)
    try:
        response = httpx.post(f"{server}/api/agent/pair", json={"code": code}, timeout=20)
        response.raise_for_status()
    except httpx.HTTPError as exc:
        typer.echo("Pareamento rejeitado ou servidor indisponível", err=True)
        raise typer.Exit(1) from exc
    device_file.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(device_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as out:
        json.dump({"server": server, **response.json()}, out)
    typer.echo(f"Dispositivo cadastrado em {device_file}")


@app.command("agent")
def run_agent(
    device_file: DeviceFile = DEFAULT_DEVICE,
    codex: str = "codex",
    workspace: str = "default",
    source: Path | None = None,
    include: Annotated[list[str] | None, typer.Option()] = None,
    exclude: Annotated[list[str] | None, typer.Option()] = None,
    once: bool = False,
    poll: Annotated[float, typer.Option(min=1, max=30)] = 10,
    sync_every: Annotated[float | None, typer.Option(min=30)] = None,
):
    """Atenda pedidos de sincronização com o histórico local ou um arquivo de exemplo."""
    try:
        device = json.loads(device_file.read_text(encoding="utf-8"))
        server, secret = local_or_tls(device["server"]), device["token"]
    except (OSError, KeyError, ValueError) as exc:
        raise typer.BadParameter("Arquivo de dispositivo inválido. Execute cvault pair.") from exc
    selection = Selection(include, exclude)
    collector = JsonSource(source, selection) if source else CodexSource(codex, workspace, selection)
    spool = Spool(device_file.with_suffix(".db"))

    async def run():
        async with httpx.AsyncClient(
            base_url=server, timeout=90, headers={"Authorization": f"Bearer {secret}"}
        ) as client:
            agent = Agent(client, spool, collector, selection)
            if once:
                if sync_every:
                    await agent.post("/api/agent/request-sync")
                result = await agent.once()
                show(result or {"state": "idle"})
                if result and result.get("error"):
                    raise typer.Exit(1)
            else:
                await agent.run(poll, sync_every)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    try:
        asyncio.run(run())
    except (CollectorError, httpx.HTTPError) as exc:
        typer.echo(f"Falha no serviço local ({type(exc).__name__})", err=True)
        raise typer.Exit(1) from exc
    except KeyboardInterrupt:
        typer.echo("Serviço local encerrado.")


@app.command()
def devices():
    show(request("GET", "/api/devices").json())


@app.command("list")
def list_projects():
    show(request("GET", "/api/projects").json())


@app.command()
def pull(device: Annotated[str, typer.Option("--device")]):
    show(request("POST", f"/api/pull/{quote(device, safe='')}").json())


@app.command("pull-all")
def pull_all():
    show(request("POST", "/api/pull-all").json())


@app.command("job-status")
def job_status(job_id: str):
    show(request("GET", f"/api/jobs/{quote(job_id, safe='')}").json())


@app.command()
def chats(project: str = "", offset: int = 0, limit: int = 30):
    show(
        request("GET", "/api/chats", params={"project_id": project, "offset": offset, "limit": limit}).json()
    )


@app.command()
def search(query: str, project: str = "", offset: int = 0, limit: int = 20):
    show(
        request(
            "GET", "/api/search", params={"q": query, "project_id": project, "offset": offset, "limit": limit}
        ).json()
    )


@app.command()
def read(chat: str, offset: int = 0, limit: int = 30):
    show(
        request("GET", f"/api/chats/{quote(chat, safe='')}", params={"offset": offset, "limit": limit}).json()
    )


@app.command()
def export(project: str, output: Path = Path(".local/exports")):
    downloads = [
        (ext, request("GET", f"/api/projects/{quote(project, safe='')}/export/{kind}").content)
        for kind, ext in (("json", "json"), ("markdown", "md"))
    ]
    output.mkdir(parents=True, exist_ok=True)
    for extension, content in downloads:
        path = output / f"{quote(project, safe='')}.{extension}"
        path.write_bytes(content)
        typer.echo(str(path.resolve()))


@app.command("continue-context")
def continue_context(chat: str, message: Annotated[list[str] | None, typer.Option()] = None):
    result = request(
        "POST", f"/api/chats/{quote(chat, safe='')}/continue", json={"message_ids": message}
    ).json()
    typer.echo(result["prompt"])


@app.command("map-projects")
def map_projects(
    target: str, sources: list[str], confirm: Annotated[bool, typer.Option("--confirm")] = False
):
    show(
        request(
            "POST",
            "/api/projects/map",
            json={"target_id": target, "source_ids": sources, "confirmed": confirm},
        ).json()
    )


@app.command("revoke-device")
def revoke_device(device_id: str):
    show(request("POST", f"/api/devices/{quote(device_id, safe='')}/revoke").json())


@app.command()
def reconstruct(
    archives: list[Path],
    selectors: Path | None = None,
    execute: bool = False,
    profile: Path = Path(".local/browser-profile"),
    journal_dir: Path = Path(".local/reconstruction"),
):
    """Planeje ou execute a reconstrução experimental de projetos exportados."""
    plans = [make_plan(json.loads(archive.read_text(encoding="utf-8"))) for archive in archives]
    if len({p["project_id"] for p in plans}) != len(plans):
        raise typer.BadParameter("Passe apenas uma exportação por projeto")
    if not execute:
        show(
            {
                "projects": [
                    {"name": p["name"], "chats": [{"id": c["id"], "title": c["title"]} for c in p["chats"]]}
                    for p in plans
                ]
            }
        )
        return
    if not selectors:
        raise typer.BadParameter("Informe um arquivo de seletores validado com --selectors")
    from playwright.sync_api import sync_playwright

    selectors_data = json.loads(selectors.read_text(encoding="utf-8"))
    journal_dir.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as playwright:
        context = playwright.chromium.launch_persistent_context(str(profile), headless=False)
        try:
            page = context.pages[0] if context.pages else context.new_page()
            page.goto("https://chatgpt.com/")
            typer.confirm(
                "Conclua o login no navegador. Iniciar a criação de projetos e conversas?", abort=True
            )
            for plan in plans:
                journal = journal_dir / f"{digest(plan['project_id'])}.json"
                lock = journal.with_suffix(".lock")
                try:
                    fd = os.open(lock, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                except FileExistsError as exc:
                    raise typer.BadParameter(f"Reconstrução já em andamento para {plan['name']}") from exc
                os.close(fd)
                try:
                    show(execute_plan(page, plan, selectors_data, journal, "https://chatgpt.com/"))
                finally:
                    lock.unlink()
        finally:
            context.close()
