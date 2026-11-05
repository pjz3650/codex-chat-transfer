from __future__ import annotations

import hmac
import time
from datetime import timedelta
from urllib.parse import urlsplit

from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    AuthorizeError,
    RefreshToken,
    RegistrationError,
    TokenError,
    construct_redirect_uri,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from sqlalchemy import delete

from .models import iso, now
from .security import token
from .store import Database, OAuthState, VaultError, hashed

SCOPE = "vault"


class VaultOAuth:
    """OAuth for one self-hosted archive. The SDK handles protocol and PKCE checks."""

    def __init__(self, db: Database):
        self.db = db
        self.issuer = db.settings.public_url
        self.resource = f"{self.issuer}/mcp"
        self.generation = hashed(db.settings.admin_token.get_secret_value())

    def key(self, kind: str, secret: str) -> str:
        return f"{kind}:{hashed(self.generation + secret)}"

    def put(self, kind: str, secret: str, data: dict, ttl: int | None = None):
        with self.db.session() as s:
            s.execute(delete(OAuthState).where(OAuthState.expires < iso()))
            s.add(
                OAuthState(
                    key=self.key(kind, secret),
                    kind=kind,
                    data=data,
                    expires=iso(now() + timedelta(seconds=ttl)) if ttl else None,
                )
            )

    def get(self, kind: str, secret: str) -> dict | None:
        with self.db.session() as s:
            row = s.get(OAuthState, self.key(kind, secret))
            return row.data if row and (not row.expires or row.expires >= iso()) else None

    def take(self, kind: str, secret: str) -> dict | None:
        with self.db.session() as s:
            return s.scalar(
                delete(OAuthState)
                .where(OAuthState.key == self.key(kind, secret), OAuthState.expires >= iso())
                .returning(OAuthState.data)
            )

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        data = self.get("client", client_id)
        return OAuthClientInformationFull.model_validate(data) if data else None

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        allowed = set(self.db.settings.oauth_redirect_hosts)
        for uri in client_info.redirect_uris or []:
            parsed = urlsplit(str(uri))
            host = parsed.hostname
            scheme_ok = parsed.scheme == "https" or (
                parsed.scheme == "http" and host in {"localhost", "127.0.0.1"}
            )
            if not scheme_ok or host not in allowed or parsed.username or parsed.fragment:
                raise RegistrationError("invalid_redirect_uri", "Host de retorno não autorizado")
        self.put("client", client_info.client_id, client_info.model_dump(mode="json"))

    async def authorize(self, client: OAuthClientInformationFull, params: AuthorizationParams) -> str:
        if params.resource and params.resource != self.resource:
            raise AuthorizeError("invalid_request", "Recurso OAuth inválido")
        nonce = token()
        self.put(
            "consent", nonce, {"client_id": client.client_id, "params": params.model_dump(mode="json")}, 600
        )
        return f"{self.issuer}/vault/consent/{nonce}"

    def approve(self, nonce: str, admin_token: str) -> str:
        if not hmac.compare_digest(
            admin_token.encode(), self.db.settings.admin_token.get_secret_value().encode()
        ):
            raise VaultError("Chave do acervo inválida", 401)
        data = self.take("consent", nonce)
        if data is None:
            raise VaultError("Solicitação de conexão expirada", 404)
        params = AuthorizationParams.model_validate(data["params"])
        code = token()
        stored = AuthorizationCode(
            code=code,
            scopes=params.scopes or [SCOPE],
            expires_at=time.time() + 120,
            client_id=data["client_id"],
            code_challenge=params.code_challenge,
            redirect_uri=params.redirect_uri,
            redirect_uri_provided_explicitly=params.redirect_uri_provided_explicitly,
            resource=self.resource,
            subject="vault-owner",
        )
        self.put("code", code, stored.model_dump(mode="json", exclude={"code"}), 120)
        return construct_redirect_uri(str(params.redirect_uri), code=code, state=params.state)

    async def load_authorization_code(self, client: OAuthClientInformationFull, authorization_code: str):
        data = self.get("code", authorization_code)
        if data and data["client_id"] == client.client_id:
            return AuthorizationCode(code=authorization_code, **data)
        return None

    async def exchange_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: AuthorizationCode
    ):
        if self.take("code", authorization_code.code) is None:
            raise TokenError("invalid_grant", "Código expirado ou já utilizado")
        return self.issue(client.client_id, authorization_code.scopes)

    def issue(self, client_id: str, scopes: list[str]) -> OAuthToken:
        access, refresh = token(), token()
        access_ttl, refresh_ttl = 3600, 30 * 86400
        data = {"client_id": client_id, "scopes": scopes, "resource": self.resource, "subject": "vault-owner"}
        self.put("access", access, {**data, "expires_at": int(time.time()) + access_ttl}, access_ttl)
        self.put("refresh", refresh, {**data, "expires_at": int(time.time()) + refresh_ttl}, refresh_ttl)
        return OAuthToken(
            access_token=access,
            expires_in=access_ttl,
            refresh_token=refresh,
            scope=" ".join(scopes),
        )

    async def load_refresh_token(self, client: OAuthClientInformationFull, refresh_token: str):
        data = self.get("refresh", refresh_token)
        if data and data["client_id"] == client.client_id:
            return RefreshToken(token=refresh_token, **data)
        return None

    async def exchange_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: RefreshToken, scopes: list[str]
    ):
        if self.take("refresh", refresh_token.token) is None:
            raise TokenError("invalid_grant", "Refresh token expirado ou já utilizado")
        return self.issue(client.client_id, scopes or refresh_token.scopes)

    async def load_access_token(self, token: str):
        data = self.get("access", token)
        return AccessToken(token=token, **data) if data else None

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        self.take("access" if isinstance(token, AccessToken) else "refresh", token.token)
