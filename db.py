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
# v8: + Message.expected_ack/ack_status/acked_at (ack-tracking voor outgoing)
# v9: + UserContact tabel (per-user opgeslagen contacten)
# v10: UserContact.pubkey is nu volledige 32-byte hex (64 chars) i.p.v. prefix.
#      Bestaande user_contacts tabel wordt gedropt en opnieuw aangemaakt.
# v11: + Bot tabel (admin-defined channel-bots met variable-templates)
SCHEMA_VERSION = "11"


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

    # Ack-tracking voor outgoing msgs:
    #   expected_ack = 4-byte token uit MSG_SENT (hex)
    #   ack_status   = 'sent' | 'acked' | 'failed' (NULL voor incoming)
    #   acked_at     = wanneer ACK ontvangen (NULL als nog niet)
    expected_ack: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)
    ack_status:   Mapped[Optional[str]] = mapped_column(String(8), nullable=True)
    acked_at:     Mapped[Optional[datetime]] = mapped_column(nullable=True)

    __table_args__ = (
        Index("ix_msg_channel_lookup", "kind", "channel_idx", "ts"),
        Index("ix_msg_peer_lookup", "kind", "peer", "ts"),
        Index("ix_msg_expected_ack", "expected_ack"),
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


class Bot(Base):
    """Een eenvoudige channel-bot: reageert op '?<keyword>' in een specifiek
    kanaal met een reply-template. Template kan variabelen bevatten zoals
    {TIME}, {UPNODE}, {UPRADIO}, {HELP} — bot.py vult die bij send."""

    __tablename__ = "bots"

    id:          Mapped[int] = mapped_column(primary_key=True)
    name:        Mapped[str] = mapped_column(String(64))
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    channel_idx: Mapped[int] = mapped_column(Integer)
    keyword:     Mapped[str] = mapped_column(String(64))   # zonder ?-prefix
    reply:       Mapped[str] = mapped_column(Text)
    enabled:     Mapped[bool] = mapped_column(default=True)
    created_at:  Mapped[datetime] = mapped_column(default=_utcnow)


class UserContact(Base):
    """Opgeslagen contactpersoon per web-user. Twee verschillende web-users
    delen geen contacten; elke user beheert zijn eigen lijst.

    pubkey = volledige 32-byte hex (64 hex chars) van de contact.
    Voor message-lookup gebruiken we de eerste 12 chars (matcht Message.peer).
    """

    __tablename__ = "user_contacts"

    username:   Mapped[str] = mapped_column(String(64), primary_key=True)
    pubkey:     Mapped[str] = mapped_column(String(64), primary_key=True)
    name:       Mapped[str] = mapped_column(String(64), default="")
    notes:      Mapped[Optional[str]] = mapped_column(Text, nullable=True)
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

        # v7 → v8: Message ack-tracking kolommen
        mcols_res = await conn.execute(text("PRAGMA table_info(messages)"))
        mcols = {r[1] for r in mcols_res.fetchall()}
        if mcols and "expected_ack" not in mcols:
            await conn.execute(text("ALTER TABLE messages ADD COLUMN expected_ack VARCHAR(16) DEFAULT NULL"))
            await conn.execute(text("ALTER TABLE messages ADD COLUMN ack_status VARCHAR(8) DEFAULT NULL"))
            await conn.execute(text("ALTER TABLE messages ADD COLUMN acked_at TIMESTAMP DEFAULT NULL"))

        # v9 → v10: user_contacts.pubkey_prefix → pubkey (vol 32-byte hex).
        # SQLite kan kolom niet hernoemen zonder migratie-overhead, dus
        # droppen + her-create maakt 'm opnieuw met de nieuwe kolomnaam.
        ucols_res = await conn.execute(text("PRAGMA table_info(user_contacts)"))
        ucols2 = {r[1] for r in ucols_res.fetchall()}
        if ucols2 and "pubkey_prefix" in ucols2 and "pubkey" not in ucols2:
            await conn.execute(text("DROP TABLE user_contacts"))

    # Nogmaals create_all om eventuele zojuist gedropte tabellen te herbouwen
    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

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
    expected_ack: Optional[str] = None,
    ack_status: Optional[str] = None,
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
        expected_ack=expected_ack,
        ack_status=ack_status,
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

async def channel_history(channel_idx: int, limit: int = 20,
                           before_id: Optional[int] = None) -> list[Message]:
    """Laatste `limit` msgs in dit kanaal, optioneel ouder dan `before_id`
    (voor paginatie met 'laad oudere berichten')."""
    Session = _require_session()
    async with Session() as s:
        stmt = select(Message).where(
            Message.kind == "channel", Message.channel_idx == channel_idx
        )
        if before_id is not None:
            stmt = stmt.where(Message.id < before_id)
        stmt = stmt.order_by(Message.ts.desc()).limit(limit)
        result = await s.execute(stmt)
        rows = list(result.scalars().all())
    return list(reversed(rows))  # oudste eerst


async def dm_history(peer_prefix: str, limit: int = 20,
                      before_id: Optional[int] = None) -> list[Message]:
    Session = _require_session()
    async with Session() as s:
        stmt = select(Message).where(
            Message.kind == "dm", Message.peer == peer_prefix
        )
        if before_id is not None:
            stmt = stmt.where(Message.id < before_id)
        stmt = stmt.order_by(Message.ts.desc()).limit(limit)
        result = await s.execute(stmt)
        rows = list(result.scalars().all())
    return list(reversed(rows))


async def search_messages(query: str, *, kind: Optional[str] = None,
                            channel_idx: Optional[int] = None,
                            peer: Optional[str] = None,
                            limit: int = 100) -> list[Message]:
    """Tekst-zoek in alle berichten (case-insensitive). Optioneel filter
    op kind/channel/peer. Returnt oudste-eerst, max `limit` rijen."""
    if not query or not query.strip():
        return []
    q = query.strip().lower()
    Session = _require_session()
    from sqlalchemy import func
    async with Session() as s:
        stmt = select(Message).where(func.lower(Message.text).like(f"%{q}%"))
        if kind is not None:
            stmt = stmt.where(Message.kind == kind)
        if channel_idx is not None:
            stmt = stmt.where(Message.channel_idx == channel_idx)
        if peer is not None:
            stmt = stmt.where(Message.peer == peer)
        stmt = stmt.order_by(Message.ts.desc()).limit(limit)
        result = await s.execute(stmt)
        rows = list(result.scalars().all())
    return list(reversed(rows))


async def dm_partners() -> list[str]:
    """Lijst van unieke peer-prefixes waar we DM's mee hebben gewisseld."""
    Session = _require_session()
    from sqlalchemy import distinct
    async with Session() as s:
        result = await s.execute(
            select(distinct(Message.peer)).where(
                Message.kind == "dm", Message.peer.isnot(None)
            )
        )
        return [r[0] for r in result.all() if r[0]]


async def count_messages() -> int:
    Session = _require_session()
    async with Session() as s:
        from sqlalchemy import func
        result = await s.execute(select(func.count()).select_from(Message))
        return int(result.scalar_one())


async def mark_message_acked(expected_ack: str) -> Optional[Message]:
    """Markeer een outgoing-msg als acked op basis van het 4-byte token.
    Returnt het bijgewerkte Message of None als niet gevonden."""
    if not expected_ack:
        return None
    Session = _require_session()
    async with Session() as s:
        result = await s.execute(
            select(Message)
            .where(Message.expected_ack == expected_ack, Message.ack_status != "acked")
            .order_by(Message.ts.desc())
            .limit(1)
        )
        row = result.scalar_one_or_none()
        if row is None:
            return None
        row.ack_status = "acked"
        row.acked_at = _utcnow()
        await s.commit()
        await s.refresh(row)
    return row


# ---------------------------------------------------------------------------
# Rapportages: simpele aggregaties voor de Reports-pagina
# ---------------------------------------------------------------------------

async def reports_overview(hours: float = 24.0) -> dict:
    """Overzicht: totalen, top-kanalen, ack-rate, msgs-per-bucket.

    hours = lengte van de periode voor de tijdgrafiek + ack-rate.
    De grafiek krijgt 24 buckets, ongeacht periode (bucket-grootte schaalt).
    """
    Session = _require_session()
    from sqlalchemy import func
    if hours <= 0:
        hours = 24.0
    now_ts = _utcnow().timestamp()
    period_secs = int(hours * 3600)
    cutoff_period = datetime.fromtimestamp(now_ts - period_secs, tz=timezone.utc)
    cutoff_24h    = datetime.fromtimestamp(now_ts - 86400, tz=timezone.utc)
    cutoff_7d     = datetime.fromtimestamp(now_ts - 7*86400, tz=timezone.utc)
    async with Session() as s:
        total      = (await s.execute(select(func.count()).select_from(Message))).scalar_one()
        last_24h   = (await s.execute(
            select(func.count()).select_from(Message).where(Message.ts >= cutoff_24h))).scalar_one()
        last_7d    = (await s.execute(
            select(func.count()).select_from(Message).where(Message.ts >= cutoff_7d))).scalar_one()

        # Top-5 kanalen op msg-volume in laatste 7 dagen
        top_chan_q = (
            select(Message.channel_idx, func.count().label("n"))
            .where(Message.kind == "channel", Message.ts >= cutoff_7d)
            .group_by(Message.channel_idx)
            .order_by(func.count().desc())
            .limit(5)
        )
        top_channels = [
            {"channel_idx": r[0], "count": int(r[1])}
            for r in (await s.execute(top_chan_q)).all()
        ]

        # Ack-rate (DM only; channels acken niet) over de periode
        out_total_q = select(func.count()).select_from(Message).where(
            Message.kind == "dm", Message.direction == "out", Message.ts >= cutoff_period)
        out_acked_q = select(func.count()).select_from(Message).where(
            Message.kind == "dm", Message.direction == "out", Message.ts >= cutoff_period,
            Message.ack_status == "acked")
        out_total = (await s.execute(out_total_q)).scalar_one()
        out_acked = (await s.execute(out_acked_q)).scalar_one()
        ack_rate = (out_acked / out_total) if out_total else None

        # Berichten-per-bucket
        ts_q = select(Message.ts).where(Message.ts >= cutoff_period)
        ts_rows = (await s.execute(ts_q)).scalars().all()

    # 24 buckets over de gekozen periode
    n_buckets = 24
    bucket_secs = period_secs / n_buckets
    buckets = [0] * n_buckets
    now = _utcnow()
    for ts in ts_rows:
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        delta_s = (now - ts).total_seconds()
        idx = int(delta_s // bucket_secs)
        if 0 <= idx < n_buckets:
            buckets[n_buckets - 1 - idx] += 1

    return {
        "totals": {"total": int(total), "last_24h": int(last_24h), "last_7d": int(last_7d)},
        "top_channels": top_channels,
        "ack_rate": round(ack_rate, 3) if ack_rate is not None else None,
        "ack_count": {"sent": int(out_total), "acked": int(out_acked)},
        "buckets": buckets,
        "period_hours": hours,
        "bucket_secs": int(bucket_secs),
        "n_buckets": n_buckets,
    }


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


# ---------------------------------------------------------------------------
# Bots
# ---------------------------------------------------------------------------

def _normalize_keyword(k: str) -> str:
    k = (k or "").strip().lower().lstrip("?")
    return k


async def list_bots(*, only_enabled: bool = False) -> list[Bot]:
    Session = _require_session()
    async with Session() as s:
        stmt = select(Bot)
        if only_enabled:
            stmt = stmt.where(Bot.enabled == True)  # noqa: E712
        stmt = stmt.order_by(Bot.channel_idx, Bot.keyword)
        result = await s.execute(stmt)
        return list(result.scalars().all())


async def get_bot(bot_id: int) -> Optional[Bot]:
    Session = _require_session()
    async with Session() as s:
        return await s.get(Bot, bot_id)


async def add_bot(*, name: str, channel_idx: int, keyword: str, reply: str,
                   description: Optional[str] = None,
                   enabled: bool = True) -> Bot:
    if not name or not name.strip():
        raise ValueError("name vereist")
    keyword = _normalize_keyword(keyword)
    if not keyword:
        raise ValueError("keyword vereist")
    if not reply or not reply.strip():
        raise ValueError("reply vereist")
    Session = _require_session()
    async with Session() as s:
        b = Bot(name=name.strip(), description=description,
                channel_idx=int(channel_idx),
                keyword=keyword, reply=reply,
                enabled=bool(enabled))
        s.add(b)
        await s.commit()
        await s.refresh(b)
    return b


async def update_bot(bot_id: int, **fields) -> bool:
    Session = _require_session()
    async with Session() as s:
        b = await s.get(Bot, bot_id)
        if b is None:
            return False
        if "name" in fields:        b.name = (fields["name"] or "").strip() or b.name
        if "description" in fields: b.description = fields["description"]
        if "channel_idx" in fields: b.channel_idx = int(fields["channel_idx"])
        if "keyword" in fields:
            kw = _normalize_keyword(fields["keyword"])
            if kw: b.keyword = kw
        if "reply" in fields and fields["reply"]:        b.reply = fields["reply"]
        if "enabled" in fields:     b.enabled = bool(fields["enabled"])
        await s.commit()
    return True


async def delete_bot(bot_id: int) -> bool:
    Session = _require_session()
    async with Session() as s:
        b = await s.get(Bot, bot_id)
        if b is None:
            return False
        await s.delete(b)
        await s.commit()
    return True


# ---------------------------------------------------------------------------
# User-contacts
# ---------------------------------------------------------------------------

async def list_user_contacts(username: str) -> list[UserContact]:
    Session = _require_session()
    async with Session() as s:
        result = await s.execute(
            select(UserContact)
            .where(UserContact.username == username)
            .order_by(UserContact.name)
        )
        return list(result.scalars().all())


async def add_user_contact(username: str, pubkey: str,
                            name: str, notes: Optional[str] = None) -> UserContact:
    """pubkey = volledige 32-byte hex (64 hex chars)."""
    Session = _require_session()
    pubkey = pubkey.strip().lower()
    if not pubkey:
        raise ValueError("pubkey vereist")
    if len(pubkey) != 64:
        raise ValueError(f"pubkey moet 64 hex chars (32 bytes) zijn, kreeg {len(pubkey)}")
    try:
        bytes.fromhex(pubkey)
    except ValueError:
        raise ValueError("pubkey moet hex zijn")
    async with Session() as s:
        existing = await s.get(UserContact, (username, pubkey))
        if existing is not None:
            existing.name = name
            if notes is not None:
                existing.notes = notes
            await s.commit()
            await s.refresh(existing)
            return existing
        c = UserContact(username=username, pubkey=pubkey, name=name, notes=notes)
        s.add(c)
        await s.commit()
        await s.refresh(c)
    return c


async def remove_user_contact(username: str, pubkey: str) -> bool:
    Session = _require_session()
    pubkey = pubkey.strip().lower()
    async with Session() as s:
        c = await s.get(UserContact, (username, pubkey))
        if c is None:
            return False
        await s.delete(c)
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
