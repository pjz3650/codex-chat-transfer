from __future__ import annotations

import hashlib
import hmac
import uuid
from contextlib import contextmanager
from datetime import timedelta
from typing import Any

from sqlalchemy import (
    JSON,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    create_engine,
    select,
    update,
)
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker
from sqlalchemy.pool import StaticPool

from .config import Settings
from .models import Header, Item, Patch, Snapshot, digest, iso, now
from .security import sanitize, token


def uid() -> str:
    return str(uuid.uuid4())


def hashed(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


class VaultError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


class Base(DeclarativeBase):
    pass


class Device(Base):
    __tablename__ = "devices"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    name: Mapped[str] = mapped_column(String(100), unique=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    last_seen: Mapped[str | None] = mapped_column(String(40), nullable=True)
    last_sync: Mapped[str | None] = mapped_column(String(40), nullable=True)
    revoked: Mapped[bool] = mapped_column(default=False)


class Pairing(Base):
    __tablename__ = "pairings"
    code_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(100))
    expires: Mapped[str] = mapped_column(String(40))
    used: Mapped[bool] = mapped_column(default=False)


class Project(Base):
    __tablename__ = "projects"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(2000))
    repository: Mapped[str | None] = mapped_column(Text, nullable=True)
    redirect_id: Mapped[str | None] = mapped_column(ForeignKey("projects.id"), nullable=True)


class Thread(Base):
    __tablename__ = "threads"
    __table_args__ = (UniqueConstraint("device_id", "workspace", "source_id"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    device_id: Mapped[str] = mapped_column(ForeignKey("devices.id"), index=True)
    workspace: Mapped[str] = mapped_column(String(300))
    source_id: Mapped[str] = mapped_column(String(300))
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), index=True)
    header: Mapped[dict] = mapped_column(JSON)
    checksum: Mapped[str] = mapped_column(String(64))
    content_hash: Mapped[str] = mapped_column(String(64))
    synced_at: Mapped[str] = mapped_column(String(40))


class Record(Base):
    __tablename__ = "records"
    __table_args__ = (UniqueConstraint("thread_id", "source_id"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    thread_id: Mapped[str] = mapped_column(ForeignKey("threads.id", ondelete="CASCADE"), index=True)
    source_id: Mapped[str] = mapped_column(String(300))
    position: Mapped[int] = mapped_column(Integer)
    text: Mapped[str] = mapped_column(Text)
    data: Mapped[dict] = mapped_column(JSON)


class Job(Base):
    __tablename__ = "jobs"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    device_id: Mapped[str] = mapped_column(ForeignKey("devices.id"), index=True)
    state: Mapped[str] = mapped_column(String(20), default="pending")
    created_at: Mapped[str] = mapped_column(String(40), default=iso)
    lease_until: Mapped[str | None] = mapped_column(String(40), nullable=True)
    lease_token: Mapped[str | None] = mapped_column(String(100), nullable=True)
    result: Mapped[dict | None] = mapped_column(JSON, nullable=True)


class OAuthState(Base):
    __tablename__ = "oauth_state"
    key: Mapped[str] = mapped_column(String(200), primary_key=True)
    kind: Mapped[str] = mapped_column(String(30), index=True)
    data: Mapped[dict] = mapped_column(JSON)
    expires: Mapped[str | None] = mapped_column(String(40), nullable=True)


class Database:
    def __init__(self, settings: Settings):
        self.settings = settings
        options: dict[str, Any] = {"pool_pre_ping": True}
        if settings.database_url.startswith("sqlite"):
            options["connect_args"] = {"check_same_thread": False, "timeout": 30}
            if ":memory:" in settings.database_url:
                options["poolclass"] = StaticPool
        self.engine = create_engine(settings.database_url, **options)
        self.sessions = sessionmaker(self.engine, expire_on_commit=False)

    def initialize(self):
        Base.metadata.create_all(self.engine)
        if self.engine.dialect.name == "postgresql":
            from sqlalchemy import text

            with self.engine.begin() as conn:
                conn.execute(
                    text(
                        "CREATE INDEX IF NOT EXISTS records_text_gin ON records USING GIN (to_tsvector('simple', text))"
                    )
                )

    @contextmanager
    def session(self, *, write: bool = False):
        try:
            with self.sessions.begin() as session:
                if write and self.engine.dialect.name == "sqlite":
                    session.connection().exec_driver_sql("BEGIN IMMEDIATE")
                yield session
        except IntegrityError as exc:
            raise VaultError("Conflito de registro; atualize e tente novamente", 409) from exc

    def create_pairing(self, name: str) -> dict:
        code = token()
        expires = iso(now() + timedelta(minutes=10))
        with self.session(write=True) as s:
            if s.scalar(select(Device).where(Device.name == name)):
                raise VaultError("Nome de dispositivo já cadastrado", 409)
            s.add(Pairing(code_hash=hashed(code), name=name, expires=expires))
        return {"code": code, "expires_at": expires, "name": name}

    def redeem(self, code: str) -> dict:
        secret = token()
        with self.session(write=True) as s:
            pairing = s.get(Pairing, hashed(code))
            if not pairing or pairing.used or pairing.expires < iso():
                raise VaultError("Código inválido, expirado ou já utilizado", 401)
            claimed = s.execute(
                update(Pairing)
                .where(Pairing.code_hash == pairing.code_hash, Pairing.used.is_(False))
                .values(used=True)
            ).rowcount
            if not claimed:
                raise VaultError("Código já utilizado", 401)
            device = Device(name=pairing.name, token_hash=hashed(secret), last_seen=iso())
            s.add(device)
            s.flush()
            return {"device_id": device.id, "name": device.name, "token": secret}

    def authenticate_device(self, secret: str) -> str:
        with self.session() as s:
            device = s.scalar(select(Device).where(Device.token_hash == hashed(secret)))
            if not device or device.revoked:
                raise VaultError("Dispositivo não autorizado", 401)
            return device.id

    def devices(self) -> list[dict]:
        cutoff = iso(now() - timedelta(seconds=self.settings.device_ttl_seconds))
        with self.session() as s:
            return [
                {
                    "id": d.id,
                    "name": d.name,
                    "online": bool(d.last_seen and d.last_seen >= cutoff and not d.revoked),
                    "last_seen": d.last_seen,
                    "last_sync": d.last_sync,
                    "revoked": d.revoked,
                }
                for d in s.scalars(select(Device).order_by(Device.name))
            ]

    def revoke_device(self, device_id: str):
        with self.session(write=True) as s:
            device = s.scalar(select(Device).where(Device.id == device_id).with_for_update())
            if not device:
                raise VaultError("Dispositivo não encontrado", 404)
            device.revoked = True
            for job in s.scalars(
                select(Job).where(Job.device_id == device_id, Job.state.in_(["pending", "running"]))
            ):
                job.state, job.result = "failed", {"error": "Dispositivo revogado"}

    def enqueue(self, device: str | None = None) -> dict:
        devices = self.devices()
        selected = [
            d for d in devices if not d["revoked"] and (device is None or device in {d["id"], d["name"]})
        ]
        if device and not selected:
            raise VaultError("Dispositivo não encontrado", 404)
        result = []
        with self.session(write=True) as s:
            for d in selected:
                locked = s.scalar(select(Device).where(Device.id == d["id"]).with_for_update())
                if locked.revoked:
                    continue
                job = s.scalar(
                    select(Job)
                    .where(Job.device_id == d["id"], Job.state.in_(["pending", "running"]))
                    .limit(1)
                )
                if job is None:
                    job = Job(device_id=d["id"])
                    s.add(job)
                    s.flush()
                result.append(
                    {
                        "job_id": job.id,
                        "device": d["name"],
                        "state": job.state,
                        "availability": "online" if d["online"] else "pending_offline",
                    }
                )
        return {"jobs": result, "message": "Solicitações registradas; acompanhe com job_status."}

    def claim(self, device_id: str) -> dict | None:
        with self.session(write=True) as s:
            device = s.scalar(select(Device).where(Device.id == device_id).with_for_update())
            if not device or device.revoked:
                raise VaultError("Dispositivo não autorizado", 401)
            device.last_seen = iso()
            candidate = s.scalar(
                select(Job)
                .where(
                    Job.device_id == device_id,
                    (Job.state == "pending") | ((Job.state == "running") & (Job.lease_until < iso())),
                )
                .order_by(Job.created_at)
                .limit(1)
            )
            if candidate is None:
                return None
            lease = token()
            count = s.execute(
                update(Job)
                .where(
                    Job.id == candidate.id,
                    (Job.state == "pending") | ((Job.state == "running") & (Job.lease_until < iso())),
                )
                .values(
                    state="running",
                    lease_token=lease,
                    lease_until=iso(now() + timedelta(seconds=self.settings.job_lease_seconds)),
                )
            ).rowcount
            return {"id": candidate.id, "lease_token": lease} if count else None

    def active_job(self, s, device_id: str, job_id: str, lease: str) -> Job:
        device = s.scalar(select(Device).where(Device.id == device_id).with_for_update())
        if not device or device.revoked:
            raise VaultError("Dispositivo não autorizado", 401)
        job = s.scalar(select(Job).where(Job.id == job_id).with_for_update())
        if not job or job.device_id != device_id:
            raise VaultError("Job não encontrado", 404)
        if (
            job.state != "running"
            or not hmac.compare_digest(job.lease_token or "", lease)
            or job.lease_until < iso()
        ):
            raise VaultError("Lease expirado ou inválido", 409)
        return job

    def finish_job(self, device_id: str, job_id: str, lease: str, result: dict):
        with self.session(write=True) as s:
            job = self.active_job(s, device_id, job_id, lease)
            job.state = "failed" if result.get("error") else "complete"
            job.result = result
            if not result.get("error"):
                s.get(Device, device_id).last_sync = iso()

    def renew_job(self, device_id: str, job_id: str, lease: str):
        with self.session(write=True) as s:
            job = self.active_job(s, device_id, job_id, lease)
            job.lease_until = iso(now() + timedelta(seconds=self.settings.job_lease_seconds))
            s.get(Device, device_id).last_seen = iso()

    def job_status(self, job_id: str) -> dict:
        with self.session() as s:
            job = s.get(Job, job_id)
            if not job:
                raise VaultError("Job não encontrado", 404)
            return {"id": job.id, "state": job.state, "result": job.result, "created_at": job.created_at}

    def patch_base(self, device_id: str, job_id: str, lease: str, header: Header) -> dict:
        with self.session() as s:
            self.active_job(s, device_id, job_id, lease)
            thread = s.scalar(
                select(Thread).where(
                    Thread.device_id == device_id,
                    Thread.workspace == header.workspace,
                    Thread.source_id == header.source_id,
                )
            )
            return {
                "checksum": thread.checksum if thread else None,
                "ids": list(s.scalars(select(Record.source_id).where(Record.thread_id == thread.id)))
                if thread
                else [],
            }

    def ingest(self, device_id: str, patch: Patch, job_id: str, lease: str) -> dict:
        with self.session(write=True) as s:
            self.active_job(s, device_id, job_id, lease)
            thread = s.scalar(
                select(Thread)
                .where(
                    Thread.device_id == device_id,
                    Thread.source_id == patch.header.source_id,
                    Thread.workspace == patch.header.workspace,
                )
                .with_for_update()
            )
            if thread and thread.checksum == patch.checksum:
                return {"chat_id": thread.id, "checksum": thread.checksum, "changed": False}
            if patch.base != (thread.checksum if thread else None):
                raise VaultError("Base divergente; reenvie snapshot completo", 409)
            existing = (
                {r.source_id: r for r in s.scalars(select(Record).where(Record.thread_id == thread.id))}
                if thread
                else {}
            )
            merged = {k: Item.model_validate(r.data) for k, r in existing.items() if k not in patch.removed}
            merged.update({i.id: i for i in patch.upserts})
            if set(merged) != set(patch.order):
                raise VaultError("Patch incompleto ou ordem inválida", 422)
            snapshot = sanitize(Snapshot(header=patch.header, items=[merged[k] for k in patch.order]))
            if snapshot.checksum != patch.checksum:
                raise VaultError("Checksum inválido ou conteúdo não filtrado", 422)
            if thread is None:
                project_id = digest([device_id, snapshot.header.workspace, snapshot.header.cwd])
                project = s.scalar(select(Project).where(Project.id == project_id).with_for_update())
                if project is None:
                    name = snapshot.header.cwd.replace("\\", "/").rstrip("/").split("/")[-1] or "Sem pasta"
                    s.add(Project(id=project_id, name=name, repository=snapshot.header.repository))
                    s.flush()
                elif project.redirect_id:
                    project_id = project.redirect_id
                thread = Thread(
                    device_id=device_id,
                    workspace=snapshot.header.workspace,
                    source_id=snapshot.header.source_id,
                    project_id=project_id,
                    header={},
                    checksum="",
                    content_hash="",
                    synced_at=iso(),
                )
                s.add(thread)
                s.flush()
            order_set = set(patch.order)
            for key, record in existing.items():
                if key not in order_set:
                    s.delete(record)
            for position, item in enumerate(snapshot.items):
                record = existing.get(item.id)
                if record is None:
                    record = Record(
                        thread_id=thread.id,
                        source_id=item.id,
                        position=position,
                        text=item.text,
                        data=item.model_dump(),
                    )
                    s.add(record)
                else:
                    record.position, record.text, record.data = position, item.text, item.model_dump()
            thread.header = snapshot.header.model_dump()
            thread.checksum = snapshot.checksum
            thread.content_hash = digest([i.model_dump() for i in snapshot.items])
            thread.synced_at = iso()
            s.get(Device, device_id).last_seen = iso()
            return {"chat_id": thread.id, "checksum": thread.checksum, "changed": True}

    def snapshot(self, chat_id: str) -> Snapshot:
        with self.session() as s:
            thread = s.get(Thread, chat_id)
            if not thread:
                raise VaultError("Conversa não encontrada", 404)
            records = s.scalars(select(Record).where(Record.thread_id == chat_id).order_by(Record.position))
            return Snapshot(
                header=Header.model_validate(thread.header),
                items=[Item.model_validate(r.data) for r in records],
            )

    def projects(self) -> dict:
        with self.session() as s:
            projects = []
            for project in s.scalars(
                select(Project).where(Project.redirect_id.is_(None)).order_by(Project.name)
            ):
                threads = list(s.scalars(select(Thread).where(Thread.project_id == project.id)))
                unique = {(t.workspace, t.source_id, t.content_hash) for t in threads}
                projects.append(
                    {
                        "id": project.id,
                        "name": project.name,
                        "repository": project.repository,
                        "unique_chats": len(unique),
                        "copies": len(threads),
                    }
                )
        devices = self.devices()
        return {
            "projects": projects,
            "coverage": devices,
            "pending_devices": [
                d["name"] for d in devices if not d["revoked"] and (not d["last_sync"] or not d["online"])
            ],
        }

    def chats(self, project_id: str | None, offset: int = 0, limit: int = 50) -> dict:
        with self.session() as s:
            query = select(Thread, Device.name).join(Device)
            if project_id:
                if not s.get(Project, project_id):
                    raise VaultError("Projeto não encontrado", 404)
                query = query.where(Thread.project_id == project_id)
            rows = list(
                s.execute(query.order_by(Thread.synced_at.desc(), Thread.id).offset(offset).limit(limit + 1))
            )
            data = [
                {
                    "id": t.id,
                    "project_id": t.project_id,
                    "device": name,
                    "source_id": t.source_id,
                    "workspace": t.workspace,
                    "header": t.header,
                    "synced_at": t.synced_at,
                }
                for t, name in rows[:limit]
            ]
            return {"data": data, "next_offset": offset + limit if len(rows) > limit else None}

    def read(self, chat_id: str, offset: int = 0, limit: int = 50) -> dict:
        with self.session() as s:
            thread = s.get(Thread, chat_id)
            if not thread:
                raise VaultError("Conversa não encontrada", 404)
            device = s.get(Device, thread.device_id)
            records = list(
                s.scalars(
                    select(Record)
                    .where(Record.thread_id == chat_id)
                    .order_by(Record.position)
                    .offset(offset)
                    .limit(limit + 1)
                )
            )
            return {
                "chat_id": chat_id,
                "header": thread.header,
                "device": device.name,
                "content_is_untrusted": True,
                "items": [
                    {**r.data, "reference": f"vault://chat/{chat_id}#{r.source_id}"} for r in records[:limit]
                ],
                "next_offset": offset + limit if len(records) > limit else None,
            }

    def search(self, query: str, project_id: str | None, offset: int = 0, limit: int = 30) -> dict:
        words = query.split()
        if not words or len(query) > 500:
            raise VaultError("Informe uma busca entre 1 e 500 caracteres", 422)
        with self.session() as s:
            statement = (
                select(Record, Thread, Device.name)
                .select_from(Record)
                .join(Thread, Thread.id == Record.thread_id)
                .join(Device, Device.id == Thread.device_id)
            )
            if project_id:
                statement = statement.where(Thread.project_id == project_id)
            if self.engine.dialect.name == "postgresql":
                from sqlalchemy import func, literal_column

                config = literal_column("'simple'::regconfig")
                statement = statement.where(
                    func.to_tsvector(config, Record.text).op("@@")(func.plainto_tsquery(config, query))
                )
            else:
                for word in words:
                    statement = statement.where(Record.text.icontains(word, autoescape=True))
            rows = list(
                s.execute(
                    statement.order_by(Thread.synced_at.desc(), Record.position, Record.id)
                    .offset(offset)
                    .limit(limit + 1)
                )
            )
            return {
                "data": [
                    {
                        "chat_id": t.id,
                        "message_id": r.source_id,
                        "date": r.data.get("timestamp"),
                        "position": r.position,
                        "device": name,
                        "title": t.header["title"],
                        "excerpt": r.text[:1500],
                        "reference": f"vault://chat/{t.id}#{r.source_id}",
                    }
                    for r, t, name in rows[:limit]
                ],
                "next_offset": offset + limit if len(rows) > limit else None,
                "content_is_untrusted": True,
            }

    def merge_projects(self, sources: list[str], target: str, confirmed: bool) -> dict:
        if not confirmed:
            raise VaultError("O mapeamento de projetos exige confirmação explícita", 422)
        with self.session(write=True) as s:
            project_ids = set(sources) | {target}
            projects = {
                p.id: p
                for p in s.scalars(
                    select(Project).where(Project.id.in_(project_ids)).order_by(Project.id).with_for_update()
                )
            }
            destination = projects.get(target)
            if not destination or destination.redirect_id or set(projects) != project_ids:
                raise VaultError("Projeto não encontrado", 404)
            for source in set(sources) - {target}:
                s.execute(update(Thread).where(Thread.project_id == source).values(project_id=target))
                s.execute(
                    update(Project)
                    .where((Project.id == source) | (Project.redirect_id == source))
                    .values(redirect_id=target)
                )
            return {"target_id": target, "mapped_sources": sources}
