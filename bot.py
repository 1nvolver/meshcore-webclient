"""
Eenvoudige command-bot voor MeshCore.

Hoe het werkt:
  - Bot luistert op de dispatch-stream (zelfde bron als print/db/web).
  - Reageert op berichten die met `?` beginnen, gevolgd door een command.
  - BOT_CHANNELS env var bepaalt waar de bot luistert:
        BOT_CHANNELS=dm           → alleen private messages
        BOT_CHANNELS=dm,0         → DM + Public channel
        BOT_CHANNELS=dm,0,1,2     → DM + slots 0/1/2
    Niet gezet of leeg → bot is uit.

Veiligheid:
  - Reageert nooit op outgoing messages → geen self-loops.
  - Reageert nooit op messages waar `sender` 'self' is.
  - Bot stuurt antwoord via dezelfde send_and_dispatch flow, dus alles
    wordt netjes gelogd in de DB en zichtbaar in de Web UI.

Nieuwe commands toevoegen: gebruik @command("naam").
"""

from __future__ import annotations

import os
import time
from datetime import datetime
from typing import Awaitable, Callable, Optional


# Module-state — gevuld door setup_bot()
_send_channel: Optional[Callable[[int, str], Awaitable[bool]]] = None
_send_dm: Optional[Callable[[str, str], Awaitable[bool]]] = None
_gateway_state = None  # gateway.GatewayState
_started_at: float = time.time()

# Registry: command-naam → handler (async, msg → str|None)
_handlers: dict[str, Callable] = {}


# ---------------------------------------------------------------------------
# Decorator om commands te registreren
# ---------------------------------------------------------------------------

def command(name: str):
    def deco(fn):
        _handlers[name.lower()] = fn
        return fn
    return deco


# ---------------------------------------------------------------------------
# Built-in commands
# ---------------------------------------------------------------------------

@command("time")
async def cmd_time(msg) -> str:
    """?time → lokale tijd op de gateway."""
    return datetime.now().strftime("%H:%M:%S  %d-%m-%Y")


@command("who")
async def cmd_who(msg) -> str:
    """?who → naam van de gateway-node."""
    name = getattr(_gateway_state, "self_name", None) if _gateway_state else None
    return name or "unknown"


# ---------------------------------------------------------------------------
# Channel-permissie via env var
# ---------------------------------------------------------------------------

def _allowed_channels() -> set[str]:
    cfg = os.environ.get("BOT_CHANNELS", "")
    return {x.strip().lower() for x in cfg.split(",") if x.strip()}


def is_enabled() -> bool:
    return bool(_allowed_channels())


def _allows(msg) -> bool:
    allowed = _allowed_channels()
    if not allowed:
        return False
    if msg.kind == "dm":
        return "dm" in allowed
    if msg.kind == "channel":
        return str(msg.channel_idx) in allowed
    return False


# ---------------------------------------------------------------------------
# Dispatch handler
# ---------------------------------------------------------------------------

async def handle(msg) -> None:
    # Alleen inkomende, niet onze eigen echo
    if msg.direction != "in":
        return
    if msg.sender == "self":
        return
    if not _allows(msg):
        return

    text = (msg.text or "").strip()
    if not text.startswith("?"):
        return

    parts = text[1:].split(maxsplit=1)
    if not parts:
        return
    cmd_name = parts[0].lower()
    fn = _handlers.get(cmd_name)
    if fn is None:
        return  # geen onbekende-command spam

    try:
        reply = await fn(msg)
    except Exception as e:  # noqa: BLE001
        reply = f"err: {e}"
    if not reply:
        return

    # Antwoord terug naar dezelfde context
    try:
        if msg.kind == "dm" and msg.sender:
            assert _send_dm is not None
            await _send_dm(msg.sender, str(reply))
        elif msg.kind == "channel" and msg.channel_idx is not None:
            assert _send_channel is not None
            await _send_channel(msg.channel_idx, str(reply))
    except Exception as e:  # noqa: BLE001
        print(f"\r[bot] reply faalde: {e}\n> ", end="", flush=True)


# ---------------------------------------------------------------------------
# Setup
# ---------------------------------------------------------------------------

def setup_bot(*, send_channel, send_dm, gateway_state):
    """Bewaar send-helpers en gateway-state. Returnt de dispatch-handler."""
    global _send_channel, _send_dm, _gateway_state
    _send_channel = send_channel
    _send_dm = send_dm
    _gateway_state = gateway_state
    return handle
