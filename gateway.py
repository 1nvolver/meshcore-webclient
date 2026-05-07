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


# Globale runtime-state. Klein houden; voor grote dingen → eigen module.
class GatewayState:
    self_pubkey: Optional[str] = None
    self_name: Optional[str] = None


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

class IncomingMessage:
    """Stabiele shape voor inkomende berichten — onafhankelijk van de
    exacte meshcore-py event payload. Dispatch-handlers werken hierop."""

    __slots__ = ("kind", "channel_idx", "sender", "text", "raw")

    def __init__(self, kind: str, channel_idx, sender, text, raw):
        self.kind = kind                  # "dm" | "channel"
        self.channel_idx = channel_idx    # int | None
        self.sender = sender              # str | None  (pubkey_prefix)
        self.text = text                  # str
        self.raw = raw                    # original payload dict


class Dispatch:
    """Eenvoudige fan-out: registreer N handlers per message-kind, fire 'm
    één voor één. Een falende handler stopt de andere niet."""

    def __init__(self):
        self._handlers: dict[str, list] = {"dm": [], "channel": []}

    def register(self, kind: str, handler) -> None:
        self._handlers.setdefault(kind, []).append(handler)

    async def fire(self, msg: IncomingMessage) -> None:
        for h in list(self._handlers.get(msg.kind, [])):
            try:
                await h(msg)
            except Exception as e:  # noqa: BLE001
                name = getattr(h, "__name__", repr(h))
                print(f"\r[!] handler {name} faalde: {e}\n> ", end="", flush=True)


# Singleton dispatcher voor deze proces-instantie
dispatch = Dispatch()


# Built-in handlers ---------------------------------------------------------

async def print_handler(msg: IncomingMessage) -> None:
    if msg.kind == "channel":
        tag = f"CH{msg.channel_idx if msg.channel_idx is not None else '?'}"
    else:
        tag = f"DM   {msg.sender or '?'}"
    sender = f" {msg.sender}" if msg.kind == "channel" and msg.sender else ""
    sys.stdout.write(f"\r[{tag}{sender}] {msg.text}\n> ")
    sys.stdout.flush()


async def db_handler(msg: IncomingMessage) -> None:
    try:
        await db.save_message(
            direction="in",
            kind=msg.kind,
            text=str(msg.text),
            channel_idx=msg.channel_idx,
            peer=msg.sender,
            raw=msg.raw,
        )
    except Exception as e:  # noqa: BLE001
        print(f"\r[!] db save ({msg.kind} in) faalde: {e}\n> ", end="", flush=True)


# Adapters: vertalen meshcore events naar IncomingMessage en fire'n -------

async def on_contact_msg(event):
    sender = _extract(event, "pubkey_prefix", "from", "src", default=None)
    text = _extract(event, "text", "msg", "message", default="")
    msg = IncomingMessage(
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
    msg = IncomingMessage(
        kind="channel",
        channel_idx=idx,
        sender=str(sender) if sender and sender != "?" else None,
        text=str(text),
        raw=_payload(event),
    )
    await dispatch.fire(msg)


# ---------------------------------------------------------------------------
# CLI loop
# ---------------------------------------------------------------------------

HELP = """\
Berichten:
  <tekst>                       stuur naar Public channel (idx 0)
  /c <slot> <tekst>             stuur naar specifiek channel-slot
  /dm <prefix> <tekst>          directe boodschap (pubkey-prefix, hex)
  /history [n]                  laatste n berichten van Public (default 20)
  /history dm <prefix> [n]      laatste n DM's met die contact

Status:
  /info                         herlees node-info
  /bat                          batterijstatus
  /poll                         handmatig nieuwe berichten ophalen

Channels:
  /channels [show]              toon channels (uit DB)
  /channels add <slot> <naam> [hex-key]
                                voeg channel toe (slot 1-7); zonder key = nieuwe random key
  /channels remove <slot>       verwijder uit DB-metadata
  /channels alias <slot> <naam> alias voor lokaal gebruik

Admin (radio / node):
  /radio [show]                 toon huidige radio-instellingen
  /radio set <freq> <bw> <sf> <cr>   alle modulatie-params tegelijk
  /radio freq|bw|sf|cr <waarde> één parameter; rest blijft
  /radio txpower <dBm>          tx-vermogen
  /name <nieuwe-naam>           verander adv-naam
  /coords show | clear | <lat> <lon>
  /advert-policy <0|1|2|3>      adv-locatie-beleid (firmware-afhankelijk)
  /reboot                       reboot de companion (vereist na set_radio/txpower)
  /identity | /region | /pathhash   (zie uitleg in CLI)

Housekeeping:
  /clean older-than <30d|24h|...>  verwijder oude berichten
  /clean all                    verwijder alle berichten
  /vacuum                       compacteer DB-bestand

Algemeen:
  /help                         deze help
  /quit                         stoppen
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


DEBUG_EVENTS = os.environ.get("MESHCORE_DEBUG", "").lower() in ("1", "true", "yes")


async def _on_any_event(event):
    """Debug-handler: print elk event dat binnenkomt (alleen in debug-modus)."""
    name = getattr(getattr(event, "type", None), "name", "?")
    payload = getattr(event, "payload", event)
    print(f"\r[ev {name}] {payload}\n> ", end="", flush=True)


def subscribe_messages(mc) -> None:
    """Hang event handlers aan inkomende DM- en kanaal-berichten."""
    if EventType is None:
        print("[!] EventType niet beschikbaar in deze meshcore versie; "
              "inkomende berichten worden mogelijk niet getoond.")
        return

    candidates = [
        ("CONTACT_MSG_RECV", on_contact_msg),
        ("CHANNEL_MSG_RECV", on_channel_msg),
        # alternatieve namen die in oudere/nieuwere versies voorkomen
        ("MSG_RECV", on_contact_msg),
        ("CHANNEL_MSG", on_channel_msg),
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
    # de companion echt vuurt. Activeer met MESHCORE_DEBUG=1.
    if DEBUG_EVENTS:
        try:
            for et in list(EventType):  # type: ignore[arg-type]
                if et in seen:
                    continue
                try:
                    mc.subscribe(et, _on_any_event)
                except Exception:  # noqa: BLE001
                    pass
            print("[debug] subscribed op alle EventType-leden")
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

        # ---- Phase 3: admin / channels / housekeeping ------------------
        parts = line.split()
        head = parts[0]
        rest = parts[1:]

        if head == "/radio":
            await cmd_radio(mc, rest)
            continue
        if head == "/name" and rest:
            await cmd_set_name(mc, " ".join(rest))
            continue
        if head == "/coords":
            await cmd_set_coords(mc, rest)
            continue
        if head == "/advert-policy":
            await cmd_advert_policy(mc, rest)
            continue
        if head == "/reboot":
            await cmd_reboot(mc)
            continue
        if head == "/channels":
            await cmd_channels(mc, rest)
            continue
        if head == "/c" and len(rest) >= 2:
            try:
                slot = int(rest[0])
            except ValueError:
                print("Gebruik: /c <slot> <tekst>")
                continue
            text = " ".join(rest[1:])
            try:
                res = await send_channel(mc, slot, text)
                print(f"[→ CH{slot}] {text}   ack/result: {res}")
                await db.save_message(
                    direction="out", kind="channel", text=text,
                    channel_idx=slot, peer="self", raw=str(res),
                )
            except Exception as e:  # noqa: BLE001
                print(f"[!] verzenden mislukt: {e}")
            continue
        if head == "/clean":
            await cmd_clean(rest)
            continue
        if head == "/vacuum":
            await cmd_vacuum()
            continue
        if head in NOT_SUPPORTED_MSG:
            print(NOT_SUPPORTED_MSG[head])
            continue

        if line.startswith("/dm "):
            parts = line.split(" ", 2)
            if len(parts) < 3:
                print("Gebruik: /dm <pubkey_prefix> <bericht>")
                continue
            prefix, text = parts[1], parts[2]
            try:
                res = await send_dm(mc, prefix, text)
                print(f"[→ DM {prefix}] {text}   ack/result: {res}")
                await db.save_message(
                    direction="out", kind="dm", text=text, peer=prefix, raw=str(res),
                )
            except Exception as e:  # noqa: BLE001
                print(f"[!] verzenden mislukt: {e}")
            continue

        # Onbekend slash-commando? Dan NIET als bericht versturen.
        if line.startswith("/"):
            print(f"[!] onbekend commando: {head}  — typ /help voor de lijst")
            continue

        # default: public channel
        try:
            res = await send_channel(mc, PUBLIC_CHANNEL_IDX, line)
            print(f"[→ CH{PUBLIC_CHANNEL_IDX}] {line}   ack/result: {res}")
            await db.save_message(
                direction="out",
                kind="channel",
                text=line,
                channel_idx=PUBLIC_CHANNEL_IDX,
                peer="self",
                raw=str(res),
            )
        except Exception as e:  # noqa: BLE001
            print(f"[!] verzenden mislukt: {e}")


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
        await db.upsert_channel(0, name="Public", is_public=True, has_key=False)

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

    cli_task = asyncio.create_task(cli_loop(mc))
    stop_task = asyncio.create_task(stop.wait())
    wd_task = asyncio.create_task(watchdog(mc, stop))
    await asyncio.wait({cli_task, stop_task}, return_when=asyncio.FIRST_COMPLETED)

    # Stop alle achtergrondtasks netjes
    stop.set()
    for t in (cli_task, wd_task):
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
