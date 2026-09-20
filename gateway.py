#!/usr/bin/env python3
"""
MeshCore Gateway — Phase 1+2: connectiviteit + persistente historie.

Verbindt via USB-serial met een Seeed XIAO nRF52840 die de MeshCore
*companion* firmware draait. Toont status van de node, biedt een
simpele REPL om berichten te versturen / ontvangen, en slaat alle
verkeer op in een SQLite database (zie db.py).
"""

from __future__ import annotations

import asyncio
import os
import signal
import sys
from typing import Optional

from serial.tools import list_ports

# meshcore-py is async. De exacte module-paden / namen kunnen per
# versie iets verschillen — zie NOTES onderaan dit bestand als iets
# een ImportError of AttributeError geeft.
from meshcore import MeshCore  # type: ignore
try:
    from meshcore import EventType  # type: ignore
except ImportError:                 # oudere versies
    EventType = None  # type: ignore

import db


# Bekende USB Vendor-ID's voor Seeed XIAO nRF52840.
# 0x2886 = Seeed, 0x239A = Adafruit nRF52 bootloader (komt voor bij XIAO).
KNOWN_VIDS = {0x2886, 0x239A}

DEFAULT_BAUD = 115200
PUBLIC_CHANNEL_IDX = 0

# Watchdog: hoe vaak controleren we dat de companion nog reageert?
WATCHDOG_INTERVAL_S = 60.0
WATCHDOG_TIMEOUT_S = 5.0
WATCHDOG_FAIL_THRESHOLD = 3   # X mislukte heartbeats achter elkaar → alarm
# Na zoveel opeenvolgende mislukte heartbeats vraagt de watchdog een proces-restart
# aan: het is goedkoper en betrouwbaarder om main() af te sluiten met code != 0 en
# door systemd/docker (Restart=always / restart: unless-stopped) opnieuw te laten
# starten dan om `mc` in-place te re-initialiseren (dispatch/handlers/bots houden
# allemaal refs naar dezelfde instance). 5 × 60s ≈ 5 min stilte = restart.
WATCHDOG_HARD_FAIL_THRESHOLD = int(os.environ.get("MESHCORE_WATCHDOG_HARD_FAIL", "5"))


# Globale runtime-state. Klein houden; voor grote dingen → eigen module.
class GatewayState:
    self_pubkey: Optional[str] = None
    self_name: Optional[str] = None
    # Sentinel-string zodat 'last_scope is None' = bewust geen scope
    last_scope: object = "__unset__"
    # Companion-wide default flood-scope (None = geen default). Wordt bij
    # connect uit de companion gelezen via get_default_flood_scope en
    # geüpdate via /admin/radio/default-scope. _apply_channel_scope valt
    # hierop terug als een kanaal geen eigen scope heeft, zodat 't gedrag
    # deterministisch is ipv afhankelijk van firmware-interpretatie van
    # set_flood_scope(None). Per-kanaal scope overrulet deze default.
    default_scope: Optional[str] = None
    # True als watchdog een proces-restart heeft aangevraagd (USB stil >X min);
    # main() leest dit en exit met code 75 zodat supervisor (systemd/docker)
    # opnieuw start.
    watchdog_restart_requested: bool = False


state = GatewayState()


# ---------------------------------------------------------------------------
# Port discovery
# ---------------------------------------------------------------------------

def auto_detect_port() -> Optional[str]:
    """Vind het seriële pad van het MeshCore-device via USB VID/PID."""
    candidates = list(list_ports.comports())

    # 1) match op bekende VID
    for p in candidates:
        if p.vid in KNOWN_VIDS:
            return p.device

    # 2) heuristische fallback: eerste ttyACM* (Linux/Pi)
    for p in candidates:
        if p.device and "ACM" in p.device:
            return p.device

    return None


# ---------------------------------------------------------------------------
# Event handlers (incoming messages)
# ---------------------------------------------------------------------------

def _extract(event, *keys, default=""):
    """Pak een veld uit een event-object of dict, zonder aannames over de
    exacte API-shape (verschilt per meshcore-py versie)."""
    payload = getattr(event, "payload", event)
    if isinstance(payload, dict):
        for k in keys:
            if k in payload:
                return payload[k]
    return default


def _payload(event):
    return getattr(event, "payload", event)


# ---------------------------------------------------------------------------
# Normalised message envelope + dispatcher
# ---------------------------------------------------------------------------

class Message:
    """Stabiele shape voor zowel inkomende als uitgaande berichten —
    onafhankelijk van de exacte meshcore-py event payload.
    Dispatch-handlers werken hierop, dus print/db/web allemaal dezelfde flow."""

    __slots__ = ("direction", "kind", "channel_idx", "sender", "text", "raw",
                 "expected_ack", "ack_status", "db_id", "parent_id")

    def __init__(self, direction: str, kind: str, channel_idx, sender, text, raw=None,
                 expected_ack=None, ack_status=None, parent_id=None):
        self.direction = direction
        self.kind = kind
        self.channel_idx = channel_idx
        self.sender = sender
        self.text = text
        self.raw = raw
        self.expected_ack = expected_ack   # 4-byte hex token, alleen voor outgoing
        self.ack_status = ack_status       # 'sent' | 'acked' | None
        self.db_id = None                  # gevuld door db_handler na opslag
        self.parent_id = parent_id         # threading: id van msg waarop dit reply is


# Backward-compat alias voor bestaande imports
IncomingMessage = Message


class Dispatch:
    """Eenvoudige fan-out: registreer N handlers per message-kind, fire 'm
    één voor één. Een falende handler stopt de andere niet.

    Naast Message-events ook meta-updates (bv ack-status van een verzonden
    bericht) via register_update / fire_update."""

    def __init__(self):
        self._handlers: dict[str, list] = {"dm": [], "channel": []}
        self._update_handlers: list = []

    def register(self, kind: str, handler) -> None:
        self._handlers.setdefault(kind, []).append(handler)

    async def fire(self, msg: IncomingMessage) -> None:
        for h in list(self._handlers.get(msg.kind, [])):
            try:
                await h(msg)
            except Exception as e:  # noqa: BLE001
                name = getattr(h, "__name__", repr(h))
                print(f"\r[!] handler {name} faalde: {e}\n> ", end="", flush=True)

    def register_update(self, handler) -> None:
        self._update_handlers.append(handler)

    async def fire_update(self, payload: dict) -> None:
        for h in list(self._update_handlers):
            try:
                await h(payload)
            except Exception as e:  # noqa: BLE001
                name = getattr(h, "__name__", repr(h))
                print(f"\r[!] update handler {name} faalde: {e}\n> ", end="", flush=True)


# Singleton dispatcher voor deze proces-instantie
dispatch = Dispatch()


# Built-in handlers ---------------------------------------------------------

async def print_handler(msg: Message) -> None:
    arrow = "→" if msg.direction == "out" else " "
    if msg.kind == "channel":
        tag = f"CH{msg.channel_idx if msg.channel_idx is not None else '?'}"
    else:
        peer = msg.sender if msg.direction == "in" else (msg.sender or "?")
        tag = f"DM   {peer}"
    sender_str = f" {msg.sender}" if msg.kind == "channel" and msg.sender and msg.direction == "in" else ""
    sys.stdout.write(f"\r[{arrow}{tag}{sender_str}] {msg.text}\n> ")
    sys.stdout.flush()


async def db_handler(msg: Message) -> None:
    try:
        saved = await db.save_message(
            direction=msg.direction,
            kind=msg.kind,
            text=str(msg.text),
            channel_idx=msg.channel_idx,
            peer=msg.sender,
            raw=msg.raw,
            expected_ack=msg.expected_ack,
            ack_status=msg.ack_status,
            parent_id=getattr(msg, "parent_id", None),
        )
        msg.db_id = saved.id  # opdat web_handler de id kan meesturen
    except Exception as e:  # noqa: BLE001
        print(f"\r[!] db save ({msg.kind} {msg.direction}) faalde: {e}\n> ", end="", flush=True)


# Adapters: vertalen meshcore events naar IncomingMessage en fire'n -------

async def on_contact_msg(event):
    # txt_type != 0 = CLI/cmd response e.d. — hou die buiten de DM-historie.
    txt_type = _extract(event, "txt_type", default=0)
    if isinstance(txt_type, int) and txt_type != 0:
        return
    sender = _extract(event, "pubkey_prefix", "from", "src", default=None)
    text = _extract(event, "text", "msg", "message", default="")
    msg = Message(
        direction="in",
        kind="dm",
        channel_idx=None,
        sender=str(sender) if sender else None,
        text=str(text),
        raw=_payload(event),
    )
    await dispatch.fire(msg)


async def on_channel_msg(event):
    chan = _extract(event, "channel_idx", "channel", default=None)
    sender = _extract(event, "pubkey_prefix", "from", default=None)
    text = _extract(event, "text", "msg", "message", default="")
    if isinstance(chan, str) and chan.isdigit():
        idx = int(chan)
    elif isinstance(chan, int):
        idx = chan
    else:
        idx = None

    # Enrich payload met path/RSSI/SNR uit RX_LOG_DATA ring-buffer.
    raw_payload = _payload(event)
    if isinstance(raw_payload, dict):
        enriched = dict(raw_payload)
        _enrich_with_rxlog(enriched)
    else:
        enriched = raw_payload

    msg = Message(
        direction="in",
        kind="channel",
        channel_idx=idx,
        sender=str(sender) if sender and sender != "?" else None,
        text=str(text),
        raw=enriched,
    )
    await dispatch.fire(msg)


# ---------------------------------------------------------------------------
# CLI loop
# ---------------------------------------------------------------------------

HELP = """\
Berichten:
  <tekst>                  stuur naar Public channel
  /dm <prefix> <tekst>     directe boodschap (pubkey-prefix, hex)
  /history [n]             laatste n berichten van Public

Status:
  /info                    node-info
  /bat                     batterijstatus
  /poll                    handmatig msgs ophalen

Web-gebruikers:
  /users                   toon web-gebruikers
  /reset-admin             wis admin-accounts (eerste /setup opnieuw)

Algemeen:
  /help                    deze help
  /quit                    stoppen

Tip: alle admin/channels/housekeeping zit in de Web UI.
"""


async def _ainput(prompt: str = "> ") -> str:
    """Async wrapper rond input() via een executor."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, input, prompt)


async def print_status(mc: "MeshCore") -> None:
    # Probeer de gangbare commando-namen — niet allemaal werken op
    # iedere versie; we vangen AttributeError op.
    cmds = getattr(mc, "commands", None)
    if cmds is None:
        print("  (geen commands-interface beschikbaar — node niet (meer) verbonden)")
        return

    try:
        info = await cmds.send_appstart()
        print(f"  app-start: {info}")
    except AttributeError:
        pass
    except Exception as e:  # noqa: BLE001
        print(f"  app-start faalde: {e}")

    for getter in ("get_self_info", "get_node_info", "get_self"):
        fn = getattr(cmds, getter, None)
        if fn:
            try:
                print(f"  self-info ({getter}): {await fn()}")
            except Exception as e:  # noqa: BLE001
                print(f"  self-info ({getter}) faalde: {e}")
            break

    fn = getattr(cmds, "get_bat", None) or getattr(cmds, "get_battery", None)
    if fn:
        try:
            print(f"  batterij: {await fn()}")
        except Exception as e:  # noqa: BLE001
            print(f"  batterij faalde: {e}")


async def send_channel(mc, idx: int, text: str):
    fn = (
        getattr(mc.commands, "send_chan_msg", None)
        or getattr(mc.commands, "send_channel_msg", None)
        or getattr(mc.commands, "send_channel_message", None)
    )
    if not fn:
        raise RuntimeError("Geen send_chan_msg-achtige methode gevonden in meshcore.commands")
    return await fn(idx, text)


async def send_dm(mc, prefix: str, text: str):
    fn = (
        getattr(mc.commands, "send_msg", None)
        or getattr(mc.commands, "send_message", None)
        or getattr(mc.commands, "send_dm", None)
    )
    if not fn:
        raise RuntimeError("Geen send_msg-achtige methode gevonden in meshcore.commands")
    return await fn(prefix, text)


async def _apply_channel_scope(mc, channel_idx: int) -> None:
    """Lees scope uit DB voor het kanaal en pas 'm toe via set_flood_scope.
    Cache de laatst-gezet scope om onnodige USB-traffic te voorkomen.

    Fallback (v1.1.045): als het kanaal géén eigen scope heeft, valt 't terug
    op state.default_scope (de companion-wide default). Daardoor is 't gedrag
    voorspelbaar — channel scope overrulet default, geen channel scope =
    default geldt — onafhankelijk van hoe de firmware set_flood_scope(None)
    interpreteert.
    """
    try:
        ch = await db.get_channel(channel_idx)
    except Exception:  # noqa: BLE001
        return
    ch_scope = (ch.scope if ch and ch.scope else None)
    desired = ch_scope if ch_scope else getattr(state, "default_scope", None)
    if getattr(state, "last_scope", "__unset__") == desired:
        return  # niet veranderd, niets doen
    fn = getattr(getattr(mc, "commands", None), "set_flood_scope", None)
    if not callable(fn):
        return
    try:
        await fn(desired)
        state.last_scope = desired
    except Exception as e:  # noqa: BLE001
        print(f"\r[!] set_flood_scope({desired!r}) faalde: {e}\n> ", end="", flush=True)


async def send_and_dispatch(mc, *, kind: str, text: str,
                            channel_idx: Optional[int] = None,
                            peer: Optional[str] = None,
                            parent_id: Optional[int] = None) -> bool:
    """Verstuur via meshcore + fire 'out' Message naar dispatcher.

    Eén centrale plek voor zowel CLI als web. Returnt True bij succes.
    De dispatcher zorgt dan voor print, DB-save en web-broadcast.
    """
    try:
        if kind == "channel":
            assert channel_idx is not None
            await _apply_channel_scope(mc, channel_idx)
            res = await send_channel(mc, channel_idx, text)
            sender_label = "self"
        elif kind == "dm":
            assert peer is not None
            res = await send_dm(mc, peer, text)
            sender_label = peer
        else:
            raise ValueError(f"Onbekende kind: {kind}")
    except Exception as e:  # noqa: BLE001
        print(f"\r[!] verzenden mislukt: {e}\n> ", end="", flush=True)
        return False

    # Pak expected_ack-token uit MSG_SENT response (4 bytes hex)
    expected_ack = None
    try:
        payload = getattr(res, "payload", None)
        if isinstance(payload, dict) and "expected_ack" in payload:
            ea = payload["expected_ack"]
            expected_ack = ea.hex() if isinstance(ea, (bytes, bytearray)) else str(ea)
    except Exception:  # noqa: BLE001
        pass

    msg = Message(
        direction="out",
        kind=kind,
        channel_idx=channel_idx,
        sender=sender_label,
        text=text,
        raw=str(res),
        expected_ack=expected_ack,
        ack_status=("sent" if expected_ack else "sent"),
        parent_id=parent_id,
    )
    await dispatch.fire(msg)
    # Voor channel-sends: registreer voor implicit-ACK detectie via RX_LOG
    if kind == "channel" and msg.db_id is not None:
        _pending_repeats.append({
            "db_id": msg.db_id,
            "ts": _time.time(),
            "channel_idx": channel_idx,
        })
    return True


DEBUG_EVENTS = os.environ.get("MESHCORE_DEBUG", "").lower() in ("1", "true", "yes")


async def _on_any_event(event):
    """Debug-handler: print elk event dat binnenkomt (alleen in debug-modus)."""
    name = getattr(getattr(event, "type", None), "name", "?")
    payload = getattr(event, "payload", event)
    print(f"\r[ev {name}] {payload}\n> ", end="", flush=True)


# ---- RX_LOG_DATA enrichment ----
# meshcore-py's decrypt_channels-koppeling werkt niet (zoekt op msg_hash dat
# niet in channels_log gepushed wordt). We doen 't zelf via een ring-buffer
# van recente RX_LOG_DATA entries. Bij elke channel-msg pakken we de beste
# match (laagste path_len, binnen 10s).

import time as _time
from collections import deque as _deque

_recent_rxlogs: _deque = _deque(maxlen=200)

# Tweede ring-buffer: alle RX_LOG_DATA entries (ongeacht payload_typename).
# Gebruikt door de "ping repeater"-endpoint om SNR-here best-effort op te
# pikken voor STATUS_RESPONSE packets (die niet als GRP_TXT binnenkomen).
_recent_rxlogs_all: _deque = _deque(maxlen=200)


def get_recent_rxlogs_all() -> list:
    """Snapshot van de all-payload ring-buffer (oudste eerst)."""
    return list(_recent_rxlogs_all)

# Pending outgoing channel-msgs voor implicit-ACK detectie.
# Channel-msgs hebben geen protocol-ack; we detecteren 'mesh-pickup' door
# tijd-correlatie met inkomende RX_LOG_DATA (GRP_TXT) van repeaters.
# Element: {"db_id": int, "ts": float, "channel_idx": int}
_pending_repeats: list = []


# Lookup van pubkey-prefix → adv_name voor repeater-naam-resolving in
# path-visualisatie. Wordt periodiek bijgewerkt vanuit mc.contacts.
# Sleutels worden voor meerdere prefix-lengtes (2/4/6/8 hex chars = 1/2/3/4 bytes)
# opgeslagen zodat path_hash_size 1-4 allemaal werken.
_known_repeaters: dict[str, str] = {}


async def refresh_contacts(mc, prune: bool = True) -> dict:
    """Haal de contactenlijst opnieuw op bij de companion.

    LET OP — dit bestaat omdat `mc.contacts` alleen maar GROEIT. De SDK
    (`MeshCore._update_contacts`) doet per binnengekomen contact:

        if pk in self._contacts: self._contacts[pk].update(c)
        else:                    self._contacts[pk] = c

    Er wordt dus nooit iets verwijderd. Haal je een contact van de companion
    af, dan stuurt het apparaat 'm daarna niet meer mee — maar de oude entry
    blijft in de dict staan, en elke `get_contacts()` laat 'm gewoon staan.
    Gevolg (bug t/m v1.1.056): verwijderde repeaters kwamen bij de volgende
    schermwissel weer tevoorschijn alsof er niets gebeurd was.

    `get_contacts()` geeft het afsluitende CONTACTS-event terug, en de payload
    daarvan IS de volledige lijst zoals de companion 'm zojuist opsomde. Die
    gebruiken we als bron van waarheid om verdwenen sleutels te prunen.

    Alleen prunen bij een echt CONTACTS-event: een time-out levert een
    ERROR-event en dan mag je vooral niets weggooien.
    """
    out = {"ok": False, "pruned": 0, "total": None, "error": None}
    cmds = getattr(mc, "commands", None)
    fn = getattr(cmds, "get_contacts", None) if cmds is not None else None
    if not callable(fn):
        out["error"] = "get_contacts niet beschikbaar"
        return out
    try:
        ev = await asyncio.wait_for(fn(), timeout=8.0)
    except Exception as e:  # noqa: BLE001
        out["error"] = f"get_contacts faalde: {e}"
        return out
    if ev is None:
        out["error"] = "geen antwoord (timeout)"
        return out
    ev_type = getattr(ev, "type", None)
    ev_type_str = getattr(ev_type, "value", str(ev_type))
    out["ok"] = True
    contacts = getattr(mc, "contacts", None)
    if not isinstance(contacts, dict):
        return out
    out["total"] = len(contacts)
    if not prune:
        return out
    if ev_type_str != "contacts":
        # ERROR of iets onverwachts: lijst is mogelijk incompleet → niet prunen.
        out["error"] = f"niet gepruned; onverwacht event: {ev_type_str}"
        return out
    payload = getattr(ev, "payload", None)
    if not isinstance(payload, dict):
        out["error"] = "niet gepruned; CONTACTS-payload was geen dict"
        return out
    fresh = set(payload.keys())
    stale = [k for k in list(contacts.keys()) if k not in fresh]
    for k in stale:
        contacts.pop(k, None)
    out["pruned"] = len(stale)
    out["total"] = len(contacts)
    if stale:
        print(f"[*] contacten-cache: {len(stale)} verdwenen contact(en) opgeruimd "
              f"({out['total']} over)")
    return out


def forget_contact_locally(mc, pubkey: str) -> bool:
    """Gooi één contact uit de lokale cache, zonder de companion te bevragen.

    Gebruikt direct na een bevestigde `remove_contact`: dan klopt het scherm
    meteen, ook als de daaropvolgende refresh faalt.
    """
    contacts = getattr(mc, "contacts", None)
    if not isinstance(contacts, dict) or not pubkey:
        return False
    pk = pubkey.lower()
    for key in list(contacts.keys()):
        if isinstance(key, str) and key.lower() == pk:
            contacts.pop(key, None)
            return True
    return False


async def refresh_repeater_cache(mc) -> None:
    """Refresh _known_repeaters uit mc.contacts.
    Roept get_contacts() aan om de cache up-to-date te houden."""
    cmds = getattr(mc, "commands", None)
    if cmds is None:
        return
    # v1.1.057: via refresh_contacts() zodat verdwenen contacten óók uit
    # mc.contacts verdwijnen — een kale get_contacts() laat ze staan.
    try:
        await refresh_contacts(mc, prune=True)
    except Exception:  # noqa: BLE001
        pass  # cache blijft oude waardes — beter dan crash
    contacts = getattr(mc, "contacts", None)
    if not isinstance(contacts, dict):
        return
    new_lookup: dict[str, str] = {}
    for pk_hex, c in contacts.items():
        if not isinstance(pk_hex, str) or not pk_hex:
            continue
        name = c.get("adv_name") if isinstance(c, dict) else None
        if not name:
            continue
        for hex_chars in (2, 4, 6, 8):  # 1, 2, 3, 4 byte prefixes
            if len(pk_hex) >= hex_chars:
                key = pk_hex[:hex_chars].lower()
                # Conflict resolution: kortere prefix kan dubbelen tussen
                # verschillende repeaters; bij collision behoudt de laatste
                # wins. Voor 1-byte prefixes is dat onvermijdelijk.
                new_lookup[key] = str(name)
    _known_repeaters.clear()
    # v1.1.051: vervángen, niet mergen. Met .update() bleven prefixes van
    # verwijderde of hernoemde contacten voor altijd in de lookup staan, zodat
    # pad-visualisatie namen toonde van nodes die niet meer bestaan.
    _known_repeaters.clear()
    _known_repeaters.update(new_lookup)


# ===================== Companion-klok (v1.1.054) =====================
# `last_advert` en andere tijdstempels worden door de COMPANION gestempeld,
# maar alle leeftijdsberekeningen in de app gebruiken onze eigen klok. Loopt
# de companion uit de pas (RTC-reset na een firmware-upgrade, lege buffer-cap
# na stroomloos staan), dan klopt er niets meer van: adverts kunnen zelfs in
# de toekomst liggen. Zie CHANGELOG v1.1.053.
#
# Daarom: bij connect en daarna periodiek de klok controleren en alleen
# bijstellen als de afwijking boven de drempel komt. Niet élke ronde blind
# schrijven — dat is nodeloos USB-verkeer en flash-slijtage.

TIME_SYNC_ENABLED = os.environ.get("MESHCORE_TIME_SYNC", "1") not in ("0", "false", "no")
TIME_SYNC_INTERVAL_SECS = int(os.environ.get("MESHCORE_TIME_SYNC_INTERVAL", str(6 * 3600)))
TIME_SYNC_THRESHOLD_SECS = int(os.environ.get("MESHCORE_TIME_SYNC_THRESHOLD", "30"))

# Ondergrens-sanity: als ONZE klok overduidelijk niet gezet is (container zonder
# RTC/NTP), mogen we 'm niet naar de radio schrijven — dan maken we het erger.
# 2026-01-01; ruim vóór de bouwdatum van deze code, ruim ná elke plausibele
# "klok staat op epoch 0"-situatie.
HOST_CLOCK_SANITY_EPOCH = 1767225600

# Laatste meting/sync, zodat de web-UI kan tonen wat de loop gedaan heeft.
_last_time_sync: dict = {
    "checked_at": None, "skew_secs": None, "synced_at": None,
    "last_result": None, "error": None,
}


def last_time_sync() -> dict:
    """Snapshot van wat de sync-loop het laatst gedaan heeft (voor de web-UI)."""
    return dict(_last_time_sync)


async def read_companion_clock(mc) -> dict:
    """Lees de companion-klok en zet 'm af tegen de onze.

    Returnt {ok, companion_epoch, host_epoch, skew_secs, error}.
    skew_secs > 0 betekent: de companion loopt vóór op ons.
    """
    out = {"ok": False, "companion_epoch": None, "host_epoch": int(_time.time()),
           "skew_secs": None, "error": None}
    cmds = getattr(mc, "commands", None)
    fn = getattr(cmds, "get_time", None) if cmds is not None else None
    if not callable(fn):
        out["error"] = "get_time niet beschikbaar in deze SDK-versie"
        return out
    try:
        ev = await asyncio.wait_for(fn(), timeout=5.0)
    except Exception as e:  # noqa: BLE001
        out["error"] = f"uitlezen mislukt: {e}"
        return out
    if ev is None:
        out["error"] = "geen antwoord van de companion (timeout)"
        return out
    payload = getattr(ev, "payload", None)
    epoch = None
    if isinstance(payload, dict):
        for k in ("time", "epoch", "timestamp", "current_time"):
            v = payload.get(k)
            if isinstance(v, (int, float)) and v > 0:
                epoch = int(v)
                break
    elif isinstance(payload, (int, float)):
        epoch = int(payload)
    if epoch is None:
        out["error"] = f"onverwacht antwoord: {payload!r}"
        return out
    now = _time.time()
    out.update({"ok": True, "companion_epoch": epoch, "host_epoch": int(now),
                "skew_secs": round(epoch - now, 1)})
    return out


async def set_companion_clock(mc) -> dict:
    """Zet de companion-klok op onze tijd. Returnt {ok, error, before, after}."""
    before = await read_companion_clock(mc)
    now = _time.time()
    if now < HOST_CLOCK_SANITY_EPOCH:
        return {"ok": False, "before": before, "after": None,
                "error": ("de klok van de gateway zelf lijkt niet gezet "
                          f"({int(now)}) — weigeren om die naar de radio te schrijven")}
    cmds = getattr(mc, "commands", None)
    fn = getattr(cmds, "set_time", None) if cmds is not None else None
    if not callable(fn):
        return {"ok": False, "before": before, "after": None,
                "error": "set_time niet beschikbaar in deze SDK-versie"}
    try:
        ev = await asyncio.wait_for(fn(int(now)), timeout=5.0)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "before": before, "after": None, "error": f"set_time faalde: {e}"}
    if ev is None:
        return {"ok": False, "before": before, "after": None,
                "error": "geen antwoord van de companion (timeout)"}
    ev_type = getattr(ev, "type", None)
    ev_type_str = getattr(ev_type, "value", str(ev_type))
    if ev_type_str != "command_ok":
        return {"ok": False, "before": before, "after": None,
                "error": f"companion weigerde set_time ({ev_type_str})"}
    after = await read_companion_clock(mc)
    return {"ok": True, "before": before, "after": after, "error": None}


async def check_and_sync_clock(mc, *, force: bool = False) -> dict:
    """Eén controle-ronde. Stelt alleen bij als |skew| > drempel (of force)."""
    res = await read_companion_clock(mc)
    _last_time_sync["checked_at"] = int(_time.time())
    _last_time_sync["skew_secs"] = res.get("skew_secs")
    _last_time_sync["error"] = res.get("error")
    if not res.get("ok"):
        _last_time_sync["last_result"] = "leesfout"
        return {"checked": res, "synced": None}
    skew = res.get("skew_secs") or 0
    if not force and abs(skew) <= TIME_SYNC_THRESHOLD_SECS:
        _last_time_sync["last_result"] = "binnen drempel"
        return {"checked": res, "synced": None}
    sync = await set_companion_clock(mc)
    if sync.get("ok"):
        _last_time_sync["synced_at"] = int(_time.time())
        _last_time_sync["last_result"] = f"bijgesteld ({skew:+.0f}s)"
        rest = (sync.get("after") or {}).get("skew_secs")
        print(f"[*] companion-klok bijgesteld: afwijking was {skew:+.0f}s, nu {rest}s")
    else:
        _last_time_sync["last_result"] = "sync mislukt"
        _last_time_sync["error"] = sync.get("error")
        print(f"[!] companion-klok bijstellen mislukt: {sync.get('error')}")
    return {"checked": res, "synced": sync}


async def time_sync_loop(mc, stop: asyncio.Event) -> None:
    """Bij start en daarna elke TIME_SYNC_INTERVAL_SECS de klok controleren."""
    if not TIME_SYNC_ENABLED:
        print("[*] companion-kloksync uit (MESHCORE_TIME_SYNC=0)")
        return
    try:
        first = await check_and_sync_clock(mc)
        chk = first.get("checked") or {}
        if chk.get("ok"):
            print(f"[*] companion-klok: afwijking {chk.get('skew_secs')}s "
                  f"(drempel {TIME_SYNC_THRESHOLD_SECS}s, interval "
                  f"{TIME_SYNC_INTERVAL_SECS//3600}h)")
        else:
            print(f"[!] companion-klok niet uitleesbaar: {chk.get('error')}")
    except Exception as e:  # noqa: BLE001
        print(f"[!] kloksync-start mislukt: {e}")
    while not stop.is_set():
        try:
            await asyncio.wait_for(stop.wait(), timeout=float(TIME_SYNC_INTERVAL_SECS))
            return
        except asyncio.TimeoutError:
            pass
        try:
            await check_and_sync_clock(mc)
        except Exception:  # noqa: BLE001
            pass


async def repeater_cache_loop(mc, stop: asyncio.Event) -> None:
    """Periodiek (elke 5 min) de repeater-cache verversen."""
    await refresh_repeater_cache(mc)
    print(f"[*] repeater-cache: {len(_known_repeaters)} prefixes geladen")
    while not stop.is_set():
        try:
            await asyncio.wait_for(stop.wait(), timeout=300.0)
            return
        except asyncio.TimeoutError:
            pass
        try:
            await refresh_repeater_cache(mc)
        except Exception:  # noqa: BLE001
            pass


def _resolve_path_hashes(path_hex: str, hash_size: int) -> list[dict]:
    """Split een path-hex string in hops en resolve elke hop naar
    {'hash': '6c', 'name': 'NL-020-Involver/RPT2'} (name=None als onbekend)."""
    if not isinstance(path_hex, str) or not path_hex:
        return []
    hash_size = hash_size if isinstance(hash_size, int) and hash_size > 0 else 1
    n = hash_size * 2  # hex chars per hop
    out = []
    for i in range(0, len(path_hex), n):
        seg = path_hex[i:i + n].lower()
        if len(seg) != n:
            break
        out.append({"hash": seg, "name": _known_repeaters.get(seg)})
    return out


async def on_rx_log_event(event):
    payload = getattr(event, "payload", None)
    if not isinstance(payload, dict):
        return
    now = _time.time()
    # All-payload buffer (gebruikt door /admin/repeaters/ping voor SNR-here)
    _recent_rxlogs_all.append({
        "ts":              now,
        "payload_typename": payload.get("payload_typename"),
        "rssi":            payload.get("rssi"),
        "snr":             payload.get("snr"),
        "path_len":        payload.get("path_len"),
    })
    # Alleen GRP_TXT-payload-type heeft betekenis voor channel-msg-enrichment
    if payload.get("payload_typename") != "GRP_TXT":
        return
    _recent_rxlogs.append({
        "ts": now,
        "pkt_hash":       payload.get("pkt_hash"),
        "chan_hash":      payload.get("chan_hash"),
        "path":           payload.get("path"),
        "path_len":       payload.get("path_len"),
        "path_hash_size": payload.get("path_hash_size"),
        "rssi":           payload.get("rssi"),
        "snr":            payload.get("snr"),
    })

    # Implicit-ACK voor outgoing channel-msgs: een RX_LOG met path_len > 0
    # betekent dat een repeater het bericht doorgaf. Match op tijd met onze
    # pending outgoing-msgs (binnen 60s, pak oudste).
    pl = payload.get("path_len")
    if not isinstance(pl, int) or pl == 0 or pl == 255:
        return
    # Cleanup te oude pending-entries
    cutoff = now - 60.0
    while _pending_repeats and _pending_repeats[0]["ts"] < cutoff:
        _pending_repeats.pop(0)
    if not _pending_repeats:
        return
    pending = _pending_repeats.pop(0)
    try:
        msg = await db.mark_message_acked  # placeholder; we doen 't manueel
    except Exception:
        msg = None
    # Update direct via db.set_user_password_hash-stijl helper:
    try:
        ok = await _mark_repeated(pending["db_id"])
        if ok:
            await dispatch.fire_update({
                "type": "repeated",
                "msg_id": pending["db_id"],
                "ack_status": "repeated",
                "via_path_len": pl,
                "snr": payload.get("snr"),
                "rssi": payload.get("rssi"),
            })
    except Exception:
        pass


async def _mark_repeated(msg_id: int) -> bool:
    """Markeer een outgoing-channel-msg als 'opgepikt door mesh-repeater'."""
    Session = db._require_session()
    async with Session() as s:
        m = await s.get(db.Message, msg_id)
        if m is None:
            return False
        if m.ack_status == "acked":  # ack heeft prioriteit
            return False
        m.ack_status = "repeated"
        await s.commit()
    return True


def _enrich_with_rxlog(payload: dict) -> None:
    """Voeg path/RSSI/SNR/paths toe aan een channel-msg payload.

    Een bericht kan via meerdere paden binnenkomen (de Android-app toont
    "Heard X Times"). We groeperen op `pkt_hash` om alle ontvangsten
    van hetzelfde bericht te clusteren. De meest recente entry geeft ons
    de pkt_hash van de net-binnengekomen msg.
    """
    if not isinstance(payload, dict):
        return
    now = _time.time()
    recent = [r for r in _recent_rxlogs if now - r["ts"] <= 15.0]
    if not recent:
        return

    # Aanname: meest recente entry hoort bij deze net-gedecodeerde msg
    latest = recent[-1]
    target = latest.get("pkt_hash")
    if target is None:
        same = [latest]
    else:
        same = [r for r in recent if r.get("pkt_hash") == target]

    # Sla alle paden op (voor multi-path detail in UI), incl. naam-resolution
    paths_out = []
    for r in same:
        p = {
            "path":           r.get("path"),
            "path_len":       r.get("path_len"),
            "path_hash_size": r.get("path_hash_size"),
            "rssi":           r.get("rssi"),
            "snr":            r.get("snr"),
            "ts":             r.get("ts"),
        }
        p["path_names"] = _resolve_path_hashes(p["path"] or "", p["path_hash_size"] or 1)
        paths_out.append(p)
    payload["paths"] = paths_out

    # Primary indicator: kortste pad eerst, daarbinnen sterkste SNR
    def _score(r):
        pl = r.get("path_len") if isinstance(r.get("path_len"), int) else 999
        sn = r.get("snr")      if isinstance(r.get("snr"),  (int, float)) else -999
        return (pl, -sn)

    best = min(same, key=_score)
    if best.get("path") is not None:           payload["path"] = best["path"]
    if best.get("path_len") is not None:       payload["path_len"] = best["path_len"]
    if best.get("path_hash_size") is not None: payload["path_hash_size"] = best["path_hash_size"]
    if best.get("rssi") is not None:           payload["RSSI"] = best["rssi"]
    if best.get("snr") is not None:            payload["SNR"]  = best["snr"]


async def on_ack_event(event):
    """Wordt aangeroepen bij elke binnenkomende ACK; matched de 4-byte
    code aan een eerder verzonden msg via Message.expected_ack."""
    payload = getattr(event, "payload", None)
    if not isinstance(payload, dict):
        return
    code = payload.get("code")
    if isinstance(code, (bytes, bytearray)):
        code = code.hex()
    if not code:
        return
    msg = await db.mark_message_acked(str(code))
    if msg is None:
        return
    # Latency: ts is UTC, acked_at idem
    ts = msg.ts
    if ts.tzinfo is None:
        from datetime import timezone as _tz
        ts = ts.replace(tzinfo=_tz.utc)
    acked = msg.acked_at
    if acked.tzinfo is None:
        from datetime import timezone as _tz
        acked = acked.replace(tzinfo=_tz.utc)
    latency_s = (acked - ts).total_seconds()
    await dispatch.fire_update({
        "type": "ack",
        "msg_id": msg.id,
        "ack_status": "acked",
        "acked_at": acked.isoformat(),
        "latency_s": round(latency_s, 2),
        "expected_ack": code,
    })


def subscribe_messages(mc) -> None:
    """Hang event handlers aan inkomende DM- en kanaal-berichten + ACK."""
    if EventType is None:
        print("[!] EventType niet beschikbaar in deze meshcore versie; "
              "inkomende berichten worden mogelijk niet getoond.")
        return

    candidates = [
        ("CONTACT_MSG_RECV", on_contact_msg),
        ("CHANNEL_MSG_RECV", on_channel_msg),
        ("MSG_RECV", on_contact_msg),
        ("CHANNEL_MSG", on_channel_msg),
        ("ACK", on_ack_event),
        ("RX_LOG_DATA", on_rx_log_event),
    ]
    seen = set()
    for name, handler in candidates:
        et = getattr(EventType, name, None)
        if et and et not in seen:
            try:
                mc.subscribe(et, handler)
                seen.add(et)
            except Exception as e:  # noqa: BLE001
                print(f"[!] subscribe({name}) faalde: {e}")

    # Debug: subscribe op ALLE event-types zodat we kunnen zien welke namen
    # de companion echt vuurt. Activeer met MESHCORE_DEBUG=1. We subscriben
    # bewust óók op events die al een specifieke handler hebben — geen
    # interferentie, beide handlers worden los aangeroepen.
    if DEBUG_EVENTS:
        try:
            n = 0
            for et in list(EventType):  # type: ignore[arg-type]
                try:
                    mc.subscribe(et, _on_any_event)
                    n += 1
                except Exception:  # noqa: BLE001
                    pass
            print(f"[debug] _on_any_event op {n} EventType-leden gehookt")
        except TypeError:
            pass


async def start_message_pump(mc) -> bool:
    """Zet de auto-fetcher aan zodat inkomende berichten daadwerkelijk events vuren.

    Zonder dit haalt meshcore-py inkomende DM/CHANNEL-berichten niet uit de
    companion en blijven CONTACT_MSG_RECV / CHANNEL_MSG_RECV stil.
    """
    fn = (
        getattr(mc, "start_auto_message_fetching", None)
        or getattr(mc, "auto_message_fetching", None)
    )
    if fn is None:
        print("[!] geen start_auto_message_fetching in deze meshcore versie — "
              "gebruik /poll om handmatig op te halen.")
        return False
    try:
        await fn()
        return True
    except Exception as e:  # noqa: BLE001
        print(f"[!] start_auto_message_fetching faalde: {e}")
        return False


async def manual_poll(mc) -> None:
    """Handmatig één keer get_msg aanroepen (fallback / diagnose)."""
    cmds = getattr(mc, "commands", None)
    fn = getattr(cmds, "get_msg", None) if cmds else None
    if fn is None:
        print("[!] geen get_msg-methode gevonden")
        return
    try:
        res = await fn()
        print(f"[poll] {res}")
    except Exception as e:  # noqa: BLE001
        print(f"[!] get_msg faalde: {e}")


# ---------------------------------------------------------------------------
# Self-info & watchdog
# ---------------------------------------------------------------------------

def _dig(obj, *keys, default=None):
    """Diep-zoeken naar key in genest dict / object."""
    if obj is None:
        return default
    if hasattr(obj, "payload"):
        obj = obj.payload
    if isinstance(obj, dict):
        for k in keys:
            if k in obj:
                return obj[k]
        # try nested 'data' / 'info' wrappers
        for wrapper in ("data", "info", "self_info"):
            if wrapper in obj:
                got = _dig(obj[wrapper], *keys, default=None)
                if got is not None:
                    return got
    return default


async def load_self_info(mc) -> None:
    """Vul state.self_pubkey / self_name uit get_self_info()."""
    cmds = getattr(mc, "commands", None)
    if cmds is None:
        return
    fn = (
        getattr(cmds, "get_self_info", None)
        or getattr(cmds, "send_appstart", None)
    )
    if fn is None:
        return
    try:
        info = await asyncio.wait_for(fn(), timeout=3.0)
    except Exception as e:  # noqa: BLE001
        print(f"[!] self-info ophalen faalde: {e}")
        return

    pk = _dig(info, "pubkey", "public_key", "pub_key", "node_id")
    name = _dig(info, "name", "node_name", "adv_name")
    if pk:
        # Bewaar prefix (eerste 12 hex tekens) zodat het matcht met wat
        # andere kanten in de mesh zien als 'pubkey_prefix'.
        pk_str = pk.hex() if isinstance(pk, (bytes, bytearray)) else str(pk)
        state.self_pubkey = pk_str[:12]
    if name:
        state.self_name = str(name)
    if state.self_pubkey or state.self_name:
        print(f"[*] self: name={state.self_name!r} pubkey_prefix={state.self_pubkey}")


async def load_default_scope(mc) -> None:
    """Vul state.default_scope vanuit de companion. Best-effort: bij niet-
    ondersteunde firmware of timeout blijft default_scope op None staan."""
    cmds = getattr(mc, "commands", None)
    if cmds is None:
        return
    fn = getattr(cmds, "get_default_flood_scope", None)
    if not callable(fn):
        return  # SDK/firmware zonder default-scope support
    try:
        ev = await asyncio.wait_for(fn(), timeout=2.0)
    except Exception as e:  # noqa: BLE001
        print(f"[!] get_default_flood_scope faalde: {e}")
        return
    payload = getattr(ev, "payload", ev) if not isinstance(ev, dict) else ev
    name = (payload or {}).get("scope_name") or ""
    name = name.strip() or None
    state.default_scope = name
    if name:
        print(f"[*] default flood-scope: {name}")


async def watchdog(mc, stop: asyncio.Event) -> None:
    """Periodiek pingen zodat we merken als de companion stilvalt."""
    cmds = getattr(mc, "commands", None)
    if cmds is None:
        return
    probe = (
        getattr(cmds, "get_self_info", None)
        or getattr(cmds, "send_appstart", None)
    )
    if probe is None:
        return

    failures = 0
    alarmed = False
    while not stop.is_set():
        try:
            await asyncio.wait_for(stop.wait(), timeout=WATCHDOG_INTERVAL_S)
            return  # stop gevraagd
        except asyncio.TimeoutError:
            pass  # tijd voor de volgende ping

        try:
            await asyncio.wait_for(probe(), timeout=WATCHDOG_TIMEOUT_S)
            if alarmed:
                print("\r[watchdog] companion antwoordt weer.\n> ", end="", flush=True)
            failures = 0
            alarmed = False
        except Exception as e:  # noqa: BLE001
            failures += 1
            if failures >= WATCHDOG_FAIL_THRESHOLD and not alarmed:
                print(
                    f"\r[watchdog] companion reageert {failures}x niet "
                    f"({type(e).__name__}: {e}). Mogelijk USB-disconnect of firmware-hang.\n> ",
                    end="",
                    flush=True,
                )
                alarmed = True
            if failures >= WATCHDOG_HARD_FAIL_THRESHOLD:
                mins = (WATCHDOG_HARD_FAIL_THRESHOLD * WATCHDOG_INTERVAL_S) / 60
                print(
                    f"\r[watchdog] companion {mins:.0f} min stil — proces-restart aangevraagd "
                    f"(supervisor moet ons opnieuw starten).\n> ",
                    end="",
                    flush=True,
                )
                state.watchdog_restart_requested = True
                stop.set()
                return


# ---------------------------------------------------------------------------
# Admin / settings / channels / housekeeping
# ---------------------------------------------------------------------------

import secrets


async def confirm(prompt: str) -> bool:
    """Inline y/N prompt voor destructieve acties."""
    ans = (await _ainput(f"{prompt} [y/N] ")).strip().lower()
    return ans in ("y", "yes", "ja", "j")


async def _current_radio(mc) -> dict:
    """Lees actuele radio-instellingen uit self_info. Lege dict bij falen."""
    cmds = getattr(mc, "commands", None)
    if cmds is None:
        return {}
    fn = getattr(cmds, "get_self_info", None) or getattr(cmds, "send_appstart", None)
    if fn is None:
        return {}
    try:
        info = await asyncio.wait_for(fn(), timeout=3.0)
    except Exception:  # noqa: BLE001
        return {}
    payload = getattr(info, "payload", info)
    if not isinstance(payload, dict):
        return {}
    return {
        "freq": payload.get("radio_freq"),
        "bw": payload.get("radio_bw"),
        "sf": payload.get("radio_sf"),
        "cr": payload.get("radio_cr"),
        "tx_power": payload.get("tx_power"),
        "max_tx_power": payload.get("max_tx_power"),
        "name": payload.get("name"),
        "lat": payload.get("adv_lat"),
        "lon": payload.get("adv_lon"),
        "adv_loc_policy": payload.get("adv_loc_policy"),
    }


def _resolve(cmds, *names):
    """Vind de eerste bestaande commando-methode met één van deze namen."""
    for n in names:
        fn = getattr(cmds, n, None)
        if callable(fn):
            return fn, n
    return None, None


# ---- Radio --------------------------------------------------------------

async def cmd_radio(mc, args: list[str]) -> None:
    """Dispatch /radio sub-commando's."""
    if not args or args[0] in ("show", "info", ""):
        cur = await _current_radio(mc)
        if not cur:
            print("[!] kon radio-instellingen niet uitlezen")
            return
        print(
            f"  freq={cur['freq']} MHz  bw={cur['bw']} kHz  "
            f"sf={cur['sf']}  cr={cur['cr']}  "
            f"tx_power={cur['tx_power']}/{cur['max_tx_power']} dBm"
        )
        return

    sub = args[0]
    rest = args[1:]
    cmds = getattr(mc, "commands", None)
    if cmds is None:
        print("[!] geen commands-interface")
        return

    if sub == "set" and len(rest) == 4:
        try:
            freq, bw = float(rest[0]), float(rest[1])
            sf, cr = int(rest[2]), int(rest[3])
        except ValueError:
            print("Gebruik: /radio set <freq_mhz> <bw_khz> <sf> <cr>")
            return
        await _apply_radio(mc, freq, bw, sf, cr)
        return

    if sub in ("freq", "bw", "sf", "cr") and len(rest) == 1:
        cur = await _current_radio(mc)
        if not cur:
            print("[!] huidige radio niet leesbaar — kan niet partial wijzigen")
            return
        try:
            if sub == "freq":
                cur["freq"] = float(rest[0])
            elif sub == "bw":
                cur["bw"] = float(rest[0])
            else:
                cur[sub] = int(rest[0])
        except ValueError:
            print(f"Ongeldige waarde voor {sub}: {rest[0]}")
            return
        await _apply_radio(mc, cur["freq"], cur["bw"], cur["sf"], cur["cr"])
        return

    if sub in ("tx", "txpower", "tx_power") and len(rest) == 1:
        try:
            val = int(rest[0])
        except ValueError:
            print("Gebruik: /radio txpower <dBm>")
            return
        fn, _ = _resolve(cmds, "set_tx_power", "set_txpower")
        if fn is None:
            print("[!] set_tx_power niet beschikbaar in deze meshcore versie")
            return
        try:
            res = await fn(val)
            print(f"[ok] tx_power → {val} dBm  ({res})")
            print("    Reboot vereist om actief te worden — gebruik /reboot")
        except Exception as e:  # noqa: BLE001
            print(f"[!] set_tx_power faalde: {e}")
        return

    print("Gebruik: /radio show | set <f> <bw> <sf> <cr> | freq <v> | bw <v> | sf <v> | cr <v> | txpower <v>")


async def _apply_radio(mc, freq: float, bw: float, sf: int, cr: int) -> None:
    fn, _ = _resolve(mc.commands, "set_radio", "set_radio_params")
    if fn is None:
        print("[!] set_radio niet beschikbaar in deze meshcore versie")
        return
    if not await confirm(f"Radio → freq={freq} bw={bw} sf={sf} cr={cr}. Doorgaan?"):
        print("Geannuleerd.")
        return
    try:
        res = await fn(freq, bw, sf, cr)
        print(f"[ok] set_radio → {res}")
        print("    Reboot vereist om actief te worden — gebruik /reboot")
    except Exception as e:  # noqa: BLE001
        print(f"[!] set_radio faalde: {e}")


# ---- Naam, coords, advertise -------------------------------------------

async def cmd_set_name(mc, name: str) -> None:
    fn, _ = _resolve(mc.commands, "set_name", "set_node_name")
    if fn is None:
        print("[!] set_name niet beschikbaar")
        return
    if not await confirm(f"Naam wijzigen in {name!r}?"):
        print("Geannuleerd.")
        return
    try:
        res = await fn(name)
        print(f"[ok] naam → {name}  ({res})")
        state.self_name = name
    except Exception as e:  # noqa: BLE001
        print(f"[!] set_name faalde: {e}")


async def cmd_set_coords(mc, args: list[str]) -> None:
    if not args or args == ["show"]:
        cur = await _current_radio(mc)
        print(f"  lat={cur.get('lat')}  lon={cur.get('lon')}  policy={cur.get('adv_loc_policy')}")
        return
    if args == ["clear"]:
        lat, lon = 0.0, 0.0
    elif len(args) == 2:
        try:
            lat, lon = float(args[0]), float(args[1])
        except ValueError:
            print("Gebruik: /coords <lat> <lon>")
            return
    else:
        print("Gebruik: /coords show | clear | <lat> <lon>")
        return

    fn, _ = _resolve(mc.commands, "set_coords", "set_node_coords")
    if fn is None:
        print("[!] set_coords niet beschikbaar")
        return
    try:
        res = await fn(lat, lon)
        print(f"[ok] coords → {lat},{lon}  ({res})")
    except Exception as e:  # noqa: BLE001
        print(f"[!] set_coords faalde: {e}")


async def cmd_advert_policy(mc, args: list[str]) -> None:
    """Set advertise/location policy. Ondersteund afhankelijk van firmware."""
    if not args:
        cur = await _current_radio(mc)
        print(f"  adv_loc_policy={cur.get('adv_loc_policy')}")
        print("  Gebruik: /advert-policy <0|1|2|3>  (firmware-afhankelijk)")
        return
    try:
        val = int(args[0])
    except ValueError:
        print("Waarde moet 0-3 zijn (firmware-afhankelijk)")
        return
    fn, n = _resolve(mc.commands, "set_advert_loc_policy", "set_adv_loc_policy", "set_advert_policy")
    if fn is None:
        print("[!] set_advert_loc_policy niet beschikbaar in deze meshcore versie")
        print("    (mogelijk alleen via firmware-CLI; check `dir(mc.commands)`)")
        return
    try:
        res = await fn(val)
        print(f"[ok] {n} → {val}  ({res})")
    except Exception as e:  # noqa: BLE001
        print(f"[!] {n} faalde: {e}")


# ---- Reboot --------------------------------------------------------------

async def cmd_reboot(mc) -> None:
    fn, _ = _resolve(mc.commands, "reboot", "reset")
    if fn is None:
        print("[!] reboot niet beschikbaar")
        return
    if not await confirm("Companion reboot — verbinding gaat tijdelijk weg. Doorgaan?"):
        print("Geannuleerd.")
        return
    try:
        res = await fn()
        print(f"[ok] reboot gestuurd ({res}). Wacht ~10s en herstart deze gateway als de verbinding wegvalt.")
    except Exception as e:  # noqa: BLE001
        print(f"[!] reboot faalde: {e}")


# ---- Channels ------------------------------------------------------------

async def cmd_channels(mc, args: list[str]) -> None:
    if not args or args[0] in ("show", "list"):
        rows = await db.list_channels()
        if not rows:
            print("  (geen channels in DB — gebruik /channels add ...)")
            print(f"  N.B. node-side channel 0 is de Public channel.")
            return
        for c in rows:
            print(f"  {c.display()}")
        return

    if args[0] == "add":
        # /channels add <slot> <naam> [hex-key]
        if len(args) < 3:
            print("Gebruik: /channels add <slot 1-7> <naam> [hex-key]")
            return
        try:
            slot = int(args[1])
        except ValueError:
            print("slot moet 1-7 zijn")
            return
        if slot < 1 or slot > 7:
            print("[!] slot 0 is gereserveerd voor Public; gebruik 1-7 voor private channels")
            return
        name = args[2]
        if len(args) >= 4:
            try:
                key = bytes.fromhex(args[3])
            except ValueError:
                print("[!] hex-key ongeldig")
                return
            if len(key) != 16:
                print(f"[!] key moet 16 bytes zijn (32 hex chars), kreeg {len(key)}")
                return
            generated = False
        else:
            key = secrets.token_bytes(16)
            generated = True

        fn, _ = _resolve(mc.commands, "set_channel", "add_channel")
        if fn is None:
            print("[!] set_channel niet beschikbaar")
            return
        try:
            res = await fn(slot, name, key)
            await db.upsert_channel(slot, name=name, has_key=True, is_public=False)
            print(f"[ok] channel slot {slot} = {name!r} ({res})")
            if generated:
                print(f"    GEGENEREERDE KEY (delen met andere leden): {key.hex()}")
        except Exception as e:  # noqa: BLE001
            print(f"[!] set_channel faalde: {e}")
        return

    if args[0] in ("remove", "rm", "delete"):
        if len(args) < 2:
            print("Gebruik: /channels remove <slot>")
            return
        try:
            slot = int(args[1])
        except ValueError:
            print("slot moet een getal zijn")
            return
        if slot == 0:
            print("[!] Public channel (slot 0) kan niet verwijderd worden")
            return
        if not await confirm(f"Channel slot {slot} verwijderen uit DB-metadata?"):
            print("Geannuleerd.")
            return
        ok = await db.delete_channel(slot)
        print("[ok] verwijderd uit DB" if ok else "[!] niet gevonden")
        print("    N.B. dit verwijdert alleen onze metadata. De slot op de node "
              "blijft bestaan; overschrijf 'm met /channels add om dat ook leeg te maken.")
        return

    if args[0] == "alias":
        # /channels alias <slot> <alias>
        if len(args) < 3:
            print("Gebruik: /channels alias <slot> <alias>")
            return
        try:
            slot = int(args[1])
        except ValueError:
            print("slot moet getal zijn")
            return
        alias = " ".join(args[2:])
        rows = await db.list_channels()
        existing = next((c for c in rows if c.idx == slot), None)
        if existing is None:
            print(f"[!] slot {slot} niet bekend; voeg eerst toe met /channels add")
            return
        await db.upsert_channel(slot, name=existing.name, has_key=existing.has_key,
                                is_public=existing.is_public, alias=alias)
        print(f"[ok] alias slot {slot} → {alias!r}")
        return

    print("Gebruik: /channels [show] | add <slot> <naam> [key] | remove <slot> | alias <slot> <alias>")


# ---- Housekeeping --------------------------------------------------------

def _parse_age(s: str) -> Optional[int]:
    """'30d' → seconden; 'all' → None. None bij parse-fout."""
    s = s.strip().lower()
    if s == "all":
        return -1  # sentinel
    try:
        n = int(s[:-1])
        unit = s[-1]
    except (ValueError, IndexError):
        return None
    multipliers = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 86400 * 7}
    if unit not in multipliers:
        return None
    return n * multipliers[unit]


async def cmd_clean(args: list[str]) -> None:
    if not args:
        print("Gebruik: /clean older-than <30d|24h|...> | all")
        return
    if args[0] == "all":
        total = await db.count_messages()
        if not await confirm(f"ALLE {total} berichten verwijderen?"):
            print("Geannuleerd.")
            return
        n = await db.delete_all_messages()
        print(f"[ok] {n} berichten verwijderd.")
        return
    if args[0] == "older-than" and len(args) >= 2:
        secs = _parse_age(args[1])
        if secs is None or secs < 0:
            print("Gebruik: /clean older-than <Nd|Nh|Nm>  (bv 30d, 24h)")
            return
        count = await db.count_messages_older_than(secs)
        if count == 0:
            print("Geen berichten ouder dan dat.")
            return
        if not await confirm(f"{count} berichten ouder dan {args[1]} verwijderen?"):
            print("Geannuleerd.")
            return
        n = await db.delete_messages_older_than(secs)
        print(f"[ok] {n} berichten verwijderd.")
        return
    print("Gebruik: /clean older-than <30d|24h|...> | all")


async def cmd_vacuum() -> None:
    if not await confirm("VACUUM: compacteert het DB-bestand. Doorgaan?"):
        print("Geannuleerd.")
        return
    try:
        await db.vacuum()
        print("[ok] vacuum klaar.")
    except Exception as e:  # noqa: BLE001
        print(f"[!] vacuum faalde: {e}")


async def cmd_list_users() -> None:
    users = await db.list_users()
    if not users:
        print("  (geen users — bij eerste /login wordt /setup doorlopen)")
        return
    print(f"  {'naam':<20} {'rol':<8} {'pw':<6} laatste login")
    print(f"  {'-'*20} {'-'*8} {'-'*6} ----")
    for u in users:
        last = u.last_login.astimezone().strftime("%Y-%m-%d %H:%M") if u.last_login else "—"
        pw = "yes" if u.password_hash else "no"
        print(f"  {u.username:<20} {u.role:<8} {pw:<6} {last}")


async def cmd_reset_admin() -> None:
    users = await db.list_users()
    admins = [u for u in users if u.role == "admin"]
    if not admins:
        print("  geen admin-accounts om te wissen.")
        return
    print("  admins die gewist worden:")
    for u in admins:
        print(f"    - {u.username}")
    if not await confirm("Doorgaan?"):
        print("Geannuleerd.")
        return
    for u in admins:
        await db.delete_user(u.username)
    print(f"[ok] {len(admins)} admin-account(s) gewist.")
    print("    Bij eerstvolgende /login wordt /setup doorlopen.")


# ---- Niet-supported settings (eerlijk afmelden) -------------------------

NOT_SUPPORTED_MSG = {
    "/identity": "Identity-key management is niet beschikbaar via de companion-API. "
                 "Op companion-firmware is de identity vast tot fabrieksreset. "
                 "Kijk naar de seriële admin-CLI van de firmware als je 'm wilt wijzigen.",
    "/region": "Regional scope settings zitten in de firmware-build / admin-CLI, niet "
               "in het companion-protocol. Niet via meshcore-py te wijzigen.",
    "/pathhash": "Path-hash size zit ook in de firmware-CLI, niet in companion-protocol.",
}


# ---------------------------------------------------------------------------
# CLI loop
# ---------------------------------------------------------------------------

async def cli_loop(mc) -> None:
    print(HELP)
    while True:
        try:
            line = (await _ainput("> ")).strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return

        if not line:
            continue
        if line in ("/quit", "/exit"):
            return
        if line == "/help":
            print(HELP)
            continue
        if line == "/info":
            await print_status(mc)
            continue
        if line == "/bat":
            fn = getattr(mc.commands, "get_bat", None) or getattr(mc.commands, "get_battery", None)
            print(await fn() if fn else "geen get_bat methode gevonden")
            continue
        if line == "/poll":
            await manual_poll(mc)
            continue
        if line.startswith("/history"):
            await handle_history(line)
            continue

        # CLI is bewust minimaal — admin/channels/housekeeping zit in de Web UI.
        parts = line.split()
        head = parts[0]
        rest = parts[1:]

        if head == "/reset-admin":
            await cmd_reset_admin()
            continue
        if head == "/users":
            await cmd_list_users()
            continue

        if line.startswith("/dm "):
            parts = line.split(" ", 2)
            if len(parts) < 3:
                print("Gebruik: /dm <pubkey_prefix> <bericht>")
                continue
            prefix, text = parts[1], parts[2]
            await send_and_dispatch(mc, kind="dm", peer=prefix, text=text)
            continue

        # Onbekend slash-commando? Dan NIET als bericht versturen.
        if line.startswith("/"):
            print(f"[!] onbekend commando: {head}  — typ /help voor de lijst")
            continue

        # default: public channel
        await send_and_dispatch(mc, kind="channel", channel_idx=PUBLIC_CHANNEL_IDX, text=line)


async def handle_history(line: str) -> None:
    """Parse en toon /history-commando's."""
    parts = line.split()
    # /history [n]                        → public, n=20
    # /history dm <prefix> [n]            → DM met contact
    try:
        if len(parts) >= 2 and parts[1] == "dm":
            if len(parts) < 3:
                print("Gebruik: /history dm <pubkey_prefix> [n]")
                return
            prefix = parts[2]
            n = int(parts[3]) if len(parts) >= 4 else 20
            rows = await db.dm_history(prefix, n)
            header = f"-- DM historie met {prefix} (laatste {len(rows)}) --"
        else:
            n = int(parts[1]) if len(parts) >= 2 else 20
            rows = await db.channel_history(PUBLIC_CHANNEL_IDX, n)
            header = f"-- Public historie (laatste {len(rows)}) --"
    except ValueError:
        print("Aantal moet een geheel getal zijn")
        return

    print(header)
    if not rows:
        print("  (leeg)")
    for m in rows:
        print(f"  {m.fmt()}")
    print("-" * len(header))


# ---------------------------------------------------------------------------
# Connect helpers
# ---------------------------------------------------------------------------

class CompanionHandshakeError(RuntimeError):
    """Wordt gegooid als de node geen MeshCore-companion blijkt te zijn."""


async def _probe_companion(mc, timeout: float = 3.0) -> None:
    """Verifieer dat de aangesloten node echt antwoordt als companion.

    meshcore-py logt "No response from meshcore node" maar gooit zelf
    geen exception — dus doen we hier een eigen ping. Bij timeout of een
    leeg/None-antwoord → CompanionHandshakeError.
    """
    cmds = getattr(mc, "commands", None)
    if cmds is None:
        raise CompanionHandshakeError("geen commands-interface op MeshCore-instance")

    # send_appstart is de standaard handshake; valt terug op self-info.
    probe = getattr(cmds, "send_appstart", None) or getattr(cmds, "get_self_info", None)
    if probe is None:
        raise CompanionHandshakeError("geen handshake-commando beschikbaar in deze meshcore versie")

    try:
        result = await asyncio.wait_for(probe(), timeout=timeout)
    except asyncio.TimeoutError as e:
        raise CompanionHandshakeError(
            f"node antwoordde niet binnen {timeout}s — is dit wel companion firmware?"
        ) from e
    except Exception as e:  # noqa: BLE001
        raise CompanionHandshakeError(f"handshake faalde: {e}") from e

    if result is None or result is False:
        raise CompanionHandshakeError(
            "node retourneerde geen geldig handshake-antwoord — geen companion firmware?"
        )


async def connect_meshcore(port: str, baud: int) -> "MeshCore":
    """Maak een MeshCore-instance via serial en verifieer dat 'r een companion aan hangt.

    De factory-API heeft drie bekende vormen — we proberen ze in volgorde.
    Daarna een handshake-probe; faalt die, dan disconnecten + raise.
    """
    mc = None

    # 1) nieuwste: classmethod factory
    if hasattr(MeshCore, "create_serial"):
        mc = await MeshCore.create_serial(port, baud)

    # 2) tussenversie: SerialConnection-object meegeven aan ctor
    if mc is None:
        try:
            from meshcore.connection import SerialConnection  # type: ignore
            conn = SerialConnection(port, baud)
            mc = MeshCore(conn)
            await mc.connect()
        except ImportError:
            pass

    # 3) heel oud / alternatief: directe ctor met port-string
    if mc is None:
        mc = MeshCore(port=port, baudrate=baud)  # type: ignore[call-arg]
        if hasattr(mc, "connect"):
            await mc.connect()

    # Health-check
    try:
        await _probe_companion(mc)
    except CompanionHandshakeError:
        if hasattr(mc, "disconnect"):
            try:
                await mc.disconnect()
            except Exception:  # noqa: BLE001
                pass
        raise

    return mc


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

async def main() -> int:
    port = os.environ.get("MESHCORE_PORT") or auto_detect_port()
    baud = int(os.environ.get("MESHCORE_BAUD", DEFAULT_BAUD))
    db_path = os.environ.get("MESHCORE_DB", "meshcore.db")

    # CLI-flag: --reset-admin → wist alle admin-accounts (ww + user) zodat
    # /setup opnieuw door de eerste bezoeker doorlopen wordt. Handig als je
    # je admin-wachtwoord vergeten bent.
    reset_admin_flag = "--reset-admin" in sys.argv

    # DB eerst — als die kapot is, hoeven we niet eens met de radio te praten.
    print(f"[*] db: {db_path}")
    health = await db.init_db(db_path)
    print(f"    {health.summary()}")
    if not health.is_healthy():
        print(f"[!] DB-health niet ok: schema={health.schema_status} integrity={health.integrity}")
        print("    Maak een backup van het .db-bestand voordat je verdergaat.")
    if health.clean_shutdown is False:
        print("[!] vorige run is niet netjes afgesloten (mogelijk crash of kill -9).")
        print("    SQLite WAL-recovery is automatisch uitgevoerd.")
    print(f"    {await db.count_messages()} berichten in archief.")
    # Seed Public-kanaal als 'r nog geen DB-rij voor bestaat
    existing_channels = await db.list_channels()
    if not any(c.idx == 0 for c in existing_channels):
        await db.upsert_channel(0, name="Public", is_public=True, has_key=False, kind="public")

    # CLI-flag voor admin-reset — one-shot, vereist GEEN USB. Handig vanuit
    # een container: `docker compose run --rm gateway python gateway.py --reset-admin`.
    if reset_admin_flag:
        users = await db.list_users()
        admins = [u for u in users if u.role == "admin"]
        if not admins:
            print("[*] --reset-admin: geen admin-accounts om te wissen.")
        else:
            for u in admins:
                await db.delete_user(u.username)
            print(f"[*] --reset-admin: {len(admins)} admin-account(s) gewist.")
            print("    Bij eerstvolgende /login wordt /setup doorlopen.")
        await db.close_db()
        return 0

    if not port:
        print("ERROR: kon geen MeshCore-device vinden via USB.")
        print("       Steek de nRF52840 in, of zet MESHCORE_PORT=/dev/ttyACM0")
        await db.close_db()
        return 1

    print(f"[*] verbind met {port} @ {baud} baud …")
    try:
        mc = await connect_meshcore(port, baud)
    except CompanionHandshakeError as e:
        print(f"ERROR: handshake mislukt: {e}")
        print("       Controleer of de nRF52840 de MeshCore *companion* firmware draait")
        print("       (niet repeater / room-server).")
        return 3
    except Exception as e:  # noqa: BLE001
        print(f"ERROR: verbinding mislukt: {e}")
        return 2

    print("[*] verbonden.")
    await print_status(mc)
    await load_self_info(mc)
    await load_default_scope(mc)

    # Zet decrypt-channel-logs aan: koppelt RX_LOG_DATA (raw RF-metadata)
    # aan inkomende channel-msgs zodat 'path', 'RSSI', 'SNR' en 'attempt'
    # in de payload verschijnen. Werkt alleen voor channels die we kennen
    # (slot+key). Zonder deze toggle is path-info volledig afwezig.
    enable_fn = getattr(mc, "set_decrypt_channel_logs", None)
    if callable(enable_fn):
        try:
            enable_fn(True)
            print("[*] decrypt-channel-logs aan (path/RSSI/SNR per bericht)")
        except Exception as e:  # noqa: BLE001
            print(f"[!] decrypt-channel-logs aanzetten faalde: {e}")

    # registreer de built-in handlers op de dispatcher
    for kind in ("dm", "channel"):
        dispatch.register(kind, print_handler)
        dispatch.register(kind, db_handler)

    subscribe_messages(mc)
    if await start_message_pump(mc):
        print("[*] auto-message-fetcher actief.")

    # Auto-recap: laatste 10 van Public
    recap = await db.channel_history(PUBLIC_CHANNEL_IDX, 10)
    if recap:
        print(f"\n-- Laatste {len(recap)} op Public --")
        for m in recap:
            print(f"  {m.fmt()}")
        print("-" * 32)

    # graceful shutdown op Ctrl-C
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:
            pass  # Windows

    # Headless detectie: als stdin geen TTY is (systemd, docker zonder -it,
    # piped invoer), géén CLI-loop starten. Anders zou input() direct EOF
    # geven en zou main() per ongeluk afsluiten alsof de user /quit doet.
    has_tty = bool(getattr(sys.stdin, "isatty", lambda: False)())
    cli_task: Optional[asyncio.Task] = None
    if has_tty:
        cli_task = asyncio.create_task(cli_loop(mc))
    else:
        print("[*] geen TTY beschikbaar — CLI uitgeschakeld (headless modus)")

    stop_task = asyncio.create_task(stop.wait())
    wd_task = asyncio.create_task(watchdog(mc, stop))
    rc_task = asyncio.create_task(repeater_cache_loop(mc, stop))
    ts_task = asyncio.create_task(time_sync_loop(mc, stop))

    # Webserver — aan tenzij expliciet uitgezet met MESHCORE_WEB=0.
    web_task: Optional[asyncio.Task] = None
    if os.environ.get("MESHCORE_WEB", "1") not in ("0", "false", "no"):
        try:
            import web as web_mod
            asgi_app, _ = web_mod.setup_web(
                mc=mc,
                send_channel=lambda idx, text, parent_id=None: send_and_dispatch(
                    mc, kind="channel", channel_idx=idx, text=text, parent_id=parent_id
                ),
                send_dm=lambda peer, text, parent_id=None: send_and_dispatch(
                    mc, kind="dm", peer=peer, text=text, parent_id=parent_id
                ),
                dispatch_obj=dispatch,
                gateway_state=state,
                stop_event=stop,
            )
            host = os.environ.get("MESHCORE_WEB_HOST", "127.0.0.1")
            port = int(os.environ.get("MESHCORE_WEB_PORT", "8080"))
            web_task = asyncio.create_task(web_mod.serve(asgi_app, host, port, stop))
            n_users = await db.count_users()
            print(f"[*] web UI op http://{host}:{port}/  ({n_users} user(s) in DB)")
            if n_users == 0:
                print("    Eerste keer? Open de URL en maak een admin via /setup.")
        except Exception as e:  # noqa: BLE001
            print(f"[!] web UI niet gestart: {e}")
    else:
        print("[*] web UI uit (MESHCORE_WEB=0)")

    # Bots — DB-driven (admin definieert in Web UI). Altijd aan; als geen
    # bots zijn gedefinieerd doet 'ie simpelweg niets.
    try:
        import time as _t
        import bot as bot_mod
        bot_handler = bot_mod.setup_bot(
            mc=mc,
            send_channel=lambda idx, text: send_and_dispatch(
                mc, kind="channel", channel_idx=idx, text=text
            ),
            gateway_started_at=_t.time(),
            self_pubkey=getattr(state, "self_pubkey", None),
            gateway_state=state,
        )
        dispatch.register("channel", bot_handler)
        print("[*] bot-framework actief — definieer bots in Admin → Bots")
    except Exception as e:  # noqa: BLE001
        print(f"[!] bot-framework niet gestart: {e}")

    wait_set = {stop_task}
    if cli_task is not None:
        wait_set.add(cli_task)
    await asyncio.wait(wait_set, return_when=asyncio.FIRST_COMPLETED)

    # Stop alle achtergrondtasks netjes
    stop.set()

    # Web-task: laat 'm zelf afronden via z'n eigen stop-respect.
    # Cancellen midden in uvicorn's lifespan-shutdown veroorzaakt logspam.
    if web_task is not None and not web_task.done():
        try:
            await asyncio.wait_for(web_task, timeout=5.0)
        except asyncio.TimeoutError:
            web_task.cancel()
            try:
                await web_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass

    # CLI-task (alleen als TTY), watchdog, repeater-cache cancellen
    bg_tasks = [wd_task, rc_task, ts_task]
    if cli_task is not None:
        bg_tasks.append(cli_task)
    for t in bg_tasks:
        if not t.done():
            t.cancel()
            try:
                await t
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass

    if hasattr(mc, "disconnect"):
        try:
            await mc.disconnect()
        except Exception:  # noqa: BLE001
            pass
    await db.close_db()
    if state.watchdog_restart_requested:
        # Exit-code 75 (EX_TEMPFAIL) — niet 0, niet 1, signaleert "tijdelijk
        # probleem, herstart aub". systemd Restart=always én Restart=on-failure
        # interpreteren dit als reden om opnieuw te starten.
        print("auto-restart (watchdog).")
        return 75
    print("doei.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))


# ---------------------------------------------------------------------------
# NOTES — als iets niet werkt
# ---------------------------------------------------------------------------
# • ImportError "EventType":              meshcore versie is te oud, doe `pip install -U meshcore`
# • AttributeError op send_chan_msg:      check `dir(mc.commands)` en pas send_channel() hierboven aan
# • Geen device gevonden:                 `ls -l /dev/ttyACM*` op de Pi, of `lsusb` voor de VID
# • Permission denied op /dev/ttyACM0:    voeg gebruiker toe aan dialout-groep:
#     sudo usermod -aG dialout $USER  (en uitloggen/inloggen)
# ---------------------------------------------------------------------------
