"""
Channel-bot voor MeshCore.

Bots worden door de admin gedefinieerd in de Web UI (Admin → Bots) en
opgeslagen in de DB-tabel `bots`. Elke bot heeft:
  - naam (label)
  - channel_idx (waar de bot luistert)
  - keyword (zonder ?-prefix; bot reageert op '?<keyword>')
  - reply (template-tekst, mag {VARS} bevatten)
  - enabled (toggle)

Variabelen in reply-templates:
  {TIME}    — lokale tijd op de gateway
  {UPRADIO} — uptime van de aangesloten companion-radio
  {UPNODE}  — uptime van de gateway-applicatie
  {HELP}    — overzicht van beschikbare variabelen

Gebruikt dezelfde dispatcher als de print/db/web-handlers.
"""

from __future__ import annotations

import time
from datetime import datetime
from typing import Awaitable, Callable, Optional

import db


# ---------------------------------------------------------------------------
# Module-state — gevuld door setup_bot()
# ---------------------------------------------------------------------------

_send_channel: Optional[Callable[[int, str], Awaitable[bool]]] = None
_mc = None                          # meshcore instance, voor get_stats_core
_started_at: float = time.time()    # uptime gateway-app
_self_pubkey: Optional[str] = None  # zelf-pubkey-prefix (om eigen msgs te negeren)
_gateway_state = None               # voor live self_name lookup

# Hoeveel seconden tussen dezelfde keyword/channel-trigger om spam te voorkomen
_REPLY_DEBOUNCE_S = 5.0
_recent_replies: dict[tuple[int, str], float] = {}

# Bot-cache: voorkomt een DB-roundtrip per inkomend channel-bericht.
# Wordt elke _BOTS_TTL_S seconden ververst. Admin-wijzigingen zijn dus
# uiterlijk na die periode actief — ruim snel genoeg voor bot-beheer.
_BOTS_TTL_S = 30.0
_bots_cache: list = []
_bots_cache_ts: float = 0.0


async def _get_bots():
    """Geef de enabled bots terug — uit cache, ververst elke _BOTS_TTL_S."""
    global _bots_cache, _bots_cache_ts
    now = time.time()
    if now - _bots_cache_ts > _BOTS_TTL_S:
        try:
            _bots_cache = await db.list_bots(only_enabled=True)
            _bots_cache_ts = now
        except Exception:  # noqa: BLE001
            pass  # bij fout: oude cache blijft staan
    return _bots_cache


# ---------------------------------------------------------------------------
# Variable-resolver
# ---------------------------------------------------------------------------

VARIABLES = ["{TIME}", "{UPRADIO}", "{UPNODE}", "{HELP}"]


def _fmt_uptime(secs) -> str:
    if not isinstance(secs, (int, float)) or secs < 0:
        return "?"
    secs = int(secs)
    d, rem = divmod(secs, 86400)
    h, rem = divmod(rem, 3600)
    m, s = divmod(rem, 60)
    if d: return f"{d}d {h}h {m}m"
    if h: return f"{h}h {m}m"
    if m: return f"{m}m {s}s"
    return f"{s}s"


async def _resolve_variables(template: str) -> str:
    """Vervang alle {VAR} placeholders in de template-string."""
    out = template

    if "{TIME}" in out:
        out = out.replace("{TIME}", datetime.now().strftime("%H:%M:%S"))

    if "{UPNODE}" in out:
        out = out.replace("{UPNODE}", _fmt_uptime(time.time() - _started_at))

    if "{UPRADIO}" in out:
        radio_uptime = "?"
        if _mc is not None:
            cmds = getattr(_mc, "commands", None)
            fn = getattr(cmds, "get_stats_core", None) if cmds else None
            if fn is not None:
                try:
                    import asyncio
                    ev = await asyncio.wait_for(fn(), timeout=2.0)
                    payload = getattr(ev, "payload", ev)
                    if isinstance(payload, dict):
                        radio_uptime = _fmt_uptime(payload.get("uptime_secs"))
                except Exception:  # noqa: BLE001
                    pass
        out = out.replace("{UPRADIO}", radio_uptime)

    if "{HELP}" in out:
        out = out.replace("{HELP}", ", ".join(VARIABLES))

    return out


# ---------------------------------------------------------------------------
# Dispatch handler — wordt voor élke channel/dm-msg aangeroepen
# ---------------------------------------------------------------------------

async def handle(msg) -> None:
    # Alleen inkomende channel-msgs (DM-bots zijn niet gevraagd in deze versie)
    if msg.direction != "in":
        return
    if msg.kind != "channel":
        return
    # Niet op eigen msgs reageren (dubbele veiligheidsslot)
    if msg.sender == "self":
        return
    if _self_pubkey and msg.sender == _self_pubkey:
        return

    text = (msg.text or "").strip()
    if not text:
        return

    # Knip eventuele "NAAM: " prefix er af (afzender heeft 'm vooropgezet)
    body = text
    colon = text.find(":")
    if 0 < colon < 64 and " " not in text[:colon]:
        body = text[colon + 1:].strip()

    # Eis dat de bot @-mentioned wordt aan de node-naam (of pubkey-prefix).
    # Anders reageert 'ie niet op willekeurig ?-verkeer in het kanaal.
    self_name = getattr(_gateway_state, "self_name", None) if _gateway_state else None
    body_lower = body.lower()
    mentioned = False
    if self_name and ("@[" + self_name.lower() + "]") in body_lower:
        mentioned = True
    elif _self_pubkey and ("@[" + _self_pubkey.lower() + "]") in body_lower:
        mentioned = True
    if not mentioned:
        return

    # Pak het keyword (eerste woord beginnend met '?')
    requested = None
    for tok in body.split():
        if tok.startswith("?"):
            requested = tok[1:].lower().rstrip("?,.!:")
            break
    if not requested:
        return

    # Vind matching bots (uit cache met TTL — geen DB-roundtrip per msg)
    bots = await _get_bots()
    if not bots:
        return

    now = time.time()
    for b in bots:
        if b.channel_idx != msg.channel_idx:
            continue
        if b.keyword.lower() != requested:
            continue

        # Debounce: max één reply per keyword/channel per N seconden
        key = (b.channel_idx, b.keyword.lower())
        last = _recent_replies.get(key, 0.0)
        if now - last < _REPLY_DEBOUNCE_S:
            continue
        _recent_replies[key] = now

        try:
            reply_text = await _resolve_variables(b.reply)
        except Exception as e:  # noqa: BLE001
            reply_text = f"err: {e}"

        if _send_channel is None:
            return
        try:
            await _send_channel(b.channel_idx, reply_text)
        except Exception as e:  # noqa: BLE001
            print(f"\r[bot:{b.name}] reply faalde: {e}\n> ", end="", flush=True)
        # Eén bot per keyword reageert; loop niet door
        return


# ---------------------------------------------------------------------------
# Setup
# ---------------------------------------------------------------------------

def setup_bot(*, mc, send_channel, gateway_started_at: float,
              self_pubkey: Optional[str] = None,
              gateway_state=None):
    """Initialiseer het bot-framework. Returnt de dispatch-handler."""
    global _mc, _send_channel, _started_at, _self_pubkey, _gateway_state
    _mc = mc
    _send_channel = send_channel
    _started_at = gateway_started_at
    _self_pubkey = self_pubkey
    _gateway_state = gateway_state
    return handle
