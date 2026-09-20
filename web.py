"""
Web-laag voor de MeshCore Gateway.

FastAPI + python-socketio. Stelt een real-time chat interface beschikbaar
voor de Public channel (en in latere stappen ook private channels + admin).

Wordt door gateway.py opgestart als de env var MESHCORE_WEB_PASSWORD is
gezet. Zonder password start de webserver niet — bewuste keuze: we willen
geen open server op je LAN.

Auth: in-memory session-tokens. Bij herstart van de gateway zijn alle
sessies ongeldig en moeten gebruikers opnieuw inloggen. Goed genoeg voor
een thuis-tool.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import secrets
import time as _time
from pathlib import Path
from typing import Awaitable, Callable, Optional

import socketio
import uvicorn
from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

import db


BASE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))


# Versie-nummer x.y.z:
#   x = major (handmatig te bepalen)
#   y = minor (handmatig te bepalen)
#   z = dot-versie, bumpt bij elke door de gebruiker gevraagde wijziging
APP_VERSION = "1.1.052"

# Module-logger; uvicorn pikt deze automatisch op via root-handlers (stdout,
# systemd-journal, docker logs). Geen extra config nodig.
log = logging.getLogger("meshcore.web")


# ---------------------------------------------------------------------------
# Config / state
# ---------------------------------------------------------------------------

# Sessies: token → {username, role, allowed_views, last_seen, ...}
# In-memory; bij gateway-restart moet iedereen opnieuw inloggen. Survives
# browser-tab-close via persistent cookie (sliding 7d expiry) — zie
# _sliding_session_cookie middleware en SESSION_MAX_AGE.
_SESSIONS: dict[str, dict] = {}
COOKIE_NAME = "mc_auth"
# Sessie-levensduur (sliding). 7 dagen vanaf last_seen — elke succesvolle
# request met geldig token refresht 'm. Server-restart wist alles. Logout
# wist alleen die ene sessie. Bewust niet eindeloos: tab op gedeeld apparaat
# blijft hooguit 7d na laatste gebruik open. v1.1.047.
SESSION_MAX_AGE = 7 * 86400

# OTA-repeater-management: per (web_username, repeater_pubkey_lower) een sessie
# met login-tijd en laatste activiteit. In-memory; companion zelf vergeet de
# sessie ook na inactiviteit (firmware-side), dus dit is best-effort tracking.
_REPEATER_SESSIONS: dict[tuple[str, str], dict] = {}
_REPEATER_SESSION_TTL_SECS = 120  # client-zijde verloop-hint (UI mag herloggen)

# /admin/state cache: endpoint doet meerdere companion-calls (self_info, battery,
# device_info, node_status) + 2 DB-queries. UI poll't elke 10-30s, dus zonder
# cache zit de USB-bus onnodig vol. TTL kort houden zodat verse wijzigingen
# (radio/name/coords/channel) binnen redelijke tijd doorkomen; ?fresh=1 bypassed
# voor de UI direct na een mutatie.
_ADMIN_STATE_CACHE_TTL_SECS = 5.0
_admin_state_cache: dict = {"ts": 0.0, "data": None}


def _invalidate_admin_state_cache() -> None:
    """Forceert de volgende /admin/state-call om verse companion-data te halen.
    Roep dit aan vanuit endpoints die node/radio/channel-state wijzigen als
    je niet wil wachten tot de natuurlijke TTL is verlopen."""
    _admin_state_cache["ts"] = 0.0
    _admin_state_cache["data"] = None


# ---- Password hashing (pbkdf2_sha256, std-lib only) ----

_PBKDF2_ITERATIONS = 200_000


def hash_password(pw: str) -> str:
    salt = secrets.token_bytes(16)
    h = hashlib.pbkdf2_hmac("sha256", pw.encode("utf-8"), salt, _PBKDF2_ITERATIONS)
    return f"pbkdf2_sha256${_PBKDF2_ITERATIONS}${salt.hex()}${h.hex()}"


def verify_password(stored: str, pw: str) -> bool:
    try:
        algo, iter_s, salt_hex, h_hex = stored.split("$")
        if algo != "pbkdf2_sha256":
            return False
        iterations = int(iter_s)
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(h_hex)
        actual = hashlib.pbkdf2_hmac("sha256", pw.encode("utf-8"), salt, iterations)
        return secrets.compare_digest(expected, actual)
    except Exception:  # noqa: BLE001
        return False


# ---- Session helpers ----

def _new_session(user) -> str:
    """Maak token voor een ingelogde User-row."""
    token = secrets.token_urlsafe(24)
    try:
        views = json.loads(user.allowed_views or "[]")
    except Exception:  # noqa: BLE001
        views = ["chat"]
    _SESSIONS[token] = {
        "username": user.username,
        "role": user.role,
        "allowed_views": views,
        "callsign": (user.callsign or "") if hasattr(user, "callsign") else "",
        "last_seen": _time.time(),
    }
    return token


def _session(token: Optional[str]) -> Optional[dict]:
    if not token:
        return None
    s = _SESSIONS.get(token)
    if not s:
        return None
    # Sliding TTL — inactiviteit > SESSION_MAX_AGE = sessie verlopen.
    if _time.time() - s.get("last_seen", 0) > SESSION_MAX_AGE:
        _SESSIONS.pop(token, None)
        return None
    return s


def _session_from_request(request: Request) -> Optional[dict]:
    return _session(request.cookies.get(COOKIE_NAME))


def _is_authed_request(request: Request) -> bool:
    return _session_from_request(request) is not None


def _is_admin_request(request: Request) -> bool:
    s = _session_from_request(request)
    return bool(s and s.get("role") == "admin")


def _is_authed_environ(environ: dict) -> bool:
    return _session_from_environ(environ) is not None


def _session_from_environ(environ: dict) -> Optional[dict]:
    """Variant van _session_from_request voor Socket.IO environ-dict."""
    raw_cookie = environ.get("HTTP_COOKIE", "")
    cookies = {}
    for part in raw_cookie.split(";"):
        if "=" in part:
            k, v = part.strip().split("=", 1)
            cookies[k] = v
    return _SESSIONS.get(cookies.get(COOKIE_NAME))


# ---------------------------------------------------------------------------
# HTML (inline — geen template engine nodig voor MVP)
# ---------------------------------------------------------------------------

LOGIN_HTML = """<!DOCTYPE html>
<html lang="nl"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>MeshCore Gateway Web Client — Login</title>
<style>
  body{font-family:system-ui,sans-serif;max-width:400px;margin:5em auto;padding:1em;background:#f6f6f6}
  @media (max-width:480px){body{margin:1.5em auto;padding:0.8em}}
  h2{margin-top:0}
  form{background:#fff;padding:1.5em;border-radius:8px;box-shadow:0 1px 3px rgba(0,0,0,0.1)}
  label{display:block;margin-bottom:0.5em;font-size:0.9em;color:#444}
  /* font-size:16px voorkomt iOS-zoom-on-focus */
  input,button{display:block;width:100%;padding:12px;margin-top:6px;font:inherit;font-size:16px;box-sizing:border-box;border-radius:4px;border:1px solid #ccc}
  button{margin-top:1em;background:#2c5;color:#fff;border:0;font-weight:600;cursor:pointer;min-height:44px}
  button:hover{background:#1b4}
  .err{color:#c33;padding:8px 0;font-size:0.9em}
  footer{margin-top:2em;text-align:center;color:#888;font-size:0.8em}
</style></head>
<body>
<h2>MeshCore Gateway Web Client</h2>
<form method="POST" action="/login">
  <label>Gebruikersnaam<input type="text" name="username" autofocus required autocomplete="username"></label>
  <label>Wachtwoord<input type="password" name="password" required autocomplete="current-password"></label>
  <button type="submit">Login</button>
  {err}
</form>
<footer>&copy; Flight 815 B.V.</footer>
</body></html>"""


SETUP_HTML = """<!DOCTYPE html>
<html lang="nl"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>MeshCore Gateway Web Client — Setup</title>
<style>
  body{font-family:system-ui,sans-serif;max-width:420px;margin:5em auto;padding:1em;background:#f6f6f6}
  @media (max-width:480px){body{margin:1.5em auto;padding:0.8em}}
  h2{margin-top:0}p.intro{color:#555;font-size:0.9em}
  form{background:#fff;padding:1.5em;border-radius:8px;box-shadow:0 1px 3px rgba(0,0,0,0.1)}
  label{display:block;margin-bottom:0.5em;font-size:0.9em;color:#444}
  /* font-size:16px voorkomt iOS-zoom-on-focus */
  input,button{display:block;width:100%;padding:12px;margin-top:6px;font:inherit;font-size:16px;box-sizing:border-box;border-radius:4px;border:1px solid #ccc}
  button{margin-top:1em;background:#2c5;color:#fff;border:0;font-weight:600;cursor:pointer;min-height:44px}
  button:hover{background:#1b4}
  .err{color:#c33;padding:8px 0;font-size:0.9em}
  footer{margin-top:2em;text-align:center;color:#888;font-size:0.8em}
</style></head>
<body>
<h2>MeshCore Gateway Web Client</h2>
<p class="intro">Eerste opzet — maak de admin-gebruiker aan.</p>
<form method="POST" action="/setup">
  <label>Gebruikersnaam<input type="text" name="username" autofocus required minlength="2"></label>
  <label>Wachtwoord<input type="password" name="password" required minlength="6"></label>
  <label>Wachtwoord (nogmaals)<input type="password" name="password2" required minlength="6"></label>
  <button type="submit">Aanmaken</button>
  {err}
</form>
<footer>&copy; Flight 815 B.V.</footer>
</body></html>"""


# FIRSTLOGIN_HTML verwijderd — geforceerde wijziging gebeurt via in-app modal
# in APP_HTML zelf (forced change-password als must_change=true).

# APP_HTML lives in templates/index.html and static/{app.css,app.js}


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------

SendChannelFn = Callable[[int, str], Awaitable[bool]]


def setup_web(*, mc, send_channel: SendChannelFn, send_dm,
               dispatch_obj, gateway_state, stop_event) -> tuple:
    send_dm_fn = send_dm  # rebind voor sluiting in @sio.event
    """Bouw de ASGI-app + bezorg een hook op de dispatcher.

    Args:
        mc:            de MeshCore-instance, voor admin-commands.
        send_channel:  async (idx, text) → bool. Voor uitgaand van web → mesh.
        dispatch_obj:  de Dispatch singleton uit gateway.py — wij hangen
                       een handler op die elke channel-msg naar alle
                       verbonden clients broadcast.
        gateway_state: de GatewayState singleton (self_name, self_pubkey).
        stop_event:    asyncio.Event uit gateway.py main() — set 'm vanuit
                       /admin/quit om de hele gateway af te sluiten.

    Returns:
        (asgi_app, sio_server) tuple
    """
    # Multi-user: gebruiker logt in met username + eigen wachtwoord. Bij
    # een lege users-tabel doorloopt iedereen eerst /setup om de eerste
    # admin aan te maken.

    sio = socketio.AsyncServer(async_mode="asgi", cors_allowed_origins=[])
    app = FastAPI(docs_url=None, redoc_url=None)
    app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")

    # /admin/state-cache automatisch invalideren na een succesvolle admin-mutatie.
    # Scheelt het handmatig invalideren in elke endpoint — UI ziet wijzigingen
    # direct na de POST i.p.v. te wachten op de TTL.
    @app.middleware("http")
    async def _admin_state_cache_invalidator(request: Request, call_next):
        response = await call_next(request)
        if (request.method in ("POST", "PUT", "DELETE", "PATCH")
                and request.url.path.startswith("/admin/")
                and request.url.path != "/admin/state"
                and 200 <= response.status_code < 300):
            _invalidate_admin_state_cache()
        return response

    # Sliding session-cookie + last_seen refresh (v1.1.047). Bij elke request
    # met een geldige sessie verlengen we het cookie-vervaldatum naar
    # SESSION_MAX_AGE vanaf nu, zodat de browser na tab-close de sessie
    # behoudt zolang er activiteit is. Server-side last_seen wordt geüpdatet
    # zodat _session() de inactiviteits-expiry kan toepassen. Throttling: niet
    # vaker dan elke 60s schrijven (UI poll't elke 10-30s; geen zin elke poll
    # een Set-Cookie te sturen).
    _COOKIE_REFRESH_INTERVAL_SECS = 60
    @app.middleware("http")
    async def _sliding_session_cookie(request: Request, call_next):
        response = await call_next(request)
        # Als de route zelf al iets met onze cookie deed (login/logout/setup)
        # niet overschrijven — anders maken we de delete_cookie van /logout
        # ongedaan.
        for h_name, h_value in response.raw_headers:
            if (h_name.lower() == b"set-cookie"
                    and h_value.startswith(COOKIE_NAME.encode() + b"=")):
                return response
        token = request.cookies.get(COOKIE_NAME)
        if not token:
            return response
        sess = _SESSIONS.get(token)
        if not sess:
            return response
        now = _time.time()
        if now - sess.get("last_seen", 0) > SESSION_MAX_AGE:
            # Verlopen — niets refreshen, _session() haalt 'm zo wel weg
            return response
        if now - sess.get("last_seen", 0) < _COOKIE_REFRESH_INTERVAL_SECS:
            # Recent al gerefresht; sla over om bandwidth/headers te sparen
            return response
        sess["last_seen"] = now
        response.set_cookie(COOKIE_NAME, token, httponly=True, samesite="lax",
                            max_age=SESSION_MAX_AGE)
        return response

    # ---------- HTTP routes -----------------------------------------------

    @app.get("/login", response_class=HTMLResponse)
    async def login_get():
        if await db.count_users() == 0:
            return RedirectResponse("/setup", status_code=303)
        return HTMLResponse(LOGIN_HTML.replace("{err}", ""))

    @app.post("/login")
    async def login_post(username: str = Form(...), password: str = Form(...)):
        if await db.count_users() == 0:
            return RedirectResponse("/setup", status_code=303)
        username = username.strip()
        u = await db.get_user(username)
        bad = HTMLResponse(
            LOGIN_HTML.replace("{err}", '<div class="err">Onbekende gebruiker of fout wachtwoord</div>'),
            status_code=401,
        )
        if u is None or not u.password_hash:
            return bad
        if not verify_password(u.password_hash, password):
            return bad
        await db.touch_user_login(username)
        token = _new_session(u)
        # Refresh sessie-flag must_change uit DB
        _SESSIONS[token]["must_change"] = bool(u.must_change_password)
        resp = RedirectResponse("/", status_code=303)
        resp.set_cookie(COOKIE_NAME, token, httponly=True, samesite="lax",
                        max_age=SESSION_MAX_AGE)
        return resp

    @app.get("/setup", response_class=HTMLResponse)
    async def setup_get():
        if await db.count_users() > 0:
            return RedirectResponse("/login", status_code=303)
        return HTMLResponse(SETUP_HTML.replace("{err}", ""))

    @app.post("/setup")
    async def setup_post(username: str = Form(...),
                          password: str = Form(...),
                          password2: str = Form(...)):
        if await db.count_users() > 0:
            return RedirectResponse("/login", status_code=303)
        username = username.strip()
        if not username:
            return HTMLResponse(SETUP_HTML.replace("{err}", '<div class="err">Naam vereist</div>'), status_code=400)
        if password != password2:
            return HTMLResponse(SETUP_HTML.replace("{err}", '<div class="err">Wachtwoorden komen niet overeen</div>'), status_code=400)
        if len(password) < 6:
            return HTMLResponse(SETUP_HTML.replace("{err}", '<div class="err">Wachtwoord min 6 tekens</div>'), status_code=400)
        u = await db.add_user(username, role="admin", allowed_views=["chat", "admin"])
        await db.set_user_password_hash(username, hash_password(password))
        u = await db.get_user(username)
        token = _new_session(u)
        resp = RedirectResponse("/", status_code=303)
        resp.set_cookie(COOKIE_NAME, token, httponly=True, samesite="lax",
                        max_age=SESSION_MAX_AGE)
        return resp

    # /first-login is verwijderd: admin geeft tijdelijk ww + must_change=true,
    # de geforceerde wijzig-flow loopt via /change-password (modal in UI).

    @app.get("/logout")
    async def logout(request: Request):
        tok = request.cookies.get(COOKIE_NAME)
        if tok:
            _SESSIONS.pop(tok, None)
        resp = RedirectResponse("/login", status_code=303)
        resp.delete_cookie(COOKIE_NAME)
        return resp

    @app.get("/", response_class=HTMLResponse)
    async def index(request: Request):
        if await db.count_users() == 0:
            return RedirectResponse("/setup", status_code=303)
        if not _is_authed_request(request):
            return RedirectResponse("/login")
        return templates.TemplateResponse(
            request, "index.html", {"VERSION": APP_VERSION}
        )

    @app.get("/healthz")
    async def healthz():
        return {"ok": True, "sessions": len(_SESSIONS)}

    @app.get("/reports/overview")
    async def reports_overview(request: Request, hours: float = 24.0):
        _auth_or_401(request)
        return await db.reports_overview(hours=hours)

    @app.get("/me")
    async def me(request: Request):
        s = _session_from_request(request)
        if not s:
            return JSONResponse({"detail": "not authenticated"}, status_code=401)
        return {
            "username": s["username"],
            "role": s["role"],
            "allowed_views": s["allowed_views"],
            "callsign": s.get("callsign", ""),
            "must_change_password": bool(s.get("must_change", False)),
        }

    # -------- Admin routes ------------------------------------------------

    # _time staat top-level geïmporteerd sinds v1.1.048 (was hier lokaal,
    # maar _new_session op module-niveau had 'm ook nodig).
    from fastapi import HTTPException

    started_at = _time.time()

    def _auth_or_401(request: Request):
        if not _is_authed_request(request):
            raise HTTPException(status_code=401, detail="not authenticated")

    def _admin_or_403(request: Request):
        s = _session_from_request(request)
        if not s:
            raise HTTPException(status_code=401, detail="not authenticated")
        if s.get("role") != "admin":
            raise HTTPException(status_code=403, detail="admin only")

    def _resolve_cmd(*names):
        cmds = getattr(mc, "commands", None)
        if cmds is None:
            return None
        for n in names:
            fn = getattr(cmds, n, None)
            if callable(fn):
                return fn
        return None

    async def _read_self_info():
        fn = _resolve_cmd("get_self_info", "send_appstart")
        if fn is None:
            return {}
        try:
            ev = await asyncio.wait_for(fn(), timeout=3.0)
        except Exception:  # noqa: BLE001
            return {}
        return getattr(ev, "payload", ev) if not isinstance(ev, dict) else ev

    async def _read_battery():
        """Returnt batterij in millivolt of None."""
        fn = _resolve_cmd("get_bat", "get_battery")
        if fn is None:
            return None
        try:
            ev = await asyncio.wait_for(fn(), timeout=2.0)
            payload = getattr(ev, "payload", ev) if not isinstance(ev, dict) else ev
            if isinstance(payload, dict):
                return payload.get("level")
            return None
        except Exception:  # noqa: BLE001
            return None

    async def _read_device_info():
        """Returnt dict met model/ver/fw_build/etc, of {} bij fout."""
        fn = _resolve_cmd("send_device_query", "get_device_info")
        if fn is None:
            return {}
        try:
            ev = await asyncio.wait_for(fn(), timeout=2.0)
            payload = getattr(ev, "payload", ev) if not isinstance(ev, dict) else ev
            return payload if isinstance(payload, dict) else {}
        except Exception:  # noqa: BLE001
            return {}

    async def _read_node_status():
        """Combineert STATS_CORE (uptime, battery, errors, queue_len) met
        STATS_RADIO (last_rssi, last_snr, noise_floor, tx/rx_air_secs).
        Returnt één dict met alles wat beschikbaar is."""
        merged = {}
        for cmd_name in ("get_stats_core", "get_stats_radio"):
            fn = getattr(getattr(mc, "commands", None), cmd_name, None)
            if not callable(fn):
                continue
            try:
                ev = await asyncio.wait_for(fn(), timeout=2.0)
                payload = getattr(ev, "payload", ev) if not isinstance(ev, dict) else ev
                if isinstance(payload, dict):
                    merged.update(payload)
            except Exception:  # noqa: BLE001
                pass
        return merged

    def _fmt_uptime(secs):
        if not isinstance(secs, (int, float)) or secs < 0:
            return None
        secs = int(secs)
        d, rem = divmod(secs, 86400)
        h, rem = divmod(rem, 3600)
        m, s = divmod(rem, 60)
        if d:    return f"{d}d {h}h {m}m"
        if h:    return f"{h}h {m}m"
        if m:    return f"{m}m {s}s"
        return f"{s}s"

    @app.get("/admin", response_class=HTMLResponse)
    async def admin_page(request: Request):
        # Single-page-app: alle views zitten in '/'
        return RedirectResponse("/")

    # ---------- History endpoints (REST, niet via socket) -----------------

    from datetime import timezone as _tz

    def _iso_utc(dt):
        # Stuur altijd UTC ISO-string; browser doet de localisatie.
        if dt is None:
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=_tz.utc)
        return dt.astimezone(_tz.utc).isoformat()

    def _row_to_dict(m):
        # raw is opgeslagen als JSON-string; client parset 'm zelf
        ack_iso = _iso_utc(m.acked_at) if m.acked_at else None
        latency_s = None
        if m.acked_at and m.ts:
            ts = m.ts if m.ts.tzinfo else m.ts.replace(tzinfo=_tz.utc)
            ak = m.acked_at if m.acked_at.tzinfo else m.acked_at.replace(tzinfo=_tz.utc)
            latency_s = round((ak - ts).total_seconds(), 2)
        return {
            "id": m.id,
            "ts": _iso_utc(m.ts),
            "direction": m.direction,
            "peer": m.peer or ("self" if m.direction == "out" else "?"),
            "text": m.text,
            "kind": m.kind,
            "channel_idx": m.channel_idx,
            "raw": m.raw,
            "ack_status": m.ack_status,
            "acked_at": ack_iso,
            "latency_s": latency_s,
            "expected_ack": m.expected_ack,
            "parent_id": getattr(m, "parent_id", None),
        }

    @app.get("/channels/{idx}/history")
    async def channel_history_endpoint(request: Request, idx: int,
                                        limit: int = 30,
                                        before_id: Optional[int] = None):
        _auth_or_401(request)
        rows = await db.channel_history(idx, limit, before_id=before_id)
        return [_row_to_dict(m) for m in rows]

    @app.get("/hashtags/history")
    async def hashtag_history_endpoint(request: Request, tag: str, limit: int = 30):
        _auth_or_401(request)
        rows = await db.public_history_with_tag(tag, limit)
        return [_row_to_dict(m) for m in rows]

    @app.get("/dm/{peer}/history")
    async def dm_history_endpoint(request: Request, peer: str,
                                   limit: int = 30,
                                   before_id: Optional[int] = None):
        _auth_or_401(request)
        rows = await db.dm_history(peer, limit, before_id=before_id)
        return [_row_to_dict(m) for m in rows]

    @app.get("/messages/search")
    async def messages_search(request: Request, q: str,
                                kind: Optional[str] = None,
                                channel_idx: Optional[int] = None,
                                peer: Optional[str] = None,
                                limit: int = 100):
        _auth_or_401(request)
        rows = await db.search_messages(
            q, kind=kind, channel_idx=channel_idx, peer=peer, limit=limit
        )
        return [_row_to_dict(m) for m in rows]

    # ---------- Per-user contacts (DM-tree) ------------------------------

    @app.get("/my/contacts")
    async def my_contacts_list(request: Request):
        s = _session_from_request(request)
        if not s:
            raise HTTPException(401, "not authenticated")
        rows = await db.list_user_contacts(s["username"])
        # Set van pubkeys die de companion kent (via mc.contacts)
        comp_keys = {k.lower() for k in (getattr(mc, "contacts", None) or {}).keys()
                     if isinstance(k, str)}
        return [
            {"pubkey": c.pubkey, "pubkey_prefix": c.pubkey[:12],
             "name": c.name, "notes": c.notes,
             "known_to_companion": c.pubkey.lower() in comp_keys,
             "created_at": c.created_at.astimezone().isoformat() if c.created_at else None}
            for c in rows
        ]

    @app.post("/my/contacts/add")
    async def my_contacts_add(request: Request, payload: dict):
        s = _session_from_request(request)
        if not s:
            raise HTTPException(401, "not authenticated")
        name = (payload.get("name") or "").strip()
        pubkey = (payload.get("pubkey") or "").strip()
        if not name or not pubkey:
            raise HTTPException(400, "name + pubkey vereist")
        try:
            c = await db.add_user_contact(s["username"], pubkey, name,
                                            notes=payload.get("notes"))
        except ValueError as e:
            raise HTTPException(400, str(e))
        return {"ok": True, "name": c.name, "pubkey": c.pubkey,
                "message": f"contact '{c.name}' opgeslagen"}

    @app.post("/my/contacts/remove")
    async def my_contacts_remove(request: Request, payload: dict):
        s = _session_from_request(request)
        if not s:
            raise HTTPException(401, "not authenticated")
        pubkey = (payload.get("pubkey") or "").strip()
        ok = await db.remove_user_contact(s["username"], pubkey)
        return {"ok": ok, "message": "verwijderd" if ok else "niet gevonden"}

    @app.get("/contacts")
    async def contacts_list(request: Request):
        _auth_or_401(request)
        # mc.contacts is een dict van {pubkey_hex: contact_obj}
        contacts = getattr(mc, "contacts", None) or {}
        partners = set(await db.dm_partners())
        items = []
        for pk_hex, c in contacts.items():
            if not isinstance(pk_hex, str) or not pk_hex:
                continue
            ctype = c.get("type") if isinstance(c, dict) else None
            name = c.get("adv_name") if isinstance(c, dict) else None
            items.append({
                "pubkey": pk_hex,
                "pubkey_prefix": pk_hex[:12],
                "name": name or "?",
                "type": ctype,
                "last_advert": c.get("last_advert") if isinstance(c, dict) else None,
                "lat": c.get("adv_lat") if isinstance(c, dict) else None,
                "lon": c.get("adv_lon") if isinstance(c, dict) else None,
                "out_path_len": c.get("out_path_len") if isinstance(c, dict) else None,
                "has_dm_history": (pk_hex[:12] in partners),
            })
        # Sorteer: clients (type=1) eerst, dan op naam
        def _sort_key(x):
            t = x.get("type") if isinstance(x.get("type"), int) else 99
            return (t, (x.get("name") or "").lower())
        items.sort(key=_sort_key)
        return items

    @app.get("/reports/repeaters")
    async def reports_repeaters(request: Request):
        # v1.1.030: repeaters-functionaliteit verplaatst naar Admin-groep in tree;
        # daarom nu admin-only ipv enkel auth-check.
        _admin_or_403(request)
        sess = _session_from_request(request) or {}
        username = sess.get("username") or ""
        favs = set(await db.list_fav_repeaters(username)) if username else set()
        contacts = getattr(mc, "contacts", None) or {}
        items = []
        for pk_hex, c in contacts.items():
            if not isinstance(pk_hex, str) or not pk_hex:
                continue
            ctype = c.get("type") if isinstance(c, dict) else None
            # Filter: alleen repeaters / room-servers (type 2 of 3)
            if ctype not in (2, 3):
                continue
            pk_lower = pk_hex.lower()
            items.append({
                "pubkey": pk_hex,
                "pubkey_prefix": pk_hex[:12],
                "hash_1b":  pk_hex[:2].lower(),
                "hash_2b":  pk_hex[:4].lower(),
                "name": c.get("adv_name") if isinstance(c, dict) else None,
                "type": ctype,
                "type_label": "repeater" if ctype == 2 else "room",
                "last_advert": c.get("last_advert") if isinstance(c, dict) else None,
                "lat": c.get("adv_lat") if isinstance(c, dict) else None,
                "lon": c.get("adv_lon") if isinstance(c, dict) else None,
                "out_path_len": c.get("out_path_len") if isinstance(c, dict) else None,
                "is_favorite": pk_lower in favs,
            })
        # Favorieten eerst, daarna type (clients/repeaters/rooms), dan naam.
        items.sort(key=lambda x: (
            0 if x["is_favorite"] else 1,
            x["type"] or 99,
            (x.get("name") or "").lower(),
        ))
        return {"count": len(items), "repeaters": items}

    @app.get("/reports/repeaters/favorites")
    async def reports_repeaters_favs_list(request: Request):
        _admin_or_403(request)
        sess = _session_from_request(request) or {}
        username = sess.get("username") or ""
        favs = await db.list_fav_repeaters(username) if username else []
        return {"favorites": favs}

    @app.post("/reports/repeaters/favorites")
    async def reports_repeaters_favs_add(request: Request, payload: dict):
        _admin_or_403(request)
        sess = _session_from_request(request) or {}
        username = sess.get("username") or ""
        if not username:
            raise HTTPException(401, "geen sessie")
        pubkey = (payload.get("pubkey") or "").strip().lower()
        if not pubkey:
            raise HTTPException(400, "pubkey vereist")
        try:
            added = await db.add_fav_repeater(username, pubkey)
        except ValueError as e:
            raise HTTPException(400, str(e))
        return {"ok": True, "added": added}

    @app.delete("/reports/repeaters/favorites/{pubkey}")
    async def reports_repeaters_favs_del(request: Request, pubkey: str):
        _admin_or_403(request)
        sess = _session_from_request(request) or {}
        username = sess.get("username") or ""
        if not username:
            raise HTTPException(401, "geen sessie")
        try:
            removed = await db.remove_fav_repeater(username, pubkey)
        except ValueError as e:
            raise HTTPException(400, str(e))
        return {"ok": True, "removed": removed}

    # ---------- Hashtags --------------------------------------------------

    @app.get("/hashtags")
    async def hashtags_list(request: Request):
        _auth_or_401(request)
        rows = await db.list_hashtags()
        return [{"name": h.name, "notes": h.notes} for h in rows]

    @app.post("/hashtags/add")
    async def hashtags_add(request: Request, payload: dict):
        _auth_or_401(request)
        name = (payload.get("name") or "").strip()
        if not name:
            raise HTTPException(400, "name vereist")
        row = await db.add_hashtag(name)
        if row is None:
            raise HTTPException(400, "ongeldige naam")
        return {"ok": True, "name": row.name}

    @app.post("/hashtags/remove")
    async def hashtags_remove(request: Request, payload: dict):
        _auth_or_401(request)
        name = (payload.get("name") or "").strip()
        ok = await db.delete_hashtag(name)
        return {"ok": ok}

    # ---------- Quit ------------------------------------------------------

    # ---------- Bots (admin-only) ---------------------------------------

    def _bot_to_dict(b):
        return {
            "id": b.id, "name": b.name, "description": b.description,
            "channel_idx": b.channel_idx, "keyword": b.keyword,
            "reply": b.reply, "enabled": b.enabled,
            "created_at": b.created_at.astimezone().isoformat() if b.created_at else None,
        }

    @app.get("/admin/bots")
    async def admin_bots_list(request: Request):
        _admin_or_403(request)
        return [_bot_to_dict(b) for b in await db.list_bots()]

    @app.post("/admin/bots/add")
    async def admin_bots_add(request: Request, payload: dict):
        _admin_or_403(request)
        try:
            b = await db.add_bot(
                name=payload.get("name") or "",
                description=payload.get("description"),
                channel_idx=int(payload.get("channel_idx", 0)),
                keyword=payload.get("keyword") or "",
                reply=payload.get("reply") or "",
                enabled=bool(payload.get("enabled", True)),
            )
        except ValueError as e:
            raise HTTPException(400, str(e))
        return {"ok": True, "bot": _bot_to_dict(b),
                "message": f"bot '{b.name}' aangemaakt"}

    @app.post("/admin/bots/update")
    async def admin_bots_update(request: Request, payload: dict):
        _admin_or_403(request)
        try:
            bot_id = int(payload.get("id"))
        except (TypeError, ValueError):
            raise HTTPException(400, "id vereist")
        ok = await db.update_bot(bot_id, **{
            k: payload[k] for k in
            ("name","description","channel_idx","keyword","reply","enabled")
            if k in payload
        })
        if not ok:
            raise HTTPException(404, "bot niet gevonden")
        return {"ok": True, "message": "bijgewerkt"}

    @app.post("/admin/bots/remove")
    async def admin_bots_remove(request: Request, payload: dict):
        _admin_or_403(request)
        try:
            bot_id = int(payload.get("id"))
        except (TypeError, ValueError):
            raise HTTPException(400, "id vereist")
        ok = await db.delete_bot(bot_id)
        return {"ok": ok, "message": "verwijderd" if ok else "niet gevonden"}

    @app.post("/admin/quit")
    async def admin_quit(request: Request):
        _admin_or_403(request)
        # Trigger het main() stop-event — gateway sluit zichzelf netjes af.
        stop_event.set()
        return {"ok": True, "message": "afsluiten"}

    # ---------- Node-voorkeuren (NodePrefs) ------------------------------

    @app.get("/admin/prefs")
    async def admin_prefs_get(request: Request):
        _admin_or_403(request)
        info = await _read_self_info() or {}
        return {
            "manual_add_contacts": bool(info.get("manual_add_contacts")),
            "adv_loc_policy":      info.get("adv_loc_policy"),
            "telemetry_mode_base": info.get("telemetry_mode_base"),
            "telemetry_mode_loc":  info.get("telemetry_mode_loc"),
            "telemetry_mode_env":  info.get("telemetry_mode_env"),
            "multi_acks":          info.get("multi_acks"),
        }

    async def _set_one_pref(setter_name: str, value):
        fn = _resolve_cmd(setter_name)
        if fn is None:
            raise HTTPException(501, f"{setter_name} niet beschikbaar")
        try:
            return await fn(value)
        except Exception as e:  # noqa: BLE001
            raise HTTPException(500, f"{setter_name} faalde: {e}")

    @app.post("/admin/prefs")
    async def admin_prefs_set(request: Request, payload: dict):
        _admin_or_403(request)
        results = []
        # Alleen ingestuurde keys wijzigen
        if "manual_add_contacts" in payload:
            r = await _set_one_pref("set_manual_add_contacts",
                                     bool(payload["manual_add_contacts"]))
            results.append(("manual_add_contacts", str(r)))
        if "adv_loc_policy" in payload:
            r = await _set_one_pref("set_advert_loc_policy",
                                     int(payload["adv_loc_policy"]))
            results.append(("adv_loc_policy", str(r)))
        for k_pref, k_setter in (
            ("telemetry_mode_base", "set_telemetry_mode_base"),
            ("telemetry_mode_loc",  "set_telemetry_mode_loc"),
            ("telemetry_mode_env",  "set_telemetry_mode_env"),
        ):
            if k_pref in payload:
                r = await _set_one_pref(k_setter, int(payload[k_pref]))
                results.append((k_pref, str(r)))
        if "multi_acks" in payload:
            r = await _set_one_pref("set_multi_acks", int(payload["multi_acks"]))
            results.append(("multi_acks", str(r)))
        return {"ok": True, "results": results,
                "message": ", ".join(f"{k}={v}" for k, v in results) or "geen wijzigingen"}

    # ---------- Contact-beheer -------------------------------------------

    @app.post("/contacts/remove")
    async def contacts_remove(request: Request, payload: dict):
        _admin_or_403(request)
        key = (payload.get("key") or "").strip()
        if not key:
            raise HTTPException(400, "key (pubkey hex) vereist")
        fn = _resolve_cmd("remove_contact")
        if fn is None:
            raise HTTPException(501, "remove_contact niet beschikbaar")
        try:
            res = await fn(key)
        except Exception as e:  # noqa: BLE001
            raise HTTPException(500, f"remove_contact faalde: {e}")
        return {"ok": True, "result": str(res), "message": f"contact {key[:12]} verwijderd"}

    # Housekeeping: vergeet stale contacten (geen favoriet) ----------------
    # v1.1.039: generiek — types is een set van 1 (client/companion), 2 (repeater),
    # 3 (room), 4 (sensor). age_secs is leeftijd-drempel; favorieten worden
    # standaard overgeslagen tenzij skip_favorites=False (favorieten gelden
    # alleen voor repeaters/rooms in DB; clients hebben geen 'favoriet'-flag).
    STALE_REPEATER_AGE_SECS = 28 * 86400  # default voor /admin/repeaters/* alias

    _TYPE_LABEL = {1: "client", 2: "repeater", 3: "room", 4: "sensor"}

    def _event_is_ok(ev) -> tuple[bool, str]:
        """Beoordeel het Event dat een companion-commando teruggeeft.

        De meshcore-SDK geeft een `Event` met type OK (`command_ok`) of
        ERROR (`command_error`) terug, en None als er niets binnen de
        time-out kwam. Alleen OK is succes — al het andere is een fout die
        de user te zien moet krijgen, niet stilzwijgend als 'gelukt' tellen.
        """
        if ev is None:
            return False, "geen antwoord van de companion (timeout)"
        ev_type = getattr(ev, "type", None)
        ev_type_str = getattr(ev_type, "value", str(ev_type))
        if ev_type_str == "command_ok":
            return True, ""
        payload = getattr(ev, "payload", None)
        detail = ""
        if isinstance(payload, dict):
            detail = str(payload.get("reason") or payload.get("error") or "").strip()
        elif payload:
            detail = str(payload).strip()
        if ev_type_str == "command_error":
            return False, f"companion weigerde het commando{(': ' + detail) if detail else ''}"
        return False, f"onverwacht antwoord: {ev_type_str}{(' — ' + detail) if detail else ''}"

    async def _refresh_contacts_cache() -> bool:
        """Haal de contactenlijst opnieuw op bij de companion zodat
        `mc.contacts` klopt. Returnt False als het niet lukte (dan is de
        weergave hooguit tot de volgende cache-loop achterhaald)."""
        fn = _resolve_cmd("get_contacts")
        if fn is None:
            return False
        try:
            await asyncio.wait_for(fn(), timeout=8.0)
            return True
        except Exception:  # noqa: BLE001
            return False

    async def _stale_contact_candidates(
        age_secs: int,
        type_set: set,
        skip_favorites: bool = True,
    ):
        """Bouwt de lijst kandidaten op die voldoen aan:
        - `contact.type in type_set`
        - `last_advert` bekend (>0) én `now - last_advert > age_secs`
        - niet in favorieten-lijst (alleen als `skip_favorites=True`)
        Returnt list[dict] met de velden die de UI nodig heeft.
        """
        contacts = getattr(mc, "contacts", None) or {}
        favs = set()
        if skip_favorites:
            favs = await db.all_fav_repeater_pubkeys()
        now_ts = _time.time()
        out = []
        # v1.1.052: tel waaróm een contact afvalt. Een kale "0 kandidaten" is
        # niet te onderscheiden van een bug — met deze telling zie je meteen
        # of het aan het type, de favoriet-vlag, een ontbrekende advert-tijd
        # of gewoon de drempel ligt.
        stats = {
            "contacts_total": 0,
            "skipped_bad_entry": 0,
            "skipped_type": 0,
            "skipped_favorite": 0,
            "skipped_no_advert": 0,
            "skipped_too_recent": 0,
            "newest_age_days": None,
            "oldest_age_days": None,
        }
        ages_in_scope = []
        for pk_hex, c in contacts.items():
            stats["contacts_total"] += 1
            if not isinstance(pk_hex, str) or not pk_hex:
                stats["skipped_bad_entry"] += 1
                continue
            if not isinstance(c, dict):
                stats["skipped_bad_entry"] += 1
                continue
            ctype = c.get("type")
            if ctype not in type_set:
                stats["skipped_type"] += 1
                continue
            if skip_favorites and pk_hex.lower() in favs:
                stats["skipped_favorite"] += 1
                continue
            last_adv = c.get("last_advert")
            if not isinstance(last_adv, (int, float)) or last_adv <= 0:
                # Onbekend → laat staan (veiliger; geen bewijs van staleness)
                stats["skipped_no_advert"] += 1
                continue
            age = now_ts - last_adv
            ages_in_scope.append(age)
            if age <= age_secs:
                stats["skipped_too_recent"] += 1
                continue
            out.append({
                "pubkey": pk_hex,
                "pubkey_prefix": pk_hex[:12],
                "name": c.get("adv_name") or "?",
                "type": ctype,
                "type_label": _TYPE_LABEL.get(ctype, "?"),
                "last_advert": last_adv,
                "age_days": round(age / 86400, 1),
            })
        out.sort(key=lambda x: x["last_advert"])  # oudste eerst
        if ages_in_scope:
            stats["newest_age_days"] = round(min(ages_in_scope) / 86400, 2)
            stats["oldest_age_days"] = round(max(ages_in_scope) / 86400, 2)
        return out, stats

    def _parse_stale_params(
        days_str: Optional[str],
        types_str: Optional[str],
        skip_favs_str: Optional[str],
    ):
        """Helper: parse query/body params naar (age_secs, type_set, skip_favs)."""
        try:
            days = float(days_str) if days_str is not None else 28.0
        except (TypeError, ValueError):
            raise HTTPException(400, f"days moet getal zijn (gaf {days_str!r})")
        if days < 0:
            raise HTTPException(400, "days moet ≥ 0 zijn")
        age_secs = int(days * 86400)

        if types_str is None or types_str == "":
            type_set = {2, 3}  # default: repeaters + rooms (legacy gedrag)
        else:
            try:
                type_set = {int(t) for t in str(types_str).split(",") if t.strip()}
            except ValueError:
                raise HTTPException(400, f"types moet CSV van getallen zijn (gaf {types_str!r})")
            valid = {1, 2, 3, 4}
            if not type_set.issubset(valid):
                raise HTTPException(400, f"types alleen {sorted(valid)} toegestaan")
            if not type_set:
                raise HTTPException(400, "minstens één type vereist")

        if skip_favs_str is None:
            skip_favs = True
        else:
            skip_favs = str(skip_favs_str).lower() in ("1", "true", "yes", "on")

        return age_secs, type_set, skip_favs

    # ---- Generieke endpoints (v1.1.039) --------------------------------
    @app.get("/admin/contacts/stale")
    async def admin_contacts_stale(
        request: Request,
        days: Optional[str] = "28",
        types: Optional[str] = "2,3",
        skip_favorites: Optional[str] = "1",
    ):
        _admin_or_403(request)
        age_secs, type_set, skip_favs = _parse_stale_params(days, types, skip_favorites)
        items, stats = await _stale_contact_candidates(age_secs, type_set, skip_favs)
        return {
            "count": len(items),
            "age_days_threshold": age_secs / 86400,
            "types": sorted(type_set),
            "skip_favorites": skip_favs,
            "items": items,
            "diagnostics": stats,
        }

    @app.post("/admin/contacts/cleanup")
    async def admin_contacts_cleanup(request: Request, payload: dict):
        _admin_or_403(request)
        age_secs, type_set, skip_favs = _parse_stale_params(
            str(payload.get("days")) if payload.get("days") is not None else None,
            payload.get("types"),
            str(payload.get("skip_favorites")) if payload.get("skip_favorites") is not None else None,
        )
        fn = _resolve_cmd("remove_contact")
        if fn is None:
            raise HTTPException(501, "remove_contact niet beschikbaar")
        items, _stats = await _stale_contact_candidates(age_secs, type_set, skip_favs)
        removed, failed = [], []
        for it in items:
            try:
                ev = await fn(it["pubkey"])
            except Exception as e:  # noqa: BLE001
                failed.append({"pubkey_prefix": it["pubkey_prefix"], "name": it["name"],
                               "error": str(e)})
                continue
            # v1.1.051: remove_contact levert een Event op (OK of ERROR), en bij
            # een time-out None. Tot v1.1.050 keken we daar niet naar — elke
            # niet-crashende aanroep telde als "verwijderd". Resultaat: de UI
            # meldde "N verwijderd, 0 mislukt" terwijl de companion het
            # geweigerd had en de contacten gewoon bleven staan.
            ok, why = _event_is_ok(ev)
            if ok:
                removed.append({"pubkey_prefix": it["pubkey_prefix"], "name": it["name"],
                                "type_label": it["type_label"]})
            else:
                failed.append({"pubkey_prefix": it["pubkey_prefix"], "name": it["name"],
                               "error": why})

        # Cache verversen: /reports/repeaters en de kandidatenlijst lezen beide
        # uit mc.contacts. Zonder refresh blijft een succesvol verwijderd
        # contact daar tot de 5-minuten-loop in gateway.py 'm opnieuw ophaalt —
        # wat eruitziet alsof het verwijderen niet gewerkt heeft.
        contacts_refreshed = await _refresh_contacts_cache()

        return {
            "ok": True,
            "removed_count": len(removed),
            "failed_count": len(failed),
            "removed": removed,
            "failed": failed,
            "contacts_refreshed": contacts_refreshed,
            "message": f"{len(removed)} verwijderd, {len(failed)} mislukt",
        }

    # ---- Legacy aliases (v1.1.038 en eerder) ---------------------------
    # /admin/repeaters/stale en /admin/repeaters/cleanup blijven werken met
    # de oude default (28 dagen, repeaters+rooms, skip_favorites).
    @app.get("/admin/repeaters/stale")
    async def admin_repeaters_stale(request: Request):
        _admin_or_403(request)
        items, _stats = await _stale_contact_candidates(STALE_REPEATER_AGE_SECS, {2, 3}, True)
        return {
            "count": len(items),
            "age_days_threshold": STALE_REPEATER_AGE_SECS // 86400,
            "items": items,
        }

    @app.post("/admin/repeaters/cleanup")
    async def admin_repeaters_cleanup(request: Request):
        _admin_or_403(request)
        fn = _resolve_cmd("remove_contact")
        if fn is None:
            raise HTTPException(501, "remove_contact niet beschikbaar")
        items, _stats = await _stale_contact_candidates(STALE_REPEATER_AGE_SECS, {2, 3}, True)
        removed, failed = [], []
        for it in items:
            try:
                ev = await fn(it["pubkey"])
            except Exception as e:  # noqa: BLE001
                failed.append({"pubkey_prefix": it["pubkey_prefix"], "error": str(e)})
                continue
            ok, why = _event_is_ok(ev)   # v1.1.051, zie /admin/contacts/cleanup
            if ok:
                removed.append(it["pubkey_prefix"])
            else:
                failed.append({"pubkey_prefix": it["pubkey_prefix"], "error": why})
        await _refresh_contacts_cache()
        return {
            "ok": True,
            "removed_count": len(removed),
            "failed_count": len(failed),
            "removed": removed,
            "failed": failed,
            "message": f"{len(removed)} verwijderd, {len(failed)} mislukt",
        }

    @app.post("/admin/repeaters/ping")
    async def admin_repeaters_ping(request: Request, payload: dict):
        """Stuur een status-request (ping) naar één repeater/room. Meet
        round-trip en pak SNR-there uit de status-response. SNR-here is
        best-effort: correleert tijdens de ping-window met de all-payload
        RX_LOG ring-buffer uit gateway.py."""
        _admin_or_403(request)
        pubkey = (payload.get("pubkey") or "").strip().lower()
        if not pubkey or len(pubkey) != 64:
            raise HTTPException(400, "geldige 64-char pubkey vereist")
        # Verifieer dat het contact in de companion-contactlijst zit
        contacts = getattr(mc, "contacts", None) or {}
        contact = None
        for pk_hex, c in contacts.items():
            if isinstance(pk_hex, str) and pk_hex.lower() == pubkey:
                contact = c
                pubkey = pk_hex   # gebruik exacte hex zoals companion 'm kent
                break
        if contact is None:
            raise HTTPException(404, "contact onbekend op companion")
        fn = _resolve_cmd("req_status_sync")
        if fn is None:
            raise HTTPException(501, "req_status_sync niet beschikbaar")
        # Lazy-import om circulaire import te vermijden
        import gateway as _gw
        rx_before_len = len(_gw.get_recent_rxlogs_all())
        t0 = _time.monotonic()
        try:
            res = await fn(contact)
        except Exception as e:  # noqa: BLE001
            duration_ms = int((_time.monotonic() - t0) * 1000)
            return {"status": "error", "duration_ms": duration_ms, "error": str(e)}
        t1 = _time.monotonic()
        duration_ms = int((t1 - t0) * 1000)
        if res is None:
            return {"status": "no_response", "duration_ms": duration_ms,
                    "message": f"geen antwoord binnen timeout ({duration_ms} ms)"}
        # SNR-there / RSSI-there komen uit de remote's status-payload
        snr_there  = res.get("last_snr") if isinstance(res, dict) else None
        rssi_there = res.get("last_rssi") if isinstance(res, dict) else None
        # SNR-here: pak het laatst-toegevoegde rxlog-entry dat in deze window kwam
        snr_here  = None
        rssi_here = None
        try:
            all_rx = _gw.get_recent_rxlogs_all()
            newer = all_rx[rx_before_len:]   # entries toegevoegd tijdens ping
            if newer:
                latest = newer[-1]
                snr_here  = latest.get("snr")
                rssi_here = latest.get("rssi")
        except Exception:  # noqa: BLE001
            pass
        return {
            "status": "ok",
            "duration_ms": duration_ms,
            "snr_there":  snr_there,
            "rssi_there": rssi_there,
            "snr_here":   snr_here,
            "rssi_here":  rssi_here,
        }

    # ---------- OTA repeater-management -----------------------------------

    def _get_repeater_contact(pubkey: str):
        """Vind een repeater/room in mc.contacts op basis van pubkey (64-char
        hex). Returnt (contact_dict, full_hex) of (None, None)."""
        pk = (pubkey or "").strip().lower()
        if len(pk) != 64:
            return None, None
        contacts = getattr(mc, "contacts", None) or {}
        for pk_hex, c in contacts.items():
            if isinstance(pk_hex, str) and pk_hex.lower() == pk:
                ctype = c.get("type") if isinstance(c, dict) else None
                if ctype in (2, 3):
                    return c, pk_hex
        return None, None

    def _rep_session_key(request: Request, pubkey: str):
        sess = _session_from_request(request) or {}
        un = sess.get("username") or ""
        return (un, pubkey.lower())

    def _rep_session_alive(request: Request, pubkey: str) -> bool:
        key = _rep_session_key(request, pubkey)
        s = _REPEATER_SESSIONS.get(key)
        if not s:
            return False
        if (_time.time() - s.get("last_activity", 0)) > _REPEATER_SESSION_TTL_SECS:
            _REPEATER_SESSIONS.pop(key, None)
            return False
        return True

    @app.post("/admin/repeaters/login")
    async def admin_repeaters_login(request: Request, payload: dict):
        """Login op een repeater.

        Wachtwoord-bronnen, in volgorde:
          1. `password` uit de payload (de user typt 'm);
          2. het opgeslagen wachtwoord uit de DB, als de payload er geen heeft.

        Met `remember: true` wordt een geslaagd wachtwoord opgeslagen. Het
        wachtwoord gaat nooit terug naar de browser — alleen de vlag of er een
        opgeslagen is. Zie db.RepeaterCredential voor de opslag-afweging.
        """
        _admin_or_403(request)
        pubkey = (payload.get("pubkey") or "").strip().lower()
        password = payload.get("password") or ""
        remember = bool(payload.get("remember"))
        used_saved = False
        if not password:
            saved = await db.get_repeater_password(pubkey)
            if saved:
                password, used_saved = saved, True
            else:
                raise HTTPException(400, "wachtwoord vereist (geen opgeslagen wachtwoord)")
        contact, pk_hex = _get_repeater_contact(pubkey)
        if contact is None:
            raise HTTPException(404, "contact onbekend op companion")
        fn = _resolve_cmd("send_login_sync")
        if fn is None:
            raise HTTPException(501, "send_login_sync niet beschikbaar")
        try:
            ev = await fn(contact, password)
        except Exception as e:  # noqa: BLE001
            raise HTTPException(500, f"login faalde: {e}")
        if ev is None:
            return {"ok": False, "status": "no_response",
                    "used_saved": used_saved,
                    "message": "geen antwoord van repeater (timeout)"}
        ev_type = getattr(ev, "type", None)
        ev_type_str = getattr(ev_type, "value", str(ev_type))
        if ev_type_str == "login_success":
            payload_data = getattr(ev, "payload", {}) or {}
            key = _rep_session_key(request, pk_hex)
            _REPEATER_SESSIONS[key] = {
                "logged_in_at": _time.time(),
                "last_activity": _time.time(),
                "permissions":   payload_data.get("permissions"),
                "is_admin":      payload_data.get("is_admin"),
            }
            if remember and not used_saved:
                sess = _session_from_request(request) or {}
                await db.set_repeater_password(pk_hex, password, sess.get("username") or "")
            return {"ok": True, "status": "logged_in",
                    "is_admin": payload_data.get("is_admin"),
                    "permissions": payload_data.get("permissions"),
                    "used_saved": used_saved,
                    "has_saved_password": await db.has_repeater_password(pk_hex),
                    "message": "ingelogd (opgeslagen wachtwoord)" if used_saved else "ingelogd"}
        # Mislukt. Een opgeslagen wachtwoord dat geweigerd wordt is bijna altijd
        # achterhaald (repeater-pw gewijzigd) — melden, maar niet stilzwijgend
        # wissen: dat zou ook bij een firmware-hik de opslag opruimen.
        return {"ok": False, "status": ev_type_str or "login_failed",
                "used_saved": used_saved,
                "message": ("opgeslagen wachtwoord geweigerd — is het op de repeater gewijzigd?"
                            if used_saved else "login geweigerd (verkeerd wachtwoord?)")}

    @app.post("/admin/repeaters/forget-password")
    async def admin_repeaters_forget_password(request: Request, payload: dict):
        _admin_or_403(request)
        pubkey = (payload.get("pubkey") or "").strip().lower()
        removed = await db.forget_repeater_password(pubkey)
        return {"ok": True, "removed": removed}

    @app.post("/admin/repeaters/logout")
    async def admin_repeaters_logout(request: Request, payload: dict):
        _admin_or_403(request)
        pubkey = (payload.get("pubkey") or "").strip().lower()
        contact, pk_hex = _get_repeater_contact(pubkey)
        key = _rep_session_key(request, pubkey)
        _REPEATER_SESSIONS.pop(key, None)
        if contact is not None:
            fn = _resolve_cmd("send_logout")
            if fn is not None:
                try:
                    await fn(contact)
                except Exception:  # noqa: BLE001
                    pass  # lokale sessie is sowieso al weg
        return {"ok": True, "message": "uitgelogd"}

    @app.get("/admin/repeaters/session")
    async def admin_repeaters_session(request: Request, pubkey: str):
        _admin_or_403(request)
        pubkey = (pubkey or "").strip().lower()
        alive = _rep_session_alive(request, pubkey)
        s = _REPEATER_SESSIONS.get(_rep_session_key(request, pubkey)) or {}
        return {
            "logged_in": alive,
            "logged_in_at": s.get("logged_in_at"),
            "last_activity": s.get("last_activity"),
            "is_admin": s.get("is_admin"),
            "ttl_secs": _REPEATER_SESSION_TTL_SECS,
            # Alleen de vlag — het wachtwoord zelf verlaat de server nooit.
            "has_saved_password": await db.has_repeater_password(pubkey),
        }

    async def _send_cli_and_wait(contact, pk_hex: str, cmd: str, timeout: float = 12.0):
        """Stuur een CLI-commando naar de repeater en wacht op het tekst-antwoord.
        Returnt (text, error_message). Bij time-out: text=None, error='timeout'."""
        from meshcore.events import EventType as _EVT
        send_fn = _resolve_cmd("send_cmd")
        if send_fn is None:
            return None, "send_cmd niet beschikbaar"
        try:
            res = await send_fn(contact, cmd)
        except Exception as e:  # noqa: BLE001
            return None, f"send_cmd faalde: {e}"
        if res is None:
            return None, "geen MSG_SENT bevestiging"
        # Wacht op CONTACT_MSG_RECV van deze repeater (eerste 12 hex chars matchen)
        prefix = pk_hex[:12].lower()
        try:
            ev = await mc.wait_for_event(
                _EVT.CONTACT_MSG_RECV,
                attribute_filters={"pubkey_prefix": prefix},
                timeout=timeout,
            )
        except Exception as e:  # noqa: BLE001
            return None, f"wacht-fout: {e}"
        if ev is None:
            return None, "timeout"
        text = ""
        pl = getattr(ev, "payload", None)
        if isinstance(pl, dict):
            text = pl.get("text") or ""
        return text, None

    @app.post("/admin/repeaters/cmd")
    async def admin_repeaters_cmd(request: Request, payload: dict):
        _admin_or_403(request)
        pubkey = (payload.get("pubkey") or "").strip().lower()
        cmd = (payload.get("cmd") or "").strip()
        if not cmd:
            raise HTTPException(400, "cmd vereist")
        if not _rep_session_alive(request, pubkey):
            return {"ok": False, "status": "not_logged_in",
                    "message": "niet (meer) ingelogd op deze repeater"}
        contact, pk_hex = _get_repeater_contact(pubkey)
        if contact is None:
            raise HTTPException(404, "contact onbekend op companion")
        text, err = await _send_cli_and_wait(contact, pk_hex, cmd)
        # Refresh sessie-activity bij succes
        if text is not None:
            _REPEATER_SESSIONS[_rep_session_key(request, pubkey)]["last_activity"] = _time.time()
            return {"ok": True, "status": "ok", "response": text}
        return {"ok": False, "status": "error", "response": "", "error": err}

    @app.post("/admin/repeaters/manage_status")
    async def admin_repeaters_manage_status(request: Request, payload: dict):
        """Status-overzicht: name (uit contact), bat, uptime, en — indien
        ingelogd — een best-effort 'clock' CLI-call voor de lokale tijd."""
        _admin_or_403(request)
        pubkey = (payload.get("pubkey") or "").strip().lower()
        contact, pk_hex = _get_repeater_contact(pubkey)
        if contact is None:
            raise HTTPException(404, "contact onbekend op companion")
        fn = _resolve_cmd("req_status_sync")
        if fn is None:
            raise HTTPException(501, "req_status_sync niet beschikbaar")
        try:
            res = await fn(contact)
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": str(e)}
        if res is None:
            return {"ok": False, "error": "geen antwoord (timeout)"}
        out = {
            "ok": True,
            "name": contact.get("adv_name") if isinstance(contact, dict) else None,
            "bat":  res.get("bat")    if isinstance(res, dict) else None,
            "uptime": res.get("uptime") if isinstance(res, dict) else None,
            "boot_time": None,
            "remote_clock_text": None,
            "remote_clock_error": None,
        }
        if isinstance(out["uptime"], (int, float)) and out["uptime"] > 0:
            out["boot_time"] = int(_time.time() - out["uptime"])
        if _rep_session_alive(request, pubkey):
            # Best-effort: probeer een paar gangbare CLI-varianten voor de klok
            for cmd in ("clock", "time"):
                text, err = await _send_cli_and_wait(contact, pk_hex, cmd, timeout=8.0)
                if text:
                    out["remote_clock_text"] = text.strip()
                    _REPEATER_SESSIONS[_rep_session_key(request, pubkey)]["last_activity"] = _time.time()
                    break
                else:
                    out["remote_clock_error"] = err
        return out

    @app.get("/contacts/export")
    async def contacts_export(request: Request, key: Optional[str] = None):
        """Geeft een 'meshcore://contact/add?name=...&public_key=...&type=...'
        URI terug — het officiële MeshCore QR-formaat (zie
        https://docs.meshcore.io/qr_codes/). Geen `key` = eigen card.

        v1.1.037: opgewaardeerd van meshcore-py's raw-hex `CONTACT_URI`-event
        naar het officiële URL-formaat dat de Android-app ook gebruikt. We
        bouwen de URL zelf vanuit `get_self_info` (voor eigen card) of
        `mc.contacts` (voor andermans card) — `export_contact` SDK-call is
        niet meer nodig hier.
        """
        _auth_or_401(request)
        from urllib.parse import quote as _urlquote

        name = None
        pubkey_hex = None
        ctype = None

        if key:
            # Andermans card: zoek in mc.contacts (12-char prefix of 64-char volledig).
            contacts = getattr(mc, "contacts", None) or {}
            key_lc = key.lower()
            found = None
            for pk_hex, c in contacts.items():
                if not isinstance(pk_hex, str):
                    continue
                if pk_hex.lower() == key_lc or pk_hex.lower().startswith(key_lc):
                    found = (pk_hex, c)
                    break
            if found is None:
                raise HTTPException(404, f"contact niet gevonden: {key}")
            pk_hex, c = found
            pubkey_hex = pk_hex.lower()
            if isinstance(c, dict):
                name = c.get("adv_name") or c.get("name")
                ctype = c.get("type")
            ctype = ctype if isinstance(ctype, int) else 1
        else:
            # Eigen card.
            info = await _read_self_info() or {}
            name = info.get("name") or info.get("adv_name")
            pubkey_hex = (info.get("public_key") or info.get("pubkey") or "")
            if isinstance(pubkey_hex, (bytes, bytearray)):
                pubkey_hex = pubkey_hex.hex()
            pubkey_hex = (pubkey_hex or "").lower()
            ctype = 1  # eigen node = companion

        if not name or not pubkey_hex:
            log.warning(
                "contacts_export: ontbrekende velden voor card "
                "(name=%r, pubkey_hex_len=%d, key=%r)",
                name, len(pubkey_hex or ""), key,
            )
            raise HTTPException(
                500, "naam of public_key ontbreekt — companion nog niet gereed?"
            )
        if len(pubkey_hex) != 64:
            log.warning(
                "contacts_export: pubkey-lengte %d (verwacht 64) — companion-data "
                "vermoedelijk afwijkend: %r", len(pubkey_hex), pubkey_hex[:16],
            )

        uri = (
            "meshcore://contact/add"
            f"?name={_urlquote(name, safe='')}"
            f"&public_key={pubkey_hex}"
            f"&type={int(ctype)}"
        )
        return {
            "ok": True, "uri": uri,
            "name": name, "public_key": pubkey_hex, "type": ctype,
        }

    @app.post("/contacts/import")
    async def contacts_import(request: Request, payload: dict):
        """Importeer een contact in de companion.

        Twee ondersteunde input-formaten:

        1. Officieel MeshCore URL-formaat (Android-app, v1.1.037+):
             `meshcore://contact/add?name=<...>&public_key=<64hex>&type=<int>`
           → parse de query-params, bouw een minimale contact-dict en gebruik
             `add_contact()` (= `update_contact()` met nieuwe contact).

        2. Legacy raw-hex (v1.1.036 en eerder):
             `meshcore://<rawhex>` of een blote hex-string.
           → `import_contact(bytes.fromhex(...))`. Vereist een meshcore-py-native
             card; werkt alleen tussen onze eigen clients (geen Android-interop).

        Returnt `{ok, name, public_key, type, message}` zodat de client de
        nieuwe contact óók in `/my/contacts` kan opslaan (auto-add-flow in UI).
        """
        _admin_or_403(request)
        raw = (payload.get("uri") or payload.get("card") or "").strip()
        if not raw:
            raise HTTPException(400, "uri of card vereist")

        # --- Formaat 1: officieel URL met query-params ----------------------
        if raw.lower().startswith("meshcore://contact/add"):
            from urllib.parse import urlparse, parse_qs
            try:
                u = urlparse(raw)
                params = parse_qs(u.query, keep_blank_values=False)
            except Exception as e:  # noqa: BLE001
                raise HTTPException(400, f"URL kon niet geparsed worden: {e}")
            name = (params.get("name") or [""])[0]
            pubkey_hex = (params.get("public_key") or [""])[0].lower()
            ctype_raw = (params.get("type") or ["1"])[0]
            if not name:
                raise HTTPException(400, "name ontbreekt in URL")
            if len(pubkey_hex) != 64 or any(c not in "0123456789abcdef" for c in pubkey_hex):
                raise HTTPException(400, f"public_key moet 64 hex chars zijn (gaf {len(pubkey_hex)})")
            try:
                ctype = int(ctype_raw)
            except ValueError:
                raise HTTPException(400, f"type moet integer zijn (gaf {ctype_raw!r})")

            # Minimale contact-dict voor `add_contact()`. out_path_len=-1 betekent
            # "geen pad bekend, flood" — companion zal eerste advert afwachten om
            # routing te leren. Andere velden zijn defaults; companion vult later
            # aan bij ontvangst van adverts van deze pubkey.
            contact = {
                "public_key": pubkey_hex,
                "type": ctype,
                "flags": 0,
                "out_path": "",
                "out_path_len": -1,
                "out_path_hash_mode": 0,
                "adv_name": name,
                "last_advert": 0,
                "adv_lat": 0.0,
                "adv_lon": 0.0,
            }
            fn = _resolve_cmd("add_contact", "update_contact")
            if fn is None:
                raise HTTPException(501, "add_contact niet beschikbaar in deze meshcore versie")
            try:
                res = await fn(contact)
            except Exception as e:  # noqa: BLE001
                log.exception("add_contact faalde")
                raise HTTPException(500, f"add_contact faalde: {e}")
            ev_type = getattr(getattr(res, "type", None), "name", "")
            if ev_type == "ERROR":
                log.warning("add_contact ERROR event: payload=%r", getattr(res, "payload", res))
                raise HTTPException(500, f"companion: {getattr(res, 'payload', 'unknown')}")
            return {
                "ok": True, "name": name, "public_key": pubkey_hex, "type": ctype,
                "message": f"Contact '{name}' geïmporteerd",
            }

        # --- Formaat 2: legacy raw-hex (eventueel met meshcore:// prefix) ---
        hex_card = raw
        if hex_card.lower().startswith("meshcore://"):
            hex_card = hex_card[len("meshcore://"):]
        try:
            card_bytes = bytes.fromhex(hex_card)
        except ValueError:
            raise HTTPException(
                400,
                "geen herkenbaar formaat (verwacht 'meshcore://contact/add?...' of raw hex)",
            )
        fn = _resolve_cmd("import_contact")
        if fn is None:
            raise HTTPException(501, "import_contact niet beschikbaar")
        try:
            res = await fn(card_bytes)
        except Exception as e:  # noqa: BLE001
            log.exception("import_contact faalde")
            raise HTTPException(500, f"import_contact faalde: {e}")
        # Legacy heeft geen name/pubkey-info in de respons — frontend valt
        # voor /my/contacts/add terug op een handmatige naam-prompt.
        return {"ok": True, "result": str(res), "message": "contact geïmporteerd (legacy formaat)"}

    # ---------- Change password (eigen) ----------------------------------

    @app.post("/change-password")
    async def change_password(request: Request, payload: dict):
        s = _session_from_request(request)
        if not s:
            raise HTTPException(401, "not authenticated")
        old = (payload.get("old") or "")
        new = (payload.get("new") or "")
        if len(new) < 6:
            raise HTTPException(400, "nieuw wachtwoord min 6 tekens")
        u = await db.get_user(s["username"])
        if u is None or not verify_password(u.password_hash, old):
            raise HTTPException(401, "huidig wachtwoord onjuist")
        # Nieuwe ww + must_change_password=False
        await db.set_user_password_hash(s["username"], hash_password(new),
                                         must_change_password=False)
        s["must_change"] = False  # update sessie
        return {"ok": True, "message": "wachtwoord bijgewerkt"}

    # ---------- Eigen callsign (self-serve) -------------------------------

    @app.post("/me/callsign")
    async def set_my_callsign(request: Request, payload: dict):
        s = _session_from_request(request)
        if not s:
            raise HTTPException(401, "not authenticated")
        cs = (payload.get("callsign") or "")
        # Strip whitespace, verbied control chars (newline/tab/null), max 16 codepoints.
        cs = cs.strip()
        if any(ord(ch) < 32 for ch in cs):
            raise HTTPException(400, "geen control characters toegestaan")
        if len(cs) > 16:
            raise HTTPException(400, "callsign max 16 tekens (codepoints)")
        ok = await db.set_user_callsign(s["username"], cs)
        if not ok:
            raise HTTPException(404, "user niet gevonden")
        # Sync alle actieve sessies van deze user — zo komt de callsign meteen
        # in de send-handler tevoorschijn zonder her-login.
        for ses in _SESSIONS.values():
            if ses.get("username") == s["username"]:
                ses["callsign"] = cs
        return {"ok": True, "callsign": cs,
                "message": f"callsign {'gewist' if not cs else 'gezet op ' + cs}"}

    # ---------- User-management (admin only) ------------------------------

    @app.get("/admin/users")
    async def admin_users_list(request: Request):
        _admin_or_403(request)
        rows = await db.list_users()
        return [
            {
                "username": u.username,
                "role": u.role,
                "has_password": bool(u.password_hash),
                "allowed_views": json.loads(u.allowed_views or "[]"),
                "last_login": u.last_login.isoformat() if u.last_login else None,
            }
            for u in rows
        ]

    @app.post("/admin/users/add")
    async def admin_users_add(request: Request, payload: dict):
        _admin_or_403(request)
        username = (payload.get("username") or "").strip()
        role = payload.get("role") or "user"
        temp_pw = (payload.get("temp_password") or "")
        if role not in ("user", "admin"):
            raise HTTPException(400, "role moet 'user' of 'admin' zijn")
        if not username:
            raise HTTPException(400, "username vereist")
        if len(temp_pw) < 6:
            raise HTTPException(400, "tijdelijk wachtwoord min 6 tekens")
        try:
            u = await db.add_user(
                username, role=role,
                password_hash=hash_password(temp_pw),
                must_change_password=True,
                allowed_views=["chat", "admin"] if role == "admin" else ["chat"],
            )
        except ValueError as e:
            raise HTTPException(409, str(e))
        return {"ok": True, "username": u.username, "role": u.role,
                "message": f"user '{u.username}' aangemaakt — moet bij eerste login zelf nieuw wachtwoord instellen"}

    @app.post("/admin/users/delete")
    async def admin_users_delete(request: Request, payload: dict):
        _admin_or_403(request)
        username = (payload.get("username") or "").strip()
        s = _session_from_request(request)
        if username == s["username"]:
            raise HTTPException(400, "je kan jezelf niet verwijderen")
        # Voorkom dat de laatste admin verdwijnt
        u = await db.get_user(username)
        if u is None:
            raise HTTPException(404, "user niet gevonden")
        if u.role == "admin" and await db.count_admins() <= 1:
            raise HTTPException(400, "kan laatste admin niet verwijderen")
        await db.delete_user(username)
        # Logout actieve sessies van deze user
        for tok in [t for t, ses in _SESSIONS.items() if ses["username"] == username]:
            _SESSIONS.pop(tok, None)
        return {"ok": True, "message": f"user '{username}' verwijderd"}

    @app.post("/admin/users/reset-password")
    async def admin_users_reset_pw(request: Request, payload: dict):
        _admin_or_403(request)
        username = (payload.get("username") or "").strip()
        temp_pw = (payload.get("temp_password") or "")
        if len(temp_pw) < 6:
            raise HTTPException(400, "tijdelijk wachtwoord min 6 tekens")
        u = await db.get_user(username)
        if u is None:
            raise HTTPException(404, "user niet gevonden")
        await db.reset_user_password(username, hash_password(temp_pw))
        # Forceer logout van actieve sessies van deze user
        for tok in [t for t, ses in _SESSIONS.items() if ses["username"] == username]:
            _SESSIONS.pop(tok, None)
        return {"ok": True, "message": f"tijdelijk ww gezet voor '{username}' — moet bij volgende login zelf nieuw ww kiezen"}

    @app.get("/admin/state")
    async def admin_state(request: Request, fresh: bool = False):
        _auth_or_401(request)
        # Serve uit cache als binnen TTL en geen ?fresh=1.
        now_ts = _time.time()
        cached = _admin_state_cache["data"]
        if (not fresh
                and cached is not None
                and (now_ts - _admin_state_cache["ts"]) < _ADMIN_STATE_CACHE_TTL_SECS):
            return cached
        info = await _read_self_info()
        battery_mv = await _read_battery()
        dev = await _read_device_info()
        status = await _read_node_status()
        rows = await db.list_channels()
        count = await db.count_messages()

        up_s = int(_time.time() - started_at)
        h, rem = divmod(up_s, 3600); m, s = divmod(rem, 60)
        gw_uptime = f"{h}h {m}m {s}s"

        # Firmware-string netjes maken: 'ver' is doorgaans iets als '1.13.5',
        # 'fw_build' is bv 'Mar 12 2026'.
        fw_ver = (dev.get("ver") or "").strip() or None
        fw_build = (dev.get("fw_build") or "").strip() or None
        firmware = " ".join(x for x in (fw_ver, f"({fw_build})" if fw_build else None) if x) or None

        # STATS_CORE-payload heeft 'uptime_secs' (en 'battery_mv'); fallback
        # naar oudere veldnamen voor andere firmware-versies.
        node_uptime_s = None
        if isinstance(status, dict):
            node_uptime_s = (status.get("uptime_secs")
                             or status.get("uptime")
                             or None)
        node_uptime = _fmt_uptime(node_uptime_s) if node_uptime_s is not None else None
        # Batterij: stats_core heeft 'battery_mv'; anders fallback op get_bat
        battery_mv_eff = None
        if isinstance(status, dict) and status.get("battery_mv") is not None:
            battery_mv_eff = status.get("battery_mv")
        elif isinstance(battery_mv, (int, float)):
            battery_mv_eff = battery_mv
        battery_v = round(battery_mv_eff / 1000.0, 3) if isinstance(battery_mv_eff, (int, float)) else None

        payload = {
            "node": {
                "name": info.get("name") if isinstance(info, dict) else None,
                "pubkey": getattr(gateway_state, "self_pubkey", None),
                "battery_mv": battery_mv_eff,
                "battery_v":  battery_v,
                "uptime_gw":  gw_uptime,
                "uptime_node": node_uptime,
                "model":      (dev.get("model") or "").strip() or None,
                "firmware":   firmware,
                "fw_ver":     fw_ver,
                "fw_build":   fw_build,
                "path_hash_mode": dev.get("path_hash_mode"),
                "queue_len":  status.get("queue_len") if isinstance(status, dict) else None,
                "errors":     status.get("errors")    if isinstance(status, dict) else None,
            },
            "radio": {
                "freq": info.get("radio_freq") if isinstance(info, dict) else None,
                "bw":   info.get("radio_bw")   if isinstance(info, dict) else None,
                "sf":   info.get("radio_sf")   if isinstance(info, dict) else None,
                "cr":   info.get("radio_cr")   if isinstance(info, dict) else None,
                "tx_power":     info.get("tx_power")     if isinstance(info, dict) else None,
                "max_tx_power": info.get("max_tx_power") if isinstance(info, dict) else None,
                "lat": info.get("adv_lat") if isinstance(info, dict) else None,
                "lon": info.get("adv_lon") if isinstance(info, dict) else None,
                "noise_floor":  status.get("noise_floor") if isinstance(status, dict) else None,
                "last_rssi":    status.get("last_rssi")   if isinstance(status, dict) else None,
                "last_snr":     status.get("last_snr")    if isinstance(status, dict) else None,
            },
            "channels": [
                {"idx": c.idx, "name": c.name, "alias": c.alias,
                 "has_key": c.has_key, "is_public": c.is_public,
                 "kind": c.kind, "scope": c.scope}
                for c in rows
            ],
            "db": {"count": count},
        }
        _admin_state_cache["data"] = payload
        _admin_state_cache["ts"] = _time.time()
        return payload

    @app.post("/admin/radio")
    async def admin_radio(request: Request, payload: dict):
        _auth_or_401(request)
        try:
            freq = float(payload["freq"]); bw = float(payload["bw"])
            sf = int(payload["sf"]); cr = int(payload["cr"])
        except (KeyError, TypeError, ValueError):
            raise HTTPException(400, "freq/bw/sf/cr vereist")
        fn = _resolve_cmd("set_radio", "set_radio_params")
        if fn is None:
            raise HTTPException(501, "set_radio niet beschikbaar")
        try:
            res = await fn(freq, bw, sf, cr)
        except Exception as e:  # noqa: BLE001
            raise HTTPException(500, f"set_radio faalde: {e}")
        return {"ok": True, "result": str(res), "message": "radio gezet — reboot vereist"}

    @app.get("/admin/radio/default-scope")
    async def admin_get_default_scope(request: Request):
        """Lees de companion-wide default flood-scope.
        Per-kanaal scope (zie /admin/channels/scope) overrulet deze. Bron is
        de companion zelf (SDK: get_default_flood_scope); we tonen ook de
        gateway-cache zodat de UI bij read-faal toch een ge-cachte waarde ziet."""
        _admin_or_403(request)
        cached = getattr(gateway_state, "default_scope", None)
        fn = _resolve_cmd("get_default_flood_scope")
        if fn is None:
            return {"ok": True, "supported": False,
                    "scope_name": cached or "",
                    "message": "firmware/SDK kent geen get_default_flood_scope"}
        try:
            ev = await asyncio.wait_for(fn(), timeout=2.0)
        except Exception as e:  # noqa: BLE001
            return {"ok": True, "supported": True,
                    "scope_name": cached or "",
                    "error": f"read faalde: {e}"}
        payload = getattr(ev, "payload", ev) if not isinstance(ev, dict) else ev
        name = ((payload or {}).get("scope_name") or "").strip()
        gateway_state.default_scope = name or None
        return {"ok": True, "supported": True,
                "scope_name": name,
                "scope_key": (payload or {}).get("scope_key", "")}

    @app.post("/admin/radio/default-scope")
    async def admin_set_default_scope(request: Request, payload: dict):
        """Zet de companion-wide default flood-scope. Leeg = uit.
        Updatet meteen de gateway-cache (state.default_scope) zodat
        _apply_channel_scope direct met de nieuwe default werkt."""
        _admin_or_403(request)
        scope_raw = (payload.get("scope") or "").strip()
        scope = scope_raw if scope_raw else None
        fn = _resolve_cmd("set_default_flood_scope")
        if fn is None:
            raise HTTPException(501, "firmware/SDK kent geen set_default_flood_scope")
        try:
            res = await fn(scope)
        except Exception as e:  # noqa: BLE001
            raise HTTPException(500, f"set_default_flood_scope faalde: {e}")
        gateway_state.default_scope = scope
        # last_scope-cache invalideren zodat de eerstvolgende send opnieuw
        # set_flood_scope aanroept (effectief = de nieuwe default of de
        # eigen channel-scope).
        gateway_state.last_scope = "__unset__"
        return {"ok": True, "result": str(res),
                "scope_name": scope or "",
                "message": f"default scope → {scope or '(geen)'}"}

    @app.post("/admin/txpower")
    async def admin_txpower(request: Request, payload: dict):
        _auth_or_401(request)
        try:
            dbm = int(payload["dbm"])
        except (KeyError, TypeError, ValueError):
            raise HTTPException(400, "dbm vereist")
        fn = _resolve_cmd("set_tx_power", "set_txpower")
        if fn is None:
            raise HTTPException(501, "set_tx_power niet beschikbaar")
        try:
            res = await fn(dbm)
        except Exception as e:  # noqa: BLE001
            raise HTTPException(500, f"set_tx_power faalde: {e}")
        return {"ok": True, "result": str(res), "message": f"tx_power → {dbm} dBm — reboot vereist"}

    @app.post("/admin/name")
    async def admin_name(request: Request, payload: dict):
        _auth_or_401(request)
        name = (payload.get("name") or "").strip()
        if not name:
            raise HTTPException(400, "name vereist")
        fn = _resolve_cmd("set_name", "set_node_name")
        if fn is None:
            raise HTTPException(501, "set_name niet beschikbaar")
        try:
            res = await fn(name)
        except Exception as e:  # noqa: BLE001
            raise HTTPException(500, f"set_name faalde: {e}")
        gateway_state.self_name = name
        return {"ok": True, "result": str(res), "message": f"naam → {name}"}

    @app.post("/admin/coords")
    async def admin_coords(request: Request, payload: dict):
        _auth_or_401(request)
        if payload.get("clear"):
            lat, lon = 0.0, 0.0
        else:
            try:
                lat = float(payload["lat"]); lon = float(payload["lon"])
            except (KeyError, TypeError, ValueError):
                raise HTTPException(400, "lat/lon of clear vereist")
        fn = _resolve_cmd("set_coords", "set_node_coords")
        if fn is None:
            raise HTTPException(501, "set_coords niet beschikbaar")
        try:
            res = await fn(lat, lon)
        except Exception as e:  # noqa: BLE001
            raise HTTPException(500, f"set_coords faalde: {e}")
        return {"ok": True, "result": str(res), "message": f"coords → {lat},{lon}"}

    @app.post("/admin/path-hash-mode")
    async def admin_path_hash_mode(request: Request, payload: dict):
        _admin_or_403(request)
        try:
            mode = int(payload["mode"])
        except (KeyError, TypeError, ValueError):
            raise HTTPException(400, "mode (0-3) vereist")
        if mode < 0 or mode > 3:
            raise HTTPException(400, "mode moet 0-3 zijn")
        fn = _resolve_cmd("set_path_hash_mode")
        if fn is None:
            raise HTTPException(501, "set_path_hash_mode niet beschikbaar")
        try:
            res = await fn(mode)
        except Exception as e:  # noqa: BLE001
            raise HTTPException(500, f"set_path_hash_mode faalde: {e}")
        return {"ok": True, "result": str(res),
                "message": f"path-hash-mode → {mode} (= {mode+1}-byte hashes)"}

    @app.post("/admin/channels/scope")
    async def admin_channel_scope(request: Request, payload: dict):
        _admin_or_403(request)
        try:
            slot = int(payload["slot"])
        except (KeyError, TypeError, ValueError):
            raise HTTPException(400, "slot vereist")
        scope_raw = (payload.get("scope") or "").strip()
        scope = scope_raw if scope_raw else None
        ok = await db.set_channel_scope(slot, scope)
        if not ok:
            raise HTTPException(404, "channel niet gevonden")
        return {"ok": True, "scope": scope,
                "message": f"slot {slot} scope → {scope or '(geen)'}"}

    @app.post("/admin/advert")
    async def admin_advert(request: Request, payload: dict):
        _admin_or_403(request)
        flood = bool(payload.get("flood", False))
        fn = _resolve_cmd("send_advert")
        if fn is None:
            raise HTTPException(501, "send_advert niet beschikbaar")
        try:
            res = await fn(flood)
        except Exception as e:  # noqa: BLE001
            raise HTTPException(500, f"send_advert faalde: {e}")
        kind = "flood" if flood else "zero-hop"
        return {"ok": True, "result": str(res), "message": f"advert verzonden ({kind})"}

    @app.post("/admin/reboot")
    async def admin_reboot(request: Request):
        _auth_or_401(request)
        fn = _resolve_cmd("reboot", "reset")
        if fn is None:
            raise HTTPException(501, "reboot niet beschikbaar")
        try:
            res = await fn()
        except Exception as e:  # noqa: BLE001
            raise HTTPException(500, f"reboot faalde: {e}")
        return {"ok": True, "result": str(res), "message": "reboot gestuurd"}

    # Hoe meshcore-py omgaat met channel-keys (uit commands/device.py):
    #
    #   if channel_name.startswith("#") or channel_secret is None:
    #       channel_secret = sha256(channel_name.encode("utf-8")).digest()[0:16]
    #
    # Dat betekent: voor "hashtag"-channels geven we de naam mét '#'-prefix
    # en GEEN secret door — iedereen die '#gezin' op een slot zet krijgt
    # dezelfde key (sha256-derived). Voor "private"-channels geven we een
    # eigen 16-byte secret mee.
    PRIVATE_KEY_BYTES = 16   # AES-128 (companion accepteert exact 16 bytes)

    def _next_free_slot(channels) -> Optional[int]:
        used = {c.idx for c in channels}
        for s in range(1, 8):
            if s not in used:
                return s
        return None

    async def _set_channel_on_node(slot: int, name: str, key):
        """key=None → meshcore-py derived 'm uit sha256(name) (gebruikt voor hashtag)."""
        fn = _resolve_cmd("set_channel", "add_channel")
        if fn is None:
            raise HTTPException(501, "set_channel niet beschikbaar in deze meshcore versie")
        try:
            if key is None:
                return await fn(slot, name)
            return await fn(slot, name, key)
        except Exception as e:  # noqa: BLE001
            raise HTTPException(500, f"set_channel faalde: {e}")

    @app.post("/admin/channels/add")
    async def admin_channels_add(request: Request, payload: dict):
        """Generieke channel-add. Verwacht {kind, name, slot?, key?}."""
        _auth_or_401(request)
        kind = (payload.get("kind") or "private").strip().lower()
        if kind not in ("hashtag", "private"):
            raise HTTPException(400, "kind moet 'hashtag' of 'private' zijn")
        name = (payload.get("name") or "").strip()
        if not name:
            raise HTTPException(400, "name vereist")
        # Slot: meegegeven of auto-pick
        existing = await db.list_channels()
        if "slot" in payload and payload["slot"] is not None and payload["slot"] != "":
            try:
                slot = int(payload["slot"])
            except (TypeError, ValueError):
                raise HTTPException(400, "slot moet getal zijn")
            if slot < 1 or slot > 7:
                raise HTTPException(400, "slot moet 1-7 zijn")
        else:
            slot = _next_free_slot(existing)
            if slot is None:
                raise HTTPException(409, "alle slots 1-7 zijn in gebruik")

        # Sleutel-keuze
        generated_key = None
        if kind == "hashtag":
            # Naam moet met '#' beginnen — de library trigget daarop voor
            # name-derived (sha256) key. Iedereen met dezelfde tag-naam krijgt
            # dezelfde key automatisch.
            if not name.startswith("#"):
                name = "#" + name
            key = None  # library berekent zelf
        else:  # private
            key_hex = (payload.get("key") or "").strip()
            if key_hex:
                try:
                    key = bytes.fromhex(key_hex)
                except ValueError:
                    raise HTTPException(400, "ongeldige hex-key")
                if len(key) != PRIVATE_KEY_BYTES:
                    raise HTTPException(400,
                        f"private-key moet {PRIVATE_KEY_BYTES} bytes "
                        f"({PRIVATE_KEY_BYTES*2} hex) zijn, kreeg {len(key)}")
            else:
                key = secrets.token_bytes(PRIVATE_KEY_BYTES)
                generated_key = key.hex()

        # Hardware sync: configureer slot op companion
        res = await _set_channel_on_node(slot, name, key)

        # DB-spiegel
        await db.upsert_channel(
            slot, name=name, has_key=True, is_public=False, kind=kind,
        )

        msg = f"slot {slot} = {name} ({kind})"
        return {
            "ok": True,
            "slot": slot,
            "kind": kind,
            "name": name,
            "result": str(res),
            "message": msg,
            "generated_key": generated_key,
        }

    @app.post("/admin/channels/remove")
    async def admin_channels_remove(request: Request, payload: dict):
        _auth_or_401(request)
        try:
            slot = int(payload["slot"])
        except (KeyError, TypeError, ValueError):
            raise HTTPException(400, "slot vereist")
        if slot == 0:
            raise HTTPException(400, "Public channel kan niet verwijderd worden")
        ok = await db.delete_channel(slot)
        return {"ok": ok, "message": "verwijderd uit DB-metadata" if ok else "niet gevonden"}

    # ---------- Channel QR import/export (v1.1.037) ----------------------
    # Officieel formaat (docs.meshcore.io/qr_codes/):
    #   meshcore://channel/add?name=<urlencoded>&secret=<32hex>
    # Geldt zowel voor de Android-app als voor onze web-client.
    @app.get("/admin/channels/{idx}/export")
    async def admin_channel_export(request: Request, idx: int):
        """Bouw 'meshcore://channel/add?name=...&secret=<32hex>' voor private slot."""
        _admin_or_403(request)
        if idx < 1 or idx > 7:
            raise HTTPException(400, "slot moet 1-7 zijn (slot 0 = Public, geen secret)")
        fn = _resolve_cmd("get_channel")
        if fn is None:
            raise HTTPException(501, "get_channel niet beschikbaar")
        try:
            ev = await fn(idx)
        except Exception as e:  # noqa: BLE001
            log.exception("get_channel faalde")
            raise HTTPException(500, f"get_channel faalde: {e}")
        ev_type = getattr(getattr(ev, "type", None), "name", "")
        if ev_type == "ERROR":
            log.warning("get_channel ERROR event: payload=%r", getattr(ev, "payload", ev))
            raise HTTPException(500, "companion gaf ERROR")
        payload = getattr(ev, "payload", ev)
        if not isinstance(payload, dict):
            raise HTTPException(500, f"onverwachte payload-type: {type(payload).__name__}")
        name = payload.get("channel_name") or ""
        secret = payload.get("channel_secret") or b""
        if isinstance(secret, (bytes, bytearray)):
            secret_hex = secret.hex()
        else:
            secret_hex = str(secret)
        if not name or len(secret_hex) != 32:
            raise HTTPException(500, f"channel-data incomplete (name={name!r}, secret_len={len(secret_hex)})")
        from urllib.parse import quote as _urlquote
        uri = (
            "meshcore://channel/add"
            f"?name={_urlquote(name, safe='')}"
            f"&secret={secret_hex}"
        )
        return {"ok": True, "uri": uri, "slot": idx, "name": name, "secret": secret_hex}

    @app.post("/admin/channels/import")
    async def admin_channels_import(request: Request, payload: dict):
        """Importeer een channel via 'meshcore://channel/add?name=...&secret=<32hex>' URI.

        Vindt het eerst beschikbare slot (1-7), gebruikt `set_channel`, en
        spiegelt naar DB als `private`. Faalt met 409 als alle slots vol zijn.
        """
        _admin_or_403(request)
        raw = (payload.get("uri") or "").strip()
        if not raw:
            raise HTTPException(400, "uri vereist")
        if not raw.lower().startswith("meshcore://channel/add"):
            raise HTTPException(400, "uri moet 'meshcore://channel/add?...' zijn")
        from urllib.parse import urlparse, parse_qs
        try:
            u = urlparse(raw)
            params = parse_qs(u.query, keep_blank_values=False)
        except Exception as e:  # noqa: BLE001
            raise HTTPException(400, f"URL parse faalde: {e}")
        name = (params.get("name") or [""])[0]
        secret_hex = (params.get("secret") or [""])[0].lower()
        if not name:
            raise HTTPException(400, "name ontbreekt in URL")
        if len(secret_hex) != 32 or any(c not in "0123456789abcdef" for c in secret_hex):
            raise HTTPException(400, f"secret moet 32 hex chars (16 bytes) zijn (gaf {len(secret_hex)})")
        try:
            secret_bytes = bytes.fromhex(secret_hex)
        except ValueError:
            raise HTTPException(400, "secret is geen geldige hex")

        # Voorkom dubbele import (zelfde secret op een ander slot zou werken maar
        # spamt slots vol — check eerst).
        existing = await db.list_channels()
        slot = _next_free_slot(existing)
        if slot is None:
            raise HTTPException(409, "alle slots 1-7 zijn in gebruik — verwijder eerst een kanaal")

        # Zet op companion
        try:
            res = await _set_channel_on_node(slot, name, secret_bytes)
        except HTTPException:
            raise
        except Exception as e:  # noqa: BLE001
            log.exception("set_channel faalde tijdens channel-import")
            raise HTTPException(500, f"set_channel faalde: {e}")
        # DB-spiegel: altijd 'private' bij import (alleen private hebben gedeelde
        # secret-key; hashtag is naam-derived en heeft geen QR-flow nodig).
        await db.upsert_channel(
            slot, name=name, has_key=True, is_public=False, kind="private",
        )
        return {
            "ok": True, "slot": slot, "name": name,
            "secret": secret_hex,
            "result": str(res),
            "message": f"Privé-kanaal '{name}' geïmporteerd op slot {slot}",
        }

    @app.get("/admin/clean/preview")
    async def admin_clean_preview(request: Request, seconds: int):
        _auth_or_401(request)
        return {"count": await db.count_messages_older_than(seconds)}

    @app.post("/admin/clean")
    async def admin_clean(request: Request, payload: dict):
        _auth_or_401(request)
        mode = payload.get("mode")
        if mode == "all":
            n = await db.delete_all_messages()
            return {"ok": True, "deleted": n, "message": f"{n} berichten verwijderd"}
        if mode == "older":
            try:
                seconds = int(payload["seconds"])
            except (KeyError, TypeError, ValueError):
                raise HTTPException(400, "seconds vereist")
            n = await db.delete_messages_older_than(seconds)
            return {"ok": True, "deleted": n, "message": f"{n} berichten verwijderd"}
        raise HTTPException(400, "mode moet 'all' of 'older' zijn")

    @app.post("/admin/vacuum")
    async def admin_vacuum(request: Request):
        _auth_or_401(request)
        try:
            await db.vacuum()
        except Exception as e:  # noqa: BLE001
            raise HTTPException(500, f"vacuum faalde: {e}")
        return {"ok": True, "message": "VACUUM klaar"}

    # ---------- Socket.IO --------------------------------------------------

    @sio.event
    async def connect(sid, environ, auth=None):
        if not _is_authed_environ(environ):
            raise socketio.exceptions.ConnectionRefusedError("auth")
        # Initiële history wordt door de client zelf opgehaald via REST
        # (per channel/hashtag-view), dus hier alleen auth.

    @sio.event
    async def send(sid, data):
        data = data or {}
        text = (data.get("text") or "").strip()
        if not text:
            return {"ok": False, "err": "leeg bericht"}
        # Threading: optionele parent_id (id van msg waarop dit een reply is)
        parent_id = data.get("parent_id")
        try:
            parent_id = int(parent_id) if parent_id is not None else None
        except (TypeError, ValueError):
            parent_id = None
        # Callsign-prefix: '[XXX] tekst' als de user een callsign heeft gezet.
        # Achterhaal sessie via sio environ (cookie) — sid is anoniem.
        try:
            environ = sio.get_environ(sid) or {}
        except Exception:  # noqa: BLE001
            environ = {}
        sess = _session_from_environ(environ) or {}
        cs = (sess.get("callsign") or "").strip()
        if cs:
            text = f"[{cs}] {text}"
        try:
            if data.get("kind") == "dm":
                peer = (data.get("peer") or "").strip()
                if not peer:
                    return {"ok": False, "err": "peer vereist"}
                # Pre-check: is deze contact bekend bij de companion?
                comp_keys = {k.lower() for k in (getattr(mc, "contacts", None) or {}).keys()
                             if isinstance(k, str)}
                if not any(k.startswith(peer.lower()) for k in comp_keys):
                    return {
                        "ok": False,
                        "err": "Companion kent deze contact niet — wacht op een advert van die node, "
                               "of zet 'auto-add adverts' aan in Voorkeuren."
                    }
                ok = await send_dm_fn(peer, text, parent_id=parent_id)
            else:
                idx = int(data.get("channel_idx", 0))
                ok = await send_channel(idx, text, parent_id=parent_id)
            return {"ok": bool(ok)}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "err": str(e)}

    # ---------- Dispatch hook: broadcast inkomende én uitgaande ----------

    async def web_handler(msg) -> None:
        # Channels én DM's worden gebroadcast naar alle web-clients
        if msg.kind not in ("channel", "dm"):
            return
        # raw kan een dict (incoming) of een string (outgoing ack) zijn —
        # converteer naar iets dat de client kan parsen.
        raw_out = None
        if msg.raw is not None:
            if isinstance(msg.raw, dict):
                raw_out = msg.raw
            else:
                raw_out = str(msg.raw)
        from datetime import datetime as _dt, timezone as _tz
        await sio.emit(
            "msg",
            {
                "id": getattr(msg, "db_id", None),
                "ts": _dt.now(_tz.utc).isoformat(),
                "direction": msg.direction,
                "peer": msg.sender or ("self" if msg.direction == "out" else "?"),
                "text": msg.text,
                "kind": msg.kind,
                "channel_idx": msg.channel_idx,
                "raw": raw_out,
                "ack_status": getattr(msg, "ack_status", None),
                "expected_ack": getattr(msg, "expected_ack", None),
                "parent_id": getattr(msg, "parent_id", None),
            },
        )

    dispatch_obj.register("channel", web_handler)
    dispatch_obj.register("dm", web_handler)

    async def web_update_handler(payload: dict) -> None:
        # Broadcast ack/status-update naar alle verbonden web-clients
        await sio.emit("msg-update", payload)

    if hasattr(dispatch_obj, "register_update"):
        dispatch_obj.register_update(web_update_handler)

    asgi = socketio.ASGIApp(sio, other_asgi_app=app)
    return asgi, sio


# ---------------------------------------------------------------------------
# Server runner — gebruikt vanuit gateway.py main()
# ---------------------------------------------------------------------------

async def serve(asgi_app, host: str, port: int, stop: asyncio.Event) -> None:
    """Run uvicorn tot stop-event gezet wordt.

    We cancellen serve_task NIET; we vragen uvicorn netjes om af te sluiten
    via should_exit en wachten tot 'ie zelf returnt. Force_exit alleen als
    uvicorn na 10s nog niet klaar is.
    """
    config = uvicorn.Config(
        asgi_app,
        host=host,
        port=port,
        log_level="warning",
        access_log=False,
    )
    server = uvicorn.Server(config)
    serve_task = asyncio.create_task(server.serve())
    stop_task = asyncio.create_task(stop.wait())
    try:
        await asyncio.wait(
            {serve_task, stop_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
    finally:
        if not stop_task.done():
            stop_task.cancel()
        if not serve_task.done():
            server.should_exit = True
            try:
                await asyncio.wait_for(serve_task, timeout=10.0)
            except asyncio.TimeoutError:
                server.force_exit = True
                try:
                    await asyncio.wait_for(serve_task, timeout=2.0)
                except (asyncio.TimeoutError, Exception):  # noqa: BLE001
                    pass
