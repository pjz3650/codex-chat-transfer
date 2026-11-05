from __future__ import annotations

import asyncio
import json
import shutil
from collections.abc import AsyncIterator
from contextlib import suppress
from datetime import UTC, datetime
from fnmatch import fnmatchcase
from pathlib import Path

from .models import Header, Item, Snapshot, digest
from .security import sanitize

SOURCES = [
    "cli",
    "vscode",
    "exec",
    "appServer",
    "subAgent",
    "subAgentReview",
    "subAgentCompact",
    "subAgentThreadSpawn",
    "subAgentOther",
    "unknown",
]


class CollectorError(Exception):
    pass


class RpcError(CollectorError):
    def __init__(self, method: str, code: int | None):
        super().__init__(f"Codex rejeitou {method} (código {code})")
        self.code = code


class CodexClient:
    def __init__(self, executable: str = "codex", timeout: float = 45):
        self.executable = executable
        self.timeout = timeout
        self.process: asyncio.subprocess.Process | None = None
        self.request_id = 0

    async def __aenter__(self):
        executable = shutil.which(self.executable)
        if not executable:
            raise CollectorError("Executável do Codex não encontrado. Configure --codex.")
        self.process = await asyncio.create_subprocess_exec(  # noqa: S603
            executable,
            "app-server",
            "--listen",
            "stdio://",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            limit=64 * 1024 * 1024,
        )
        try:
            await self.call("initialize", {"clientInfo": {"name": "codex-vault", "version": "0.1.0"}})
            await self.send({"method": "initialized"})
        except BaseException:
            await self.__aexit__()
            raise
        return self

    async def __aexit__(self, *_exc):
        process = self.process
        if process and process.returncode is None:
            if process.stdin:
                process.stdin.close()
            try:
                await asyncio.wait_for(process.wait(), 3)
            except TimeoutError:
                with suppress(ProcessLookupError):
                    process.kill()
                await process.wait()

    async def send(self, payload: dict):
        self.process.stdin.write((json.dumps(payload) + "\n").encode())
        await self.process.stdin.drain()

    async def call(self, method: str, params: dict) -> dict:
        self.request_id += 1
        request_id = self.request_id
        try:
            async with asyncio.timeout(self.timeout):
                await self.send({"id": request_id, "method": method, "params": params})
                while line := await self.process.stdout.readline():
                    message = json.loads(line)
                    if message.get("id") != request_id or "method" in message:
                        continue
                    if "error" in message:
                        raise RpcError(method, message["error"].get("code"))
                    return message["result"]
        except TimeoutError as exc:
            raise CollectorError(f"Tempo limite ao consultar {method}") from exc
        except (ValueError, KeyError, OSError) as exc:
            raise CollectorError(f"Falha de comunicação em {method}") from exc
        raise CollectorError("O app-server fechou antes de responder")

    async def pages(self, method: str, params: dict) -> AsyncIterator[dict]:
        cursor = None
        seen = set()
        while True:
            result = await self.call(method, {**params, "limit": 50, "cursor": cursor})
            for entry in result["data"]:
                yield entry
            cursor = result.get("nextCursor")
            if cursor is None:
                break
            if cursor in seen:
                raise CollectorError(f"Paginação repetida em {method}")
            seen.add(cursor)

    async def items(self, thread_id: str) -> list[Item]:
        try:
            return [
                normalize_item(entry["item"], entry.get("turnId"), timestamp(entry.get("startedAtMs"), 1000))
                async for entry in self.pages(
                    "thread/items/list", {"threadId": thread_id, "sortDirection": "asc"}
                )
            ]
        except RpcError as exc:
            if exc.code not in {-32601, -32600, -32000}:
                raise
        try:
            turns = [
                turn
                async for turn in self.pages(
                    "thread/turns/list",
                    {
                        "threadId": thread_id,
                        "sortDirection": "asc",
                        "itemsView": "full",
                    },
                )
            ]
        except RpcError as exc:
            if exc.code not in {-32601, -32600, -32000}:
                raise
            result = await self.call("thread/read", {"threadId": thread_id, "includeTurns": True})
            turns = result["thread"]["turns"]
        return [
            normalize_item(item, turn.get("id"), timestamp(turn.get("startedAt")))
            for turn in turns
            for item in turn.get("items", [])
        ]


def timestamp(value, divisor: int = 1) -> str | None:
    if isinstance(value, (float, int)):
        return datetime.fromtimestamp(value / divisor, tz=UTC).isoformat()
    return value if isinstance(value, str) else None


def normalize_item(raw: dict, turn_id: str | None, date: str | None) -> Item:
    kind = str(raw.get("type", "unknown"))
    role = {"userMessage": "user", "agentMessage": "assistant", "assistantMessage": "assistant"}.get(
        kind, "tool"
    )
    parts = raw.get("content", [])
    text = raw.get("text")
    if not isinstance(text, str):
        text = (
            "\n".join(p["text"] for p in parts if isinstance(p, dict) and isinstance(p.get("text"), str))
            if isinstance(parts, list)
            else ""
        )
    if not text and role == "tool":
        text = next(
            (
                raw[k]
                for k in ("aggregatedOutput", "command", "output", "summary")
                if isinstance(raw.get(k), str)
            ),
            "",
        )
    return Item(
        id=digest([turn_id, raw.get("id") or raw]),
        turn_id=turn_id,
        kind=kind,
        role=role,
        text=text,
        timestamp=date,
        payload=raw,
    )


class Selection:
    def __init__(self, include: list[str] | None = None, exclude: list[str] | None = None):
        self.include = include or []
        self.exclude = exclude or []

    def accepts(self, cwd: str) -> bool:
        path = cwd.replace("\\", "/").casefold()
        match = lambda pattern: fnmatchcase(path, pattern.replace("\\", "/").casefold())  # noqa: E731
        return (not self.include or any(map(match, self.include))) and not any(map(match, self.exclude))


class CodexSource:
    def __init__(self, executable: str, workspace: str, selection: Selection):
        self.executable = executable
        self.workspace = workspace
        self.selection = selection
        self.excluded = 0

    async def snapshots(self) -> AsyncIterator[Snapshot]:
        self.excluded = 0
        seen = set()
        async with CodexClient(self.executable) as client:
            for archived in (False, True):
                async for thread in client.pages(
                    "thread/list", {"archived": archived, "modelProviders": [], "sourceKinds": SOURCES}
                ):
                    source_id = thread["id"]
                    if source_id in seen or thread.get("ephemeral"):
                        continue
                    seen.add(source_id)
                    cwd = thread.get("cwd") or ""
                    if not self.selection.accepts(cwd):
                        self.excluded += 1
                        continue
                    remote = (thread.get("gitInfo") or {}).get("originUrl")
                    header = Header(
                        source_id=source_id,
                        workspace=self.workspace,
                        title=(thread.get("name") or thread.get("preview") or "Sem título")[:2000],
                        cwd=cwd,
                        repository=remote,
                        archived=archived,
                        updated_at=timestamp(thread.get("updatedAt")),
                        forked_from_id=thread.get("forkedFromId"),
                    )
                    yield sanitize(Snapshot(header=header, items=await client.items(source_id)))


class JsonSource:
    """Portable and deterministic input for demos, offline imports and tests."""

    def __init__(self, path: Path, selection: Selection):
        self.path = path
        self.selection = selection
        self.excluded = 0

    async def snapshots(self) -> AsyncIterator[Snapshot]:
        self.excluded = 0
        try:
            data = json.loads(self.path.read_text(encoding="utf-8-sig"))
            snapshots = data if isinstance(data, list) else data["snapshots"]
            for item in snapshots:
                snapshot = Snapshot.model_validate(item)
                if self.selection.accepts(snapshot.header.cwd):
                    yield sanitize(snapshot)
                else:
                    self.excluded += 1
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise CollectorError("Arquivo de entrada inválido ou indisponível") from exc
