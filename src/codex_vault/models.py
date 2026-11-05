from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


def now() -> datetime:
    return datetime.now(UTC)


def iso(value: datetime | None = None) -> str:
    return (value or now()).isoformat()


def digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Item(StrictModel):
    id: str = Field(min_length=1, max_length=300)
    turn_id: str | None = None
    kind: str = "message"
    role: Literal["user", "assistant", "tool", "system", "unknown"] = "unknown"
    text: str = ""
    timestamp: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)


class Header(StrictModel):
    source_id: str = Field(min_length=1, max_length=300)
    workspace: str = Field(default="default", min_length=1, max_length=300)
    title: str = Field(default="Sem título", max_length=2000)
    cwd: str = Field(default="", max_length=4000)
    repository: str | None = None
    forked_from_id: str | None = None
    archived: bool = False
    updated_at: str | None = None


class Snapshot(StrictModel):
    header: Header
    items: list[Item]

    @model_validator(mode="after")
    def unique_items(self):
        if len({i.id for i in self.items}) != len(self.items):
            raise ValueError("IDs de itens duplicados na conversa")
        return self

    @property
    def checksum(self) -> str:
        return digest(self.model_dump())


class Patch(StrictModel):
    header: Header
    base: str | None
    checksum: str = Field(pattern=r"^[a-f0-9]{64}$")
    upserts: list[Item]
    removed: list[str] = Field(default_factory=list)
    order: list[str]

    @model_validator(mode="after")
    def valid_ids(self):
        if len(set(self.order)) != len(self.order):
            raise ValueError("Ordem contém IDs duplicados")
        if len({i.id for i in self.upserts}) != len(self.upserts):
            raise ValueError("Upserts duplicados")
        if set(self.removed) & set(self.order):
            raise ValueError("Item removido continua na ordem")
        return self


def make_patch(current: Snapshot, previous: Snapshot | None) -> Patch:
    old = {i.id: i for i in previous.items} if previous else {}
    current_ids = {i.id for i in current.items}
    return Patch(
        header=current.header,
        base=previous.checksum if previous else None,
        checksum=current.checksum,
        upserts=[i for i in current.items if old.get(i.id) != i],
        removed=sorted(set(old) - current_ids),
        order=[i.id for i in current.items],
    )


class PairRequest(StrictModel):
    name: str = Field(min_length=1, max_length=100, pattern=r"^[\w .-]+$")


class PairRedeem(StrictModel):
    code: str = Field(min_length=20, max_length=200)


class JobResult(StrictModel):
    error: str | None = Field(default=None, max_length=1000)
    threads_seen: int = Field(default=0, ge=0)
    threads_sent: int = Field(default=0, ge=0)
    excluded: int = Field(default=0, ge=0)


class MergeProjects(StrictModel):
    source_ids: list[str] = Field(min_length=1, max_length=100)
    target_id: str
    confirmed: bool
