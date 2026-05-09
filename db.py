"""
SQLite + SQLAlchemy 2.x async storage voor de MeshCore Gateway.

Eén tabel: messages. Slaat zowel inkomende als uitgaande berichten op,
voor channels en DM's, met timestamp en raw-payload voor diagnose.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from sqlalchemy import Index, Integer, String, Text, select, text
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


# Bump dit als er backwards-incompatible schema-wijzigingen komen.
# Code-versie wordt vergeleken met de versie in de Meta-tabel.
# v2: + Channel tabel
# v3: + Hashtag tabel (virtuele kanalen — filter op Public)  [DEPRECATED in v4]
# v4: Channel.kind kolom (public/hashtag/private). Hashtag is nu een echt
#     channel-slot met de standaard publieke PSK; geen DB-only filter meer.
# v5: + User tabel (multi-user + rollen + per-user toegestane views)
# v6: + User.must_change_password kolom (admin geeft tijdelijk ww, user moet wijzigen)
# v7: + Channel.scope kolom (per-kanaal flood-scope, '#regio' string of leeg)
SCHEMA_VERSION = "7"


# ---------------------------------------------------------------------------
# Model
# ---------------------------------------------------------------------------

class Base(DeclarativeBase):
    pass


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Message(Base):
    __tablename__ = "messages"

    id: Mapped[int] = mapped_column(primary_key=True)
    ts: Mapped[datetime] = mapped_column(default=_utcnow, index=True)

    direction: Mapped[str] = mapped_column(String(8))   # "in" | "out"
    kind: Mapped[str] = mapped_column(String(8))        # "channel" | "dm"

    # voor channels: index van het kanaal; voor dm: NULL
    channel_idx: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)

    # bij in-messages: pubkey_prefix van de zender
    # bij out-DM: pubkey_prefix van de bestemming
    # bij out-channel: 'self'
    peer: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)

    text: Mapped[str] = mapped_column(Text, default="")
    raw: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    __table_args__ = (
        Index("ix_msg_channel_lookup", "kind", "channel_idx", "ts"),
        Index("ix_msg_peer_lookup", "kind", "peer", "ts"),
    )

    def fmt(self) -> str:
        """Korte één-regel weergave voor in de CLI."""
        ts = self.ts.astimezone().strftime("%H:%M:%S")
        arrow = "→" if self.direction == "out" else "←"
        if self.kind == "channel":
            tag = f"CH{self.channel_idx if self.channel_idx is not None else '?'}"
        else:
            tag = f"DM {self.peer or '?'}"
        return f"{ts} {arrow} {tag:<10} {self.text}"


class Meta(Base):
    """Generieke key/value tabel voor meta-info (schema_version, last_clean_shutdown, …)."""

    __tablename__ = "meta"
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text, default="")


class Channel(Base):
    """Onze metadata over een kanaal-slot. De node blijft source-of-truth
    voor de echte naam en (private) sleutel; deze tabel voegt onze eigen
    aliassen / notities toe en cached de naam zodat we die kunnen tonen
    zonder per call de companion uit te lezen."""

    __tablename__ = "channels"

    idx: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(64), default="")
    alias: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    has_key: Mapped[bool] = mapped_column(default=False)
    is_public: Mapped[bool] = mapped_column(default=False)
    # 'public' = slot 0  |  'hashtag' = standaard publieke PSK  |  'private' = unieke key
    kind: Mapped[str] = mapped_column(String(16), default="private")
    # Optionele flood-scope; vóór send wordt set_flood_scope() aangeroepen.
    # Leeg/None = geen scope (default flood).
    scope: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(default=_utcnow)

    def display(self) -> str:
        nm = self.alias or self.name or f"channel-{self.idx}"
        if self.kind == "public":
            flag = "pub "
        elif self.kind == "hashtag":
            flag = "tag "
        elif self.kind == "private":
            flag = "priv"
        else:
            flag = "??? "
        return f"[{self.idx}] {flag} {nm}"


class Hashtag(Base):
    """Virtueel kanaal: een filter op de Public channel op basis van een
    tekst-tag (bv '#weer'). Geen overeenkomende set_channel-slot op de
    companion — alleen UI-categorisatie."""

    __tablename__ = "hashtags"

    name: Mapped[str] = mapped_column(String(64), primary_key=True)
    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(default=_utcnow)


class User(Base):
    """Web UI gebruiker. Wachtwoord-hash leeg = nog niet ingesteld
    (eerste-login flow). allowed_views is JSON-list met view-namen die
    de gebruiker mag zien (bv ["chat","bot"])."""

    __tablename__ = "users"

    username: Mapped[str] = mapped_column(String(64), primary_key=True)
    role: Mapped[str] = mapped_column(String(16), default="user")  # 'admin' | 'user'
    password_hash: Mapped[str] = mapped_column(Text, default="")
    allowed_views: Mapped[str] = mapped_column(Text, default='["chat"]')  # JSON
    # True = wachtwoord moet bij eerste login worden gewijzigd (tijdelijk ww van admin)
    must_change_password: Mapped[bool] = mapped_column(default=False)
    created_at: Mapped[datetime] = mapped_column(default=_utcnow)
    last_login: Mapped[Optional[datetime]] = mapped_column(nullable=True)


# ---------------------------------------------------------------------------
# Module-level state
# ---------------------------------------------------------------------------

_engine = None
_Session: Optional[async_sessionmaker[AsyncSession]] = None


# ---------------------------------------------------------------------------
# Init / lifecycle
# ---------------------------------------------------------------------------

class DBHealthReport:
    """Opbrengst van init_db — wat we hebben aangetroffen."""

    def __init__(
        self,
        *,
        path: Path,
        is_new: bool,
        schema_status: str,        # "ok" | "fresh" | "mismatch:<old>"
        integrity: str,            # "ok" | iets anders
        clean_shutdown: Optional[bool],  # None bij fresh DB
    ) -> None:
        self.path = path
        self.is_new = is_new
        self.schema_status = schema_status
        self.integrity = integrity
        self.clean_shutdown = clean_shutdown

    def is_healthy(self) -> bool:
        if self.integrity != "ok":
            return False
        if self.schema_status.startswith("mismatch"):
            return False
        return True

    def summary(self) -> str:
        bits = [
            f"path={self.path}",
            f"new={self.is_new}",
            f"schema={self.schema_status}",
            f"integrity={self.integrity}",
        ]
        if self.clean_shutdown is not None:
            bits.append(f"clean_shutdown={self.clean_shutdown}")
        return "  ".join(bits)


async def _set_meta(s: AsyncSession, key: str, value: str) -> None:
    row = await s.get(Meta, key)
    if row is None:
        s.add(Meta(key=key, value=value))
    else:
        row.value = value


async def _get_meta(s: AsyncSession, key: str) -> Optional[str]:
    row = await s.get(Meta, key)
    return row.value if row else None


async def init_db(path: str | Path = "meshcore.db") -> DBHealthReport:
    """Maak engine, session-maker, tabellen aan; voer health checks uit.

    Stappen:
      1. WAL-mode aanzetten (betere crash-bestendigheid)
      2. PRAGMA integrity_check
      3. Tabellen aanmaken (no-op als ze al bestaan)
      4. Schema-versie vergelijken; init bij fresh DB
      5. Detecteer of vorige run netjes is afgesloten
      6. Markeer huidige run als "running" (wordt op close_db op true gezet)
    """
    global _engine, _Session
    db_path = Path(path).expanduser().resolve()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    is_new = not db_path.exists()

    _engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}", future=True)
    _Session = async_sessionmaker(_engine, expire_on_commit=False)

    # 1) WAL-mode + 2) integrity check
    integrity = "?"
    async with _engine.begin() as conn:
        await conn.execute(text("PRAGMA journal_mode=WAL"))
        await conn.execute(text("PRAGMA foreign_keys=ON"))
        result = await conn.execute(text("PRAGMA integrity_check"))
        row = result.first()
        if row:
            integrity = str(row[0])

    # 3) tabellen
    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    # 3a) Kolom-migraties die create_all NIET regelt voor bestaande tabellen
    async with _engine.begin() as conn:
        # v3 → v4: voeg Channel.kind kolom toe + zet bestaande rijen op het
        # juiste type (public voor slot 0, anders private — beste gok).
        cols_res = await conn.execute(text("PRAGMA table_info(channels)"))
        cols = {r[1] for r in cols_res.fetchall()}
        if "kind" not in cols:
            await conn.execute(text(
                "ALTER TABLE channels ADD COLUMN kind VARCHAR(16) DEFAULT 'private'"
            ))
            await conn.execute(text(
                "UPDATE channels SET kind='public' WHERE is_public=1"
            ))

        # v5 → v6: User.must_change_password kolom
        ucols_res = await conn.execute(text("PRAGMA table_info(users)"))
        ucols = {r[1] for r in ucols_res.fetchall()}
        if ucols and "must_change_password" not in ucols:
            await conn.execute(text(
                "ALTER TABLE users ADD COLUMN must_change_password BOOLEAN DEFAULT 0"
            ))

        # v6 → v7: Channel.scope kolom
        cols_res2 = await conn.execute(text("PRAGMA table_info(channels)"))
        cols2 = {r[1] for r in cols_res2.fetchall()}
        if "scope" not in cols2:
            await conn.execute(text(
                "ALTER TABLE channels ADD COLUMN scope VARCHAR(64) DEFAULT NULL"
            ))

    # 4-6) schema + shutdown-flag
    async with _Session() as s:
        existing = await _get_meta(s, "schema_version")
        if existing is None:
            # Vers of pre-v1 — beide krijgen de huidige versie
            await _set_meta(s, "schema_version", SCHEMA_VERSION)
            schema_status = "fresh"
        elif existing == SCHEMA_VERSION:
            schema_status = "ok"
        else:
            # Forward-only "additive" migratie: nieuwe tabellen zijn al
            # door create_all aangemaakt, kolom-add hierboven, dus oude DB's
            # krijgen ze erbij zonder data te verliezen.
            schema_status = f"migrated:{existing}->{SCHEMA_VERSION}"
            await _set_meta(s, "schema_version", SCHEMA_VERSION)

        prev_state = await _get_meta(s, "shutdown_state")
        if prev_state is None:
            clean_shutdown: Optional[bool] = None
        else:
            clean_shutdown = (prev_state == "clean")

        # markeer huidige run
        await _set_meta(s, "shutdown_state", "running")
        await _set_meta(s, "last_startup", _utcnow().isoformat())
        await s.commit()

    return DBHealthReport(
        path=db_path,
        is_new=is_new,
        schema_status=schema_status,
        integrity=integrity,
        clean_shutdown=clean_shutdown,
    )


async def close_db() -> None:
    """Markeer een nette afsluiting en sluit de engine."""
    global _engine, _Session
    if _Session is not None:
        try:
            async with _Session() as s:
                await _set_meta(s, "shutdown_state", "clean")
                await _set_meta(s, "last_shutdown", _utcnow().isoformat())
                await s.commit()
        except Exception:  # noqa: BLE001
            pass  # best-effort
    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _Session = None


def _require_session() -> async_sessionmaker[AsyncSession]:
    if _Session is None:
        raise RuntimeError("db.init_db() is nog niet aangeroepen")
    return _Session


# ---------------------------------------------------------------------------
# Writes
# ---------------------------------------------------------------------------

async def save_message(
    *,
    direction: str,
    kind: str,
    text: str,
    channel_idx: Optional[int] = None,
    peer: Optional[str] = None,
    raw: Optional[Any] = None,
) -> Message:
    """Persisteer één bericht. `raw` mag elk JSON-serializable object zijn."""
    Session = _require_session()
    raw_json: Optional[str]
    if raw is None:
        raw_json = None
    elif isinstance(raw, (str, bytes)):
        raw_json = raw if isinstance(raw, str) else raw.decode("utf-8", errors="replace")
    else:
        try:
            raw_json = json.dumps(raw, default=str)
        except TypeError:
            raw_json = str(raw)

    msg = Message(
        direction=direction,
        kind=kind,
        text=text,
        channel_idx=channel_idx,
        peer=peer,
        raw=raw_json,
    )
    async with Session() as s:
        s.add(msg)
        await s.commit()
        # Refresh om id/ts in het object te krijgen voor de caller
        await s.refresh(msg)
    return msg


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------

async def channel_history(channel_idx: int, limit: int = 20) -> list[Message]:
    Session = _require_session()
    async with Session() as s:
        stmt = (
            select(Message)
            .where(Message.kind == "channel", Message.channel_idx == channel_idx)
            .order_by(Message.ts.desc())
            .limit(limit)
        )
        result = await s.execute(stmt)
        rows = list(result.scalars().all())
    return list(reversed(rows))  # oudste eerst


async def dm_history(peer_prefix: str, limit: int = 20) -> list[Message]:
    Session = _require_session()
    async with Session() as s:
        stmt = (
            select(Message)
            .where(Message.kind == "dm", Message.peer == peer_prefix)
            .order_by(Message.ts.desc())
            .limit(limit)
        )
        result = await s.execute(stmt)
        rows = list(result.scalars().all())
    return list(reversed(rows))


async def count_messages() -> int:
    Session = _require_session()
    async with Session() as s:
        from sqlalchemy import func
        result = await s.execute(select(func.count()).select_from(Message))
        return int(result.scalar_one())


# ---------------------------------------------------------------------------
# Channels
# ---------------------------------------------------------------------------

async def upsert_channel(
    idx: int,
    *,
    name: str,
    has_key: bool = False,
    is_public: bool = False,
    kind: str = "private",
    alias: Optional[str] = None,
    notes: Optional[str] = None,
) -> Channel:
    Session = _require_session()
    async with Session() as s:
        row = await s.get(Channel, idx)
        if row is None:
            row = Channel(
                idx=idx, name=name, has_key=has_key, is_public=is_public,
                kind=kind, alias=alias, notes=notes,
            )
            s.add(row)
        else:
            row.name = name
            row.has_key = has_key
            row.is_public = is_public
            row.kind = kind
            if alias is not None:
                row.alias = alias
            if notes is not None:
                row.notes = notes
        await s.commit()
        await s.refresh(row)
    return row


async def delete_channel(idx: int) -> bool:
    Session = _require_session()
    async with Session() as s:
        row = await s.get(Channel, idx)
        if row is None:
            return False
        await s.delete(row)
        await s.commit()
    return True


async def list_channels() -> list[Channel]:
    Session = _require_session()
    async with Session() as s:
        result = await s.execute(select(Channel).order_by(Channel.idx))
        return list(result.scalars().all())


async def channel_by_name_or_alias(query: str) -> Optional[Channel]:
    Session = _require_session()
    async with Session() as s:
        result = await s.execute(
            select(Channel).where((Channel.name == query) | (Channel.alias == query))
        )
        return result.scalar_one_or_none()


async def get_channel(idx: int) -> Optional[Channel]:
    Session = _require_session()
    async with Session() as s:
        return await s.get(Channel, idx)


async def set_channel_scope(idx: int, scope: Optional[str]) -> bool:
    """Set/clear de flood-scope voor een kanaal. None of '' wist 'm."""
    Session = _require_session()
    async with Session() as s:
        row = await s.get(Channel, idx)
        if row is None:
            return False
        row.scope = scope if scope else None
        await s.commit()
    return True


# ---------------------------------------------------------------------------
# Hashtags (virtuele kanalen)
# ---------------------------------------------------------------------------

def normalize_hashtag(name: str) -> str:
    """'#weer' / 'weer' / '  #Weer ' → '#weer'  (lowercased, '#'-prefix)."""
    s = (name or "").strip().lower()
    if not s:
        return ""
    if not s.startswith("#"):
        s = "#" + s
    return s


async def list_hashtags() -> list[Hashtag]:
    Session = _require_session()
    async with Session() as s:
        result = await s.execute(select(Hashtag).order_by(Hashtag.name))
        return list(result.scalars().all())


async def add_hashtag(name: str, notes: Optional[str] = None) -> Optional[Hashtag]:
    """Voeg toe of werk bij. Returnt het rijobject, of None bij lege naam."""
    norm = normalize_hashtag(name)
    if not norm:
        return None
    Session = _require_session()
    async with Session() as s:
        row = await s.get(Hashtag, norm)
        if row is None:
            row = Hashtag(name=norm, notes=notes)
            s.add(row)
        elif notes is not None:
            row.notes = notes
        await s.commit()
        await s.refresh(row)
    return row


async def delete_hashtag(name: str) -> bool:
    norm = normalize_hashtag(name)
    Session = _require_session()
    async with Session() as s:
        row = await s.get(Hashtag, norm)
        if row is None:
            return False
        await s.delete(row)
        await s.commit()
    return True


async def public_history_with_tag(tag: str, limit: int = 30) -> list[Message]:
    """Public-history gefilterd op berichten waarvan de tekst de tag bevat
    (case-insensitive). Tag wordt zo nodig genormaliseerd."""
    norm = normalize_hashtag(tag)
    if not norm:
        return []
    Session = _require_session()
    async with Session() as s:
        from sqlalchemy import func
        stmt = (
            select(Message)
            .where(
                Message.kind == "channel",
                Message.channel_idx == 0,
                func.lower(Message.text).like(f"%{norm}%"),
            )
            .order_by(Message.ts.desc())
            .limit(limit)
        )
        result = await s.execute(stmt)
        rows = list(result.scalars().all())
    return list(reversed(rows))


# ---------------------------------------------------------------------------
# Housekeeping
# ---------------------------------------------------------------------------

async def count_messages_older_than(seconds: int) -> int:
    """Aantal berichten ouder dan `seconds`."""
    Session = _require_session()
    cutoff = _utcnow().timestamp() - seconds
    async with Session() as s:
        from sqlalchemy import func
        # SQLite stores datetimes as strings; vergelijk via Python-side cutoff
        # door de hele tabel te scannen — voor onze schaal prima.
        result = await s.execute(select(Message))
        rows = result.scalars().all()
        return sum(1 for m in rows if m.ts.timestamp() < cutoff)


async def delete_messages_older_than(seconds: int) -> int:
    Session = _require_session()
    cutoff_dt = datetime.fromtimestamp(_utcnow().timestamp() - seconds, tz=timezone.utc)
    async with Session() as s:
        from sqlalchemy import delete
        result = await s.execute(delete(Message).where(Message.ts < cutoff_dt))
        await s.commit()
        return int(result.rowcount or 0)


async def delete_all_messages() -> int:
    Session = _require_session()
    async with Session() as s:
        from sqlalchemy import delete
        result = await s.execute(delete(Message))
        await s.commit()
        return int(result.rowcount or 0)


async def vacuum() -> None:
    """SQLite VACUUM — compacteert het bestand. Alleen buiten transacties."""
    if _engine is None:
        raise RuntimeError("db.init_db() is nog niet aangeroepen")
    async with _engine.connect() as conn:
        # VACUUM mag niet in een transactie. AUTOCOMMIT isolation garandeert dat.
        conn = await conn.execution_options(isolation_level="AUTOCOMMIT")
        await conn.execute(text("VACUUM"))


# ---------------------------------------------------------------------------
# Users
# ---------------------------------------------------------------------------

async def list_users() -> list[User]:
    Session = _require_session()
    async with Session() as s:
        result = await s.execute(select(User).order_by(User.username))
        return list(result.scalars().all())


async def count_users() -> int:
    Session = _require_session()
    async with Session() as s:
        from sqlalchemy import func
        r = await s.execute(select(func.count()).select_from(User))
        return int(r.scalar_one())


async def count_admins() -> int:
    Session = _require_session()
    async with Session() as s:
        from sqlalchemy import func
        r = await s.execute(
            select(func.count()).select_from(User).where(User.role == "admin")
        )
        return int(r.scalar_one())


async def get_user(username: str) -> Optional[User]:
    Session = _require_session()
    async with Session() as s:
        return await s.get(User, username)


async def add_user(username: str, role: str = "user",
                   password_hash: str = "",
                   must_change_password: bool = False,
                   allowed_views: Optional[list[str]] = None) -> User:
    """Maak gebruiker aan. Voor admin-CRUD: geef tijdelijk password_hash + must_change_password=True."""
    Session = _require_session()
    av = allowed_views if allowed_views is not None else ["chat"]
    async with Session() as s:
        existing = await s.get(User, username)
        if existing is not None:
            raise ValueError(f"user '{username}' bestaat al")
        u = User(
            username=username, role=role,
            password_hash=password_hash,
            must_change_password=must_change_password,
            allowed_views=json.dumps(av),
        )
        s.add(u)
        await s.commit()
        await s.refresh(u)
    return u


async def set_must_change_password(username: str, value: bool) -> bool:
    Session = _require_session()
    async with Session() as s:
        u = await s.get(User, username)
        if u is None:
            return False
        u.must_change_password = value
        await s.commit()
    return True


async def set_user_password_hash(username: str, password_hash: str,
                                  must_change_password: Optional[bool] = None) -> bool:
    Session = _require_session()
    async with Session() as s:
        u = await s.get(User, username)
        if u is None:
            return False
        u.password_hash = password_hash
        if must_change_password is not None:
            u.must_change_password = must_change_password
        await s.commit()
    return True


async def reset_user_password(username: str, password_hash: str) -> bool:
    """Admin-reset: zet tijdelijk wachtwoord en forceer wijziging bij volgende login."""
    return await set_user_password_hash(username, password_hash, must_change_password=True)


async def touch_user_login(username: str) -> None:
    Session = _require_session()
    async with Session() as s:
        u = await s.get(User, username)
        if u is not None:
            u.last_login = _utcnow()
            await s.commit()


async def delete_user(username: str) -> bool:
    Session = _require_session()
    async with Session() as s:
        u = await s.get(User, username)
        if u is None:
            return False
        await s.delete(u)
        await s.commit()
    return True


async def set_user_role(username: str, role: str) -> bool:
    Session = _require_session()
    async with Session() as s:
        u = await s.get(User, username)
        if u is None:
            return False
        u.role = role
        await s.commit()
    return True
