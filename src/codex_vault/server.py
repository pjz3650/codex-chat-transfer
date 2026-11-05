import hmac
from contextlib import asynccontextmanager
from html import escape
from typing import Annotated, Literal
from urllib.parse import urlsplit

from fastapi import Depends, FastAPI, Form, Query, Request
from fastapi import Header as HttpHeader
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from mcp.server.auth.middleware.client_auth import AuthenticationError, ClientAuthenticator
from pydantic import Field

from .archive import continuation, export_content
from .config import Settings
from .mcp_app import build_mcp, widget_html
from .models import Header, JobResult, MergeProjects, PairRedeem, PairRequest, Patch, StrictModel, iso
from .oauth import VaultOAuth
from .store import Database, OAuthState, VaultError, hashed


class Lease(StrictModel):
    lease_token: str = Field(min_length=20, max_length=100)


class Upload(Lease):
    patch: Patch


class BaseRequest(Lease):
    header: Header


class Finish(Lease):
    result: JobResult


class ContinueRequest(StrictModel):
    message_ids: list[str] | None = Field(default=None, max_length=24)


def bearer(authorization: str | None) -> str:
    scheme, _, value = (authorization or "").partition(" ")
    if scheme.casefold() != "bearer" or not value:
        raise VaultError("Autenticação necessária", 401)
    return value


def download(content: str, name: str, content_type: str) -> Response:
    return Response(
        content,
        media_type=content_type,
        headers={
            "Content-Disposition": f'attachment; filename="{name}"',
            "Cache-Control": "no-store",
            "Referrer-Policy": "no-referrer",
            "X-Content-Type-Options": "nosniff",
        },
    )


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()
    db = Database(settings)
    oauth = VaultOAuth(db)
    mcp = build_mcp(db, oauth)
    mcp_asgi = mcp.streamable_http_app()

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        db.initialize()
        try:
            async with mcp.session_manager.run():
                yield
        finally:
            db.engine.dispose()

    app = FastAPI(title="Codex Vault", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.db = db

    @app.middleware("http")
    async def protect_requests(request: Request, call_next):
        if request.url.path.startswith("/api/agent/") and request.method == "POST":
            declared = request.headers.get("content-length")
            if declared and (
                not declared.isascii() or not declared.isdecimal() or int(declared) > settings.max_body_bytes
            ):
                return JSONResponse({"detail": "Envio excede o limite configurado"}, status_code=413)
            # Require a declared bound for uploads so streaming bodies cannot bypass it.
            if request.url.path.endswith("/ingest") and (
                declared is None or request.headers.get("transfer-encoding")
            ):
                return JSONResponse({"detail": "Envio requer Content-Length"}, status_code=411)
        response = await call_next(request)
        response.headers.setdefault("Cache-Control", "no-store")
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        return response

    @app.exception_handler(VaultError)
    async def vault_error(_request, exc: VaultError):
        return JSONResponse({"detail": str(exc)}, status_code=exc.status)

    @app.exception_handler(RequestValidationError)
    async def invalid_request(_request, exc: RequestValidationError):
        return JSONResponse(
            {"detail": [{"loc": e["loc"], "type": e["type"]} for e in exc.errors()]}, status_code=422
        )

    def admin(authorization: Annotated[str | None, HttpHeader()] = None):
        value = bearer(authorization)
        if not hmac.compare_digest(value.encode(), settings.admin_token.get_secret_value().encode()):
            raise VaultError("Credencial do acervo inválida", 401)

    def agent(authorization: Annotated[str | None, HttpHeader()] = None) -> str:
        return db.authenticate_device(bearer(authorization))

    Owner = Depends(admin)
    DeviceAuth = Annotated[str, Depends(agent)]
    Offset = Annotated[int, Query(ge=0)]
    Limit = Annotated[int, Query(ge=1, le=100)]

    @app.get("/health")
    def health():
        return {"status": "ok"}

    @app.get("/preview", response_class=HTMLResponse)
    def preview():
        return widget_html()

    @app.get("/vault/consent/{nonce}", response_class=HTMLResponse)
    def consent(nonce: str):
        grant = oauth.get("consent", nonce)
        if not grant:
            raise VaultError("Solicitação de conexão expirada", 404)
        host = escape(urlsplit(grant["params"]["redirect_uri"]).hostname or "")
        return HTMLResponse(
            f"""<!doctype html><html lang="pt-BR"><meta charset="utf-8">
          <meta name="viewport" content="width=device-width, initial-scale=1">
          <title>Conectar Codex Vault</title>
          <main><h1>Conectar Codex Vault</h1><p>Autorize {host} a consultar o acervo e solicitar sincronizações.</p>
          <form method="post"><label for="admin_token">Chave do acervo</label>
          <input id="admin_token" name="admin_token" type="password" required autocomplete="current-password">
          <button>Autorizar conexão</button></form></main></html>""",
            headers={
                "Content-Security-Policy": "default-src 'none'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'",
                "Referrer-Policy": "no-referrer",
            },
        )

    @app.post("/vault/consent/{nonce}")
    def approve(nonce: str, admin_token: Annotated[str, Form(min_length=1, max_length=1024)]):
        return RedirectResponse(
            oauth.approve(nonce, admin_token), status_code=303, headers={"Referrer-Policy": "no-referrer"}
        )

    @app.post("/revoke")
    async def revoke_oauth(request: Request):
        # MCP 1.x's revocation form requires client_secret even for public clients.
        # Reuse its client authentication and accept the RFC 7009 public-client form.
        try:
            client = await ClientAuthenticator(oauth).authenticate_request(request)
        except AuthenticationError:
            return JSONResponse({"error": "unauthorized_client"}, status_code=401)
        form = await request.form()
        secret = form.get("token")
        if not isinstance(secret, str) or not secret or len(secret) > 200:
            return JSONResponse({"error": "invalid_request"}, status_code=400)
        credential = await oauth.load_access_token(secret) or await oauth.load_refresh_token(client, secret)
        if credential and credential.client_id == client.client_id:
            await oauth.revoke_token(credential)
        return Response(status_code=200, headers={"Pragma": "no-cache"})

    @app.post("/api/pairings", dependencies=[Owner], status_code=201)
    def pairing(body: PairRequest):
        return db.create_pairing(body.name.strip())

    @app.post("/api/agent/pair", status_code=201)
    def redeem(body: PairRedeem):
        return db.redeem(body.code)

    @app.get("/api/devices", dependencies=[Owner])
    def devices():
        return {"devices": db.devices()}

    @app.post("/api/devices/{device_id}/revoke", dependencies=[Owner])
    def revoke(device_id: str):
        db.revoke_device(device_id)
        return {"revoked": True}

    @app.post("/api/pull/{device}", dependencies=[Owner], status_code=202)
    def pull(device: str):
        return db.enqueue(device)

    @app.post("/api/pull-all", dependencies=[Owner], status_code=202)
    def pull_all():
        return db.enqueue()

    @app.get("/api/jobs/{job_id}", dependencies=[Owner])
    def job_status(job_id: str):
        return db.job_status(job_id)

    @app.post("/api/agent/claim")
    def claim(device_id: DeviceAuth):
        return {"job": db.claim(device_id)}

    @app.post("/api/agent/request-sync", status_code=202)
    def request_sync(device_id: DeviceAuth):
        return db.enqueue(device_id)

    @app.post("/api/agent/jobs/{job_id}/renew")
    def renew(job_id: str, body: Lease, device_id: DeviceAuth):
        db.renew_job(device_id, job_id, body.lease_token)
        return {"renewed": True}

    @app.post("/api/agent/jobs/{job_id}/base")
    def base(job_id: str, body: BaseRequest, device_id: DeviceAuth):
        return db.patch_base(device_id, job_id, body.lease_token, body.header)

    @app.post("/api/agent/jobs/{job_id}/ingest")
    def ingest(job_id: str, body: Upload, device_id: DeviceAuth):
        return db.ingest(device_id, body.patch, job_id, body.lease_token)

    @app.post("/api/agent/jobs/{job_id}/finish")
    def finish(job_id: str, body: Finish, device_id: DeviceAuth):
        db.finish_job(device_id, job_id, body.lease_token, body.result.model_dump())
        return {"finished": True}

    @app.get("/api/projects", dependencies=[Owner])
    def projects():
        return db.projects()

    @app.post("/api/projects/map", dependencies=[Owner])
    def map_projects(body: MergeProjects):
        return db.merge_projects(body.source_ids, body.target_id, body.confirmed)

    @app.get("/api/chats", dependencies=[Owner])
    def chats(project_id: str | None = None, offset: Offset = 0, limit: Limit = 50):
        return db.chats(project_id, offset, limit)

    @app.get("/api/chats/{chat_id}", dependencies=[Owner])
    def read(chat_id: str, offset: Offset = 0, limit: Limit = 50):
        return db.read(chat_id, offset, limit)

    @app.post("/api/chats/{chat_id}/continue", dependencies=[Owner])
    def continue_chat(chat_id: str, body: ContinueRequest):
        return continuation(db, chat_id, body.message_ids)

    @app.get("/api/search", dependencies=[Owner])
    def search(
        q: Annotated[str, Query(min_length=1, max_length=500)],
        project_id: str | None = None,
        offset: Offset = 0,
        limit: Limit = 30,
    ):
        return db.search(q, project_id, offset, limit)

    @app.get("/api/projects/{project_id}/export/{kind}", dependencies=[Owner])
    def export(project_id: str, kind: Literal["json", "markdown"]):
        return download(*export_content(db, project_id, kind))

    @app.get("/download/{secret}")
    def export_download(secret: str):
        with db.session() as s:
            ticket = s.get(OAuthState, f"download:{hashed(secret)}")
            if not ticket or not ticket.expires or ticket.expires < iso():
                raise VaultError("Download expirado ou inválido", 404)
            data = ticket.data
        return download(*export_content(db, data["project_id"], data["format"]))

    app.mount("/", mcp_asgi)
    return app
