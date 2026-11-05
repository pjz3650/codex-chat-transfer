from pathlib import Path

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="VAULT_", env_file=".env", extra="ignore", hide_input_in_errors=True
    )

    database_url: str = "sqlite:///.local/vault.db"
    admin_token: SecretStr = Field(default=SecretStr(""))
    public_url: str = "http://127.0.0.1:8765"
    device_ttl_seconds: int = Field(default=90, ge=1)
    job_lease_seconds: int = Field(default=300, ge=1)
    max_body_bytes: int = Field(default=64 * 1024 * 1024, ge=1024)
    oauth_redirect_hosts: list[str] = Field(
        default_factory=lambda: [
            "chatgpt.com",
            "platform.openai.com",
            "127.0.0.1",
            "localhost",
        ]
    )

    @model_validator(mode="after")
    def validate_config(self):
        if len(self.admin_token.get_secret_value()) < 32:
            raise ValueError("Defina VAULT_ADMIN_TOKEN com pelo menos 32 caracteres aleatórios")
        self.public_url = self.public_url.rstrip("/")
        from urllib.parse import urlsplit

        url = urlsplit(self.public_url)
        local_http = url.scheme == "http" and url.hostname in {"127.0.0.1", "localhost"}
        if (
            not url.hostname
            or url.username
            or url.path
            or url.query
            or url.fragment
            or (url.scheme != "https" and not local_http)
        ):
            raise ValueError("VAULT_PUBLIC_URL deve usar HTTPS, exceto localhost")
        if self.database_url.startswith("sqlite:///"):
            path = self.database_url.removeprefix("sqlite:///")
            if path != ":memory:":
                Path(path).parent.mkdir(parents=True, exist_ok=True)
        return self
