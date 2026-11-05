from __future__ import annotations

import asyncio
import logging
import sqlite3
from collections.abc import AsyncIterator
from contextlib import closing, suppress
from pathlib import Path
from typing import Protocol

import httpx

from .collector import CollectorError, Selection
from .models import JobResult, Patch, Snapshot, digest, make_patch
from .security import sanitize

log = logging.getLogger(__name__)


class Source(Protocol):
    excluded: int

    def snapshots(self) -> AsyncIterator[Snapshot]: ...


class Spool:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        with closing(self.connect()) as conn, conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute(
                "CREATE TABLE IF NOT EXISTS snapshots (key TEXT PRIMARY KEY, acknowledged TEXT, pending TEXT)"
            )

    def connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path, timeout=30)

    def stage(self, snapshot: Snapshot):
        key = digest([snapshot.header.workspace, snapshot.header.source_id])
        payload = snapshot.model_dump_json()
        with closing(self.connect()) as conn, conn:
            conn.execute(
                """INSERT INTO snapshots (key, pending) VALUES (?, ?)
                         ON CONFLICT (key) DO UPDATE SET pending = CASE
                         WHEN snapshots.acknowledged = excluded.pending AND snapshots.pending IS NULL
                         THEN NULL ELSE excluded.pending END""",
                (key, payload),
            )

    def pending(self):
        with closing(self.connect()) as conn:
            for key, ack, pending in conn.execute(
                "SELECT key, acknowledged, pending FROM snapshots WHERE pending IS NOT NULL"
            ):
                previous = Snapshot.model_validate_json(ack) if ack else None
                yield key, previous, Snapshot.model_validate_json(pending)

    def acknowledge(self, key: str, snapshot: Snapshot):
        payload = snapshot.model_dump_json()
        with closing(self.connect()) as conn, conn:
            conn.execute(
                """UPDATE snapshots SET acknowledged = ?,
                         pending = CASE WHEN pending = ? THEN NULL ELSE pending END WHERE key = ?""",
                (payload, payload, key),
            )


class Agent:
    def __init__(
        self,
        client: httpx.AsyncClient,
        spool: Spool,
        source: Source,
        selection: Selection | None = None,
        renew_interval: float = 20,
    ):
        self.client = client
        self.spool = spool
        self.source = source
        self.selection = selection or Selection()
        self.renew_interval = renew_interval

    async def post(self, path: str, body: dict | None = None) -> dict:
        response = await self.client.post(path, json=body)
        response.raise_for_status()
        return response.json()

    async def upload(self, patch: Patch, job: dict):
        body = {"patch": patch.model_dump(), "lease_token": job["lease_token"]}
        return await self.post(f"/api/agent/jobs/{job['id']}/ingest", body)

    async def sync(self, job: dict) -> JobResult:
        seen = 0
        async for snapshot in self.source.snapshots():
            if self.selection.accepts(snapshot.header.cwd):
                self.spool.stage(sanitize(snapshot))
                seen += 1
        sent = 0
        for key, previous, snapshot in self.spool.pending():
            if not self.selection.accepts(snapshot.header.cwd):
                continue
            patch = make_patch(snapshot, previous)
            try:
                receipt = await self.upload(patch, job)
            except httpx.HTTPStatusError as exc:
                if (
                    exc.response.status_code != 409
                    or exc.response.json().get("detail") != "Base divergente; reenvie snapshot completo"
                ):
                    raise
                status = await self.post(
                    f"/api/agent/jobs/{job['id']}/base",
                    {
                        "lease_token": job["lease_token"],
                        "header": snapshot.header.model_dump(),
                    },
                )
                if status["checksum"] == snapshot.checksum:
                    receipt = {"checksum": snapshot.checksum, "changed": False}
                else:
                    # Replace the acknowledged base without deleting any other device's copy.
                    replacement = make_patch(snapshot, None)
                    replacement.base = status["checksum"]
                    replacement.removed = sorted(set(status["ids"]) - set(replacement.order))
                    receipt = await self.upload(replacement, job)
            if receipt.get("checksum") != snapshot.checksum:
                raise CollectorError("Servidor não confirmou a integridade do envio")
            self.spool.acknowledge(key, snapshot)
            sent += int(receipt.get("changed", False))
        return JobResult(threads_seen=seen, threads_sent=sent, excluded=self.source.excluded)

    async def keepalive(self, job: dict):
        while True:
            await asyncio.sleep(self.renew_interval)
            await self.post(f"/api/agent/jobs/{job['id']}/renew", {"lease_token": job["lease_token"]})

    async def once(self) -> dict | None:
        job = (await self.post("/api/agent/claim"))["job"]
        if not job:
            return None
        renewal = asyncio.create_task(self.keepalive(job))
        work = asyncio.create_task(self.sync(job))
        try:
            done, _ = await asyncio.wait({work, renewal}, return_when=asyncio.FIRST_COMPLETED)
            if renewal in done:
                await renewal
            result = work.result()
        except CollectorError as exc:
            result = JobResult(error=str(exc))
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code >= 500 or exc.response.status_code == 409:
                raise
            result = JobResult(error=f"Servidor rejeitou a sincronização (HTTP {exc.response.status_code})")
        finally:
            for task in (work, renewal):
                task.cancel()
            for task in (work, renewal):
                with suppress(asyncio.CancelledError, Exception):
                    await task
        await self.post(
            f"/api/agent/jobs/{job['id']}/finish",
            {
                "lease_token": job["lease_token"],
                "result": result.model_dump(),
            },
        )
        return result.model_dump()

    async def run(self, poll: float = 10, sync_every: float | None = None):
        loop = asyncio.get_running_loop()
        next_sync = loop.time()
        delay = poll
        while True:
            try:
                if sync_every and loop.time() >= next_sync:
                    await self.post("/api/agent/request-sync")
                    next_sync = loop.time() + sync_every
                result = await self.once()
                if result:
                    log.info("Sincronização %s", "falhou" if result["error"] else "concluída")
                delay = poll
            except httpx.HTTPError as exc:
                if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code == 401:
                    raise CollectorError("Credencial do dispositivo rejeitada") from exc
                log.warning("Conexão indisponível ou job expirado; nova tentativa em %.0fs", delay)
                delay = min(60, delay * 2)
            await asyncio.sleep(delay)
