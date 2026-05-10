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
import os
import secrets
from typing import Awaitable, Callable, Optional

import socketio
import uvicorn
from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

import db


# ---------------------------------------------------------------------------
# Config / state
# ---------------------------------------------------------------------------

# Sessies: token → {username, role, allowed_views, login_time}
# In-memory; bij gateway-restart moet iedereen opnieuw inloggen.
_SESSIONS: dict[str, dict] = {}
COOKIE_NAME = "mc_auth"


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
    }
    return token


def _session(token: Optional[str]) -> Optional[dict]:
    if not token:
        return None
    return _SESSIONS.get(token)


def _session_from_request(request: Request) -> Optional[dict]:
    return _session(request.cookies.get(COOKIE_NAME))


def _is_authed_request(request: Request) -> bool:
    return _session_from_request(request) is not None


def _is_admin_request(request: Request) -> bool:
    s = _session_from_request(request)
    return bool(s and s.get("role") == "admin")


def _is_authed_environ(environ: dict) -> bool:
    raw_cookie = environ.get("HTTP_COOKIE", "")
    cookies = {}
    for part in raw_cookie.split(";"):
        if "=" in part:
            k, v = part.strip().split("=", 1)
            cookies[k] = v
    return cookies.get(COOKIE_NAME) in _SESSIONS


# ---------------------------------------------------------------------------
# HTML (inline — geen template engine nodig voor MVP)
# ---------------------------------------------------------------------------

LOGIN_HTML = """<!DOCTYPE html>
<html lang="nl"><head>
<meta charset="utf-8">
<title>MeshCore Gateway — Login</title>
<style>
  body{font-family:system-ui,sans-serif;max-width:400px;margin:5em auto;padding:1em;background:#f6f6f6}
  h2{margin-top:0}
  form{background:#fff;padding:1.5em;border-radius:8px;box-shadow:0 1px 3px rgba(0,0,0,0.1)}
  label{display:block;margin-bottom:0.5em;font-size:0.9em;color:#444}
  input,button{display:block;width:100%;padding:10px;margin-top:6px;font:inherit;box-sizing:border-box;border-radius:4px;border:1px solid #ccc}
  button{margin-top:1em;background:#2c5;color:#fff;border:0;font-weight:600;cursor:pointer}
  button:hover{background:#1b4}
  .err{color:#c33;padding:8px 0;font-size:0.9em}
</style></head>
<body>
<h2>MeshCore Gateway</h2>
<form method="POST" action="/login">
  <label>Gebruikersnaam<input type="text" name="username" autofocus required autocomplete="username"></label>
  <label>Wachtwoord<input type="password" name="password" required autocomplete="current-password"></label>
  <button type="submit">Login</button>
  {err}
</form>
</body></html>"""


SETUP_HTML = """<!DOCTYPE html>
<html lang="nl"><head>
<meta charset="utf-8"><title>MeshCore Gateway — Setup</title>
<style>
  body{font-family:system-ui,sans-serif;max-width:420px;margin:5em auto;padding:1em;background:#f6f6f6}
  h2{margin-top:0}p.intro{color:#555;font-size:0.9em}
  form{background:#fff;padding:1.5em;border-radius:8px;box-shadow:0 1px 3px rgba(0,0,0,0.1)}
  label{display:block;margin-bottom:0.5em;font-size:0.9em;color:#444}
  input,button{display:block;width:100%;padding:10px;margin-top:6px;font:inherit;box-sizing:border-box;border-radius:4px;border:1px solid #ccc}
  button{margin-top:1em;background:#2c5;color:#fff;border:0;font-weight:600;cursor:pointer}
  button:hover{background:#1b4}
  .err{color:#c33;padding:8px 0;font-size:0.9em}
</style></head>
<body>
<h2>Eerste opzet</h2>
<p class="intro">Maak de eerste admin-gebruiker aan.</p>
<form method="POST" action="/setup">
  <label>Gebruikersnaam<input type="text" name="username" autofocus required minlength="2"></label>
  <label>Wachtwoord<input type="password" name="password" required minlength="6"></label>
  <label>Wachtwoord (nogmaals)<input type="password" name="password2" required minlength="6"></label>
  <button type="submit">Aanmaken</button>
  {err}
</form>
</body></html>"""


# FIRSTLOGIN_HTML verwijderd — geforceerde wijziging gebeurt via in-app modal
# in APP_HTML zelf (forced change-password als must_change=true).

APP_HTML = """<!DOCTYPE html>
<html lang="nl"><head>
<meta charset="utf-8">
<title>MeshCore Gateway</title>
<style>
  *{box-sizing:border-box}
  html,body{height:100%}
  body{font-family:system-ui,sans-serif;margin:0;background:#fafafa;color:#222;display:flex;flex-direction:column;height:100vh;overflow:hidden}

  /* ---- header --------------------------------------------------- */
  header{height:44px;flex:0 0 44px;background:#2c5;color:#fff;display:flex;align-items:center;justify-content:space-between;padding:0 14px;font-weight:600}
  header .title{font-size:0.95em}
  .hdr-status{display:flex;gap:14px;align-items:center;font-size:0.85em;font-weight:400;opacity:0.95}
  .hdr-status .item{display:flex;align-items:center;gap:5px;white-space:nowrap}
  .hdr-status .lbl{opacity:0.75;font-size:0.85em}
  .hdr-status .bat-good{color:#cfc}
  .hdr-status .bat-low{color:#fc6}
  .hdr-status .bat-crit{color:#fbb}
  .avatar{width:30px;height:30px;border-radius:50%;background:#1b4;display:flex;align-items:center;justify-content:center;cursor:pointer;position:relative}
  .avatar svg{width:18px;height:18px;fill:#fff}
  .menu{position:absolute;top:36px;right:0;background:#fff;color:#222;border:1px solid #ddd;border-radius:4px;box-shadow:0 2px 8px rgba(0,0,0,0.15);min-width:140px;display:none;z-index:100}
  .menu.show{display:block}
  .menu a{display:block;padding:8px 14px;text-decoration:none;color:#222;font-size:0.9em;font-weight:400;cursor:pointer}
  .menu a:hover{background:#f0f0f0}
  .menu a.danger{color:#c33}

  /* ---- main 3-col layout ---------------------------------------- */
  .body{flex:1;display:flex;overflow:hidden}
  .pane{display:flex;flex-direction:column;overflow:hidden;background:#fff}
  .pane.tree{flex:0 0 16.66%;border-right:1px solid #e5e5e5;background:#f8f8f8;transition:flex-basis 0.15s}
  .pane.main{flex:1 1 0;border-right:1px solid #e5e5e5}
  .pane.detail{flex:0 0 16.66%;background:#f8f8f8;transition:flex-basis 0.15s}
  .pane.collapsed{flex-basis:28px !important;border:none}
  .pane.collapsed > *:not(.collapse-toggle){display:none}

  .collapse-toggle{height:28px;flex:0 0 28px;border:0;background:#eee;color:#444;cursor:pointer;font-size:1.4em;line-height:1;font-weight:bold;display:flex;align-items:center;justify-content:center;border-bottom:1px solid #ddd}
  .collapse-toggle:hover{background:#dde;color:#2c5}

  /* ---- tree ----------------------------------------------------- */
  .tree-content{flex:1;overflow-y:auto;padding:8px 4px;font-size:0.9em}
  .tree-group{margin-bottom:6px}
  .tree-group-h{padding:6px 10px;font-weight:600;color:#666;font-size:0.85em;text-transform:uppercase;letter-spacing:0.05em;cursor:pointer;user-select:none;display:flex;align-items:center;gap:6px}
  .tree-group-h::before{content:"\\25BE";display:inline-block;width:14px;font-size:1.1em;color:#666;line-height:1}
  .tree-group.folded .tree-group-h::before{content:"\\25B8"}
  .tree-group.folded ul, .tree-group.folded .tree-add{display:none}
  .tree-group ul{list-style:none;margin:0;padding:0}
  .tree-group li{padding:5px 10px 5px 28px;cursor:pointer;border-radius:3px;color:#333}
  .tree-group li:hover{background:#ecece8}
  .tree-group li.active{background:#dfe;color:#161;font-weight:600}
  .tree-group li{display:flex;align-items:center;justify-content:space-between}
  .tree-group li .label{flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
  .tree-group li .badge{font-size:0.7em;background:#ccc;color:#444;border-radius:8px;padding:1px 6px;margin-left:4px;flex:0 0 auto}
  .tree-group li.active .badge{background:#2c5;color:#fff}
  .icon-btn{width:18px;height:18px;border:0;background:transparent;color:#888;cursor:pointer;padding:0;display:flex;align-items:center;justify-content:center;flex:0 0 auto;border-radius:3px;margin-left:4px}
  .icon-btn:hover{color:#c33;background:#f5d5d5}
  .icon-btn svg{width:14px;height:14px;fill:currentColor}
  .tree-add{padding:4px 10px 8px 28px;color:#888;cursor:pointer;font-size:0.85em}
  .tree-add:hover{color:#2c5}

  /* ---- main content --------------------------------------------- */
  .view-title{padding:8px 16px;border-bottom:1px solid #eee;font-size:0.95em;font-weight:600;background:#fff}
  .view-content{flex:1;overflow-y:auto;background:#fff}

  /* chat-specific */
  #chat-controls{display:flex;align-items:center;gap:6px;padding:6px 10px;background:#f8f8f8;border-bottom:1px solid #eee;flex:0 0 auto;flex-wrap:wrap}
  #chat-filter{flex:1;min-width:140px;padding:5px 8px;font:inherit;border:1px solid #ccc;border-radius:3px;font-size:13px}
  .cc-btn{padding:4px 8px;font:inherit;font-size:12px;background:#fff;color:#333;border:1px solid #ccc;border-radius:3px;cursor:pointer}
  .cc-btn:hover{background:#eef}
  .cc-now{font-weight:600;color:#161;border-color:#9c9}
  .cc-pause{font-weight:600}
  .cc-pause.paused{background:#fc6;color:#000}
  .chat-paused-banner{padding:6px 16px;background:#ffe;color:#963;font-size:12px;border-bottom:1px solid #fda;text-align:center}
  #log{padding:8px 16px;font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:13px}
  .msg{margin:3px 0;line-height:1.5;cursor:pointer;padding:1px 6px;border-radius:3px;border:1px solid transparent}
  .msg:hover{background:#f6f6f6}
  .msg.selected{background:#fffbcc;border-color:#e8d370}
  .msg.mention{background:#fff8d6;border-left:3px solid #f5b800;padding-left:8px}
  .msg.mention.selected{background:#fff0a0;border-color:#e8a000;border-left:3px solid #f5b800}
  .out{color:#070}
  .out::before{content:"\\2192  ";color:#7a7;font-weight:bold}
  .in::before{content:"   "}
  .ts{color:#999;margin-right:8px}
  .peer{font-weight:600;margin-right:6px}
  .sys{color:#999;font-style:italic}
  #chat-form{display:flex;border-top:1px solid #ddd;padding:8px;background:#f0f0f0;flex:0 0 auto;position:relative}
  #chat-form input{flex:1;padding:10px;font:inherit;border:1px solid #ccc;border-radius:4px}
  #chat-form button{padding:10px 20px;font:inherit;background:#2c5;color:#fff;border:0;border-radius:4px;margin-left:8px;cursor:pointer;font-weight:600}
  #chat-form button:disabled{background:#aaa;cursor:wait}
  #emoji-btn{padding:8px 12px !important;background:#fff !important;color:#333 !important;border:1px solid #ccc !important;font-size:1.2em !important}
  #emoji-btn:hover{background:#f8f8f8 !important}
  #emoji-picker{position:absolute;bottom:54px;right:8px;width:280px;background:#fff;border:1px solid #ccc;border-radius:6px;box-shadow:0 2px 12px rgba(0,0,0,0.15);padding:8px;display:none;z-index:50}
  #emoji-picker.show{display:block}
  .emoji-grid{display:grid;grid-template-columns:repeat(8,1fr);gap:2px}
  .emoji-grid button{padding:4px !important;font-size:1.3em !important;background:transparent !important;border:0 !important;cursor:pointer !important;border-radius:3px !important;margin:0 !important}
  .emoji-grid button:hover{background:#f0f0f0 !important}

  /* admin-specific */
  .admin-content{padding:14px;overflow-y:auto}
  /* icon-action-buttons (in tabellen) */
  .btn-icon{width:28px;height:28px;border:1px solid #ccc;background:#fff;border-radius:4px;cursor:pointer;display:inline-flex;align-items:center;justify-content:center;padding:0;margin-right:3px;vertical-align:middle}
  .btn-icon:hover{background:#f0f0f0}
  .btn-icon svg{width:16px;height:16px;fill:#555}
  .btn-icon.danger:hover{background:#fcc}
  .btn-icon.danger svg{fill:#c33}
  .btn-icon.toggle-on svg{fill:#161}
  .btn-icon.toggle-off svg{fill:#aaa}
  .admin-content section{background:#fff;border:1px solid #eee;border-radius:6px;padding:14px;margin-bottom:14px}
  .admin-content h2{margin:0 0 10px;font-size:1em;color:#2c5;border-bottom:1px solid #eee;padding-bottom:6px}
  .row{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin:6px 0}
  .row label{flex:0 0 110px;font-size:0.9em;color:#555}
  .row input,.row select{padding:6px 8px;font:inherit;border:1px solid #ccc;border-radius:4px}
  .row input[type=number]{width:90px}
  .row input[type=text]{flex:1;min-width:180px}
  .row button{padding:6px 14px;font:inherit;background:#2c5;color:#fff;border:0;border-radius:4px;cursor:pointer;font-weight:600}
  .row button.danger{background:#c53}
  .row button.small{padding:3px 8px;font-size:0.85em}
  .kv{font-family:ui-monospace,monospace;font-size:13px;color:#444}
  .kv span.k{display:inline-block;width:140px;color:#888}
  .note{font-size:0.85em;color:#888}
  table{width:100%;border-collapse:collapse;font-size:0.9em}
  th,td{text-align:left;padding:6px 8px;border-bottom:1px solid #f0f0f0}
  th{background:#f8f8f8;font-weight:600}

  /* ---- detail pane --------------------------------------------- */
  .detail-content{flex:1;overflow-y:auto;padding:14px;font-size:0.85em}
  .detail-section{margin-bottom:14px}
  .detail-section h3{margin:0 0 6px;font-size:0.78em;color:#888;text-transform:uppercase;letter-spacing:0.05em}
  .detail-section .kv{font-size:12px}
  .detail-section .kv span.k{width:90px}
  .detail-actions{display:flex;gap:6px;margin-bottom:10px}
  .detail-actions button{padding:6px 10px;font:inherit;font-size:0.85em;background:#2c5;color:#fff;border:0;border-radius:4px;cursor:pointer;font-weight:600;flex:1}
  .detail-actions button:hover{background:#1b4}
  .detail-actions button.sec{background:#888}
  .detail-actions button.sec:hover{background:#666}
  .msg-quote{font-family:ui-monospace,monospace;font-size:12px;background:#f4f4f0;padding:6px 8px;border-radius:3px;border-left:3px solid #ccc;white-space:pre-wrap;word-break:break-word;max-height:120px;overflow-y:auto}
  .rssi-good{color:#161;font-weight:600}
  .rssi-mid{color:#a60;font-weight:600}
  .rssi-bad{color:#c33;font-weight:600}
  /* path-pillen */
  .path-row{margin:4px 0;background:#fafafa;border-radius:4px;border:1px solid #eee}
  .path-row > summary{padding:6px 8px;cursor:pointer;font-size:11px;color:#444;list-style:none}
  .path-row > summary::-webkit-details-marker{display:none}
  .path-row > summary::before{content:"\\25B8  ";color:#888}
  .path-row[open] > summary::before{content:"\\25BE  "}
  .path-row > summary:hover{background:#f0f0f0}
  .hop-chain{padding:6px 8px 8px;font-size:11px;line-height:1.9;border-top:1px solid #eee;word-break:break-word}
  .hop{display:inline-block;padding:1px 6px;border-radius:3px;margin:0 2px;font-family:ui-monospace,monospace;font-size:11px}
  .hop-known{background:#dfeefd;color:#024;font-weight:600;font-family:inherit}
  .hop-unknown{background:#f0f0f0;color:#888}
  .hop-arrow{color:#aaa;margin:0 1px}
  .hop-self{background:#cfe;color:#040;font-weight:700;font-family:inherit}
  /* inline ack + signal indicators in chat-list */
  .ack{display:inline-block;margin-right:6px;font-weight:600;font-family:ui-monospace,monospace;font-size:11px;width:18px;text-align:left}
  .ack-sent{color:#888}
  .ack-acked{color:#161}
  .ack-failed{color:#c33}
  .sig-dot{display:inline-block;margin-right:6px;font-size:14px;line-height:1;vertical-align:middle;width:12px;text-align:center}
  .sig-good{color:#161}
  .sig-ok{color:#9c0}
  .sig-mid{color:#e80}
  .sig-bad{color:#c33}

  /* ---- toast --------------------------------------------------- */
  #toast{position:fixed;bottom:20px;right:20px;padding:10px 16px;background:#222;color:#fff;border-radius:4px;opacity:0;transition:opacity 0.2s;pointer-events:none;max-width:480px;white-space:pre-wrap;z-index:200}
  #toast.show{opacity:0.95}
  #toast.err{background:#c33}
  #toast.ok{background:#161}
  #toast.mention{background:#f5b800;color:#222;font-weight:600;border-left:6px solid #c98a00}
</style></head>
<body>

<header>
  <span class="title" id="conn-title">MeshCore Gateway</span>
  <div class="hdr-status" id="hdr-status"></div>
  <div class="avatar" onclick="toggleMenu(event)" title="account">
    <svg viewBox="0 0 24 24"><path d="M12 12c2.7 0 5-2.3 5-5s-2.3-5-5-5-5 2.3-5 5 2.3 5 5 5zm0 2c-3.3 0-10 1.7-10 5v3h20v-3c0-3.3-6.7-5-10-5z"/></svg>
    <div class="menu" id="user-menu" onclick="event.stopPropagation()">
      <a id="menu-username" style="font-weight:600;color:#888;cursor:default" onclick="event.stopPropagation()">…</a>
      <a onclick="changePasswordPrompt()">Wachtwoord wijzigen</a>
      <a onclick="location.href='/logout'">Logout</a>
      <a id="menu-quit" class="danger" onclick="quitApp()" style="display:none">Quit (gateway stoppen)</a>
    </div>
  </div>
</header>

<div class="body">

  <!-- ------ LEFT PANE ------ -->
  <div class="pane tree" id="pane-tree">
    <button class="collapse-toggle" onclick="toggleCollapse('tree')" title="inklappen">&laquo;</button>
    <div class="tree-content">
      <div class="tree-group" id="grp-chat">
        <div class="tree-group-h" onclick="toggleGroup('grp-chat')">Chat</div>
        <ul id="tree-channels"></ul>
        <div class="tree-add" onclick="promptAddHashtag()">+ hashtag</div>
      </div>
      <div class="tree-group" id="grp-dm">
        <div class="tree-group-h" onclick="toggleGroup('grp-dm')">DM</div>
        <ul id="tree-dms">
          <li onclick="selectContactsManager()" id="li-contacts-mgr"
              style="font-style:italic;color:#888;border-bottom:1px solid #eee;margin-bottom:2px">
            <span class="label">Contactpersonen</span>
          </li>
        </ul>
      </div>
      <div class="tree-group" id="grp-reports">
        <div class="tree-group-h" onclick="toggleGroup('grp-reports')">Rapportages</div>
        <ul>
          <li onclick="selectReport('overview')"  data-report="overview">Overzicht</li>
          <li onclick="selectReport('repeaters')" data-report="repeaters">Repeaters</li>
        </ul>
      </div>
      <div class="tree-group" id="grp-admin" style="display:none">
        <div class="tree-group-h" onclick="toggleGroup('grp-admin')">Admin</div>
        <ul>
          <li onclick="selectAdminView('radio')"        data-sub="radio">Radio</li>
          <li onclick="selectAdminView('node')"         data-sub="node">Node</li>
          <li onclick="selectAdminView('prefs')"        data-sub="prefs">Voorkeuren</li>
          <li onclick="selectAdminView('channels')"     data-sub="channels">Channels</li>
          <li onclick="selectAdminView('contacts')"     data-sub="contacts">Contacten</li>
          <li onclick="selectAdminView('bots')"         data-sub="bots">Bots</li>
          <li onclick="selectAdminView('housekeeping')" data-sub="housekeeping">Housekeeping</li>
          <li onclick="selectAdminView('users')"        data-sub="users">Gebruikers</li>
        </ul>
      </div>
    </div>
  </div>

  <!-- ------ MIDDLE PANE ------ -->
  <div class="pane main">
    <div class="view-title" id="view-title">Public</div>
    <div class="view-content" id="view-content">
      <!-- Chat view -->
      <div id="chat-view" style="display:flex;flex-direction:column;height:100%">
        <div id="chat-controls">
          <input id="chat-filter" type="text" placeholder="filter op tekst…" autocomplete="off">
          <button class="cc-btn" onclick="loadOlder()" title="laad oudere berichten">↑ ouder</button>
          <button class="cc-btn" onclick="shiftTime(-12)">-12u</button>
          <button class="cc-btn" onclick="shiftTime(-2)">-2u</button>
          <button class="cc-btn" onclick="shiftTime(2)">+2u</button>
          <button class="cc-btn" onclick="shiftTime(12)">+12u</button>
          <button class="cc-btn cc-now" onclick="jumpToNow()">nu</button>
          <button class="cc-btn cc-pause" id="cc-pause-btn" onclick="togglePause()" title="pauzeer/hervat live updates">⏸</button>
        </div>
        <div id="log" style="flex:1;overflow-y:auto"></div>
        <form id="chat-form">
          <input id="txt" autocomplete="off" placeholder="Bericht naar Public…" disabled>
          <button id="emoji-btn" type="button" onclick="toggleEmojiPicker(event)" title="emoji invoegen">&#x1F642;</button>
          <button id="btn" type="submit" disabled>Stuur</button>
          <div id="emoji-picker" onclick="event.stopPropagation()">
            <div class="emoji-grid" id="emoji-grid"></div>
          </div>
        </form>
      </div>
      <!-- Admin view (hidden by default) -->
      <div id="admin-view" class="admin-content" style="display:none"></div>
      <!-- Reports view -->
      <div id="reports-view" class="admin-content" style="display:none"></div>
      <!-- Contacten-beheer view (per-user) -->
      <div id="contacts-view" class="admin-content" style="display:none"></div>
    </div>
  </div>

  <!-- ------ RIGHT PANE ------ -->
  <div class="pane detail" id="pane-detail">
    <button class="collapse-toggle" onclick="toggleCollapse('detail')" title="inklappen">&raquo;</button>
    <div class="detail-content" id="detail-content">…</div>
  </div>

</div>

<div id="toast"></div>

<!-- Socket.IO MOET vóór de inline JS — anders is io() undefined wanneer we hem aanroepen. -->
<script src="https://cdn.socket.io/4.7.5/socket.io.min.js"></script>

<script>
/* ============== state ============== */
let STATE = {
  view: 'chat',           // 'chat' | 'admin' | 'reports'
  adminSub: 'radio',
  reportSub: 'overview',
  channel: {kind:'public', idx:0, name:'Public'},  // ook DM: {kind:'dm', peer, name}
  channels: [],
  contacts: [],           // van /contacts (companion-contactenlijst)
  myContacts: [],         // van /my/contacts (per-user opgeslagen)
  status: null,
  selectedMsg: null,
  me: null,
  msgIndex: {},
  // chat-controls
  paused: false,
  pendingMsgs: [],        // berichten die binnenkomen tijdens pauze
  filterText: '',         // huidige tekst-filter (lowercase)
  timeAnchorHours: 0,     // 0 = realtime; >0 = N uur in het verleden
  reportPeriodHours: 24,  // default grafiekperiode
};

/* ============== helpers ============== */
function $(id){return document.getElementById(id);}
function escapeHTML(s){return String(s||'').replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;","\\"":"&quot;","'":"&#39;"})[c]);}

/* Server stuurt ISO-UTC timestamps; client formatteert naar lokale tijd. */
function fmtTs(iso){
  if (!iso) return '?';
  // Backward-compat: oude HH:MM:SS strings gewoon teruggeven
  if (typeof iso === 'string' && iso.length <= 8 && iso.indexOf('T') === -1) return iso;
  try {
    const d = new Date(iso);
    if (isNaN(d)) return iso;
    return d.toLocaleTimeString([], {hour:'2-digit', minute:'2-digit', second:'2-digit'});
  } catch(e) { return iso; }
}
let _toastTimer = null;
function toast(msg, cls, durationMs){
  const t = $('toast');
  t.textContent = msg;
  t.className = 'show' + (cls?' '+cls:'');
  if (_toastTimer) clearTimeout(_toastTimer);
  _toastTimer = setTimeout(()=>t.className='', durationMs || 4000);
}

/* ============== mentions: highlight + sound + toast ============== */
function isMention(m){
  const s = STATE.status && STATE.status.node || {};
  const text = (m.text || '').toLowerCase();
  if (!text.includes('@[')) return false;
  if (s.name && text.includes('@[' + String(s.name).toLowerCase() + ']')) return true;
  if (s.pubkey && text.includes('@[' + String(s.pubkey).toLowerCase() + ']')) return true;
  return false;
}

let _audioCtx = null;
function playMentionBeep(){
  try {
    if (!_audioCtx) _audioCtx = new (window.AudioContext || window.webkitAudioContext)();
    const ctx = _audioCtx;
    if (ctx.state === 'suspended') ctx.resume();
    const now = ctx.currentTime;
    // Twee korte tonen (E5 → A5) voor herkenbare 'ping'
    [659.25, 880].forEach((freq, i) => {
      const osc = ctx.createOscillator();
      const gain = ctx.createGain();
      osc.type = 'sine';
      osc.frequency.value = freq;
      osc.connect(gain); gain.connect(ctx.destination);
      const start = now + i * 0.12;
      gain.gain.setValueAtTime(0.0001, start);
      gain.gain.exponentialRampToValueAtTime(0.18, start + 0.02);
      gain.gain.exponentialRampToValueAtTime(0.0001, start + 0.18);
      osc.start(start);
      osc.stop(start + 0.20);
    });
  } catch(e) {}
}

function notifyMention(m){
  const sender = m._sender || extractSender(m) || '?';
  const body   = m._body || m.text || '';
  const short  = body.length > 80 ? body.slice(0, 77) + '…' : body;
  toast('@' + sender + ' → ' + short, 'mention', 6000);
  // Native browser-notification als tab op de achtergrond staat
  showNativeNotification('Mention van ' + sender, body);
}

function onMention(m){
  playMentionBeep();
  notifyMention(m);
}
async function api(path, opts){
  opts = opts || {};
  opts.headers = Object.assign({'Content-Type':'application/json'}, opts.headers||{});
  const r = await fetch(path, opts);
  let body = null;
  try { body = await r.json(); } catch(e) {}
  if (!r.ok) {
    toast((body && body.detail) || ('HTTP '+r.status), 'err');
    throw new Error(r.status);
  }
  return body;
}

/* ============== layout: collapse + menu + groups ============== */
function toggleCollapse(which){
  const pane = $('pane-'+which);
  pane.classList.toggle('collapsed');
  // toggle arrow
  const btn = pane.querySelector('.collapse-toggle');
  if (which === 'tree') btn.innerHTML = pane.classList.contains('collapsed') ? '&raquo;' : '&laquo;';
  else                  btn.innerHTML = pane.classList.contains('collapsed') ? '&laquo;' : '&raquo;';
}
function toggleGroup(id){ $(id).classList.toggle('folded'); }
function toggleMenu(e){ e.stopPropagation(); $('user-menu').classList.toggle('show'); }
document.addEventListener('click', () => $('user-menu').classList.remove('show'));

async function quitApp(){
  if (!confirm('De gateway helemaal afsluiten? CLI en Web stoppen beide.')) return;
  try {
    await api('/admin/quit', {method:'POST', body:'{}'});
    toast('Gateway sluit af…', 'ok');
    setTimeout(()=>document.body.innerHTML='<p style="padding:40px;font-family:system-ui">Gateway is afgesloten.</p>', 1500);
  } catch(e){}
}

/* ============== tree rendering ============== */
function renderTree(){
  const ul = $('tree-channels');
  ul.innerHTML = '';
  // Sorteer: public eerst, dan hashtag, dan private — alles uit DB
  const order = {public:0, hashtag:1, private:2};
  const sorted = [...STATE.channels].sort((a,b) => {
    const ka = order[a.kind] ?? 9;
    const kb = order[b.kind] ?? 9;
    if (ka !== kb) return ka - kb;
    return a.idx - b.idx;
  });
  sorted.forEach(c => {
    // Hashtag-namen zijn al opgeslagen met '#' prefix, dus geen extra prefixing.
    const display = c.alias || c.name || ('slot ' + c.idx);
    ul.appendChild(makeChanLi({
      kind: c.kind, idx: c.idx, name: display, rawName: c.name,
    }));
  });
  // Admin-sub-items markeren
  document.querySelectorAll('#grp-admin li[data-sub]').forEach(li => {
    li.classList.toggle('active', STATE.view === 'admin' && li.dataset.sub === STATE.adminSub);
  });
  // Reports-sub-items markeren
  document.querySelectorAll('#grp-reports li[data-report]').forEach(li => {
    li.classList.toggle('active', STATE.view === 'reports' && li.dataset.report === STATE.reportSub);
  });
  // DM-tree
  renderDmTree();
}

function renderDmTree(){
  const ul = $('tree-dms');
  if (!ul) return;
  // Pak het Contactpersonen-beheer-item, knip het er even uit en plak weer bovenaan
  const mgr = $('li-contacts-mgr');
  ul.innerHTML = '';
  if (mgr) {
    mgr.classList.toggle('active', STATE.view === 'contacts');
    ul.appendChild(mgr);
  }

  const my = STATE.myContacts || [];
  if (my.length === 0) {
    const li = document.createElement('li');
    li.style.color = '#bbb';
    li.style.fontStyle = 'italic';
    li.style.fontSize = '0.85em';
    li.textContent = '(geen opgeslagen contacten)';
    ul.appendChild(li);
    return;
  }
  my.forEach(c => {
    const li = document.createElement('li');
    const lab = document.createElement('span');
    lab.className = 'label';
    lab.textContent = c.name || c.pubkey_prefix;
    li.appendChild(lab);
    li.onclick = () => selectChannel({kind:'dm', peer: c.pubkey_prefix, name: c.name || c.pubkey_prefix});
    if (STATE.view === 'chat' && STATE.channel.kind === 'dm' && STATE.channel.peer === c.pubkey_prefix) {
      li.classList.add('active');
    }
    ul.appendChild(li);
  });
}
function makeChanLi(ch){
  const li = document.createElement('li');
  const lab = document.createElement('span');
  lab.className = 'label';
  lab.textContent = ch.name;
  li.appendChild(lab);
  li.onclick = () => selectChannel(ch);
  if (STATE.view==='chat' && sameChan(ch, STATE.channel)) li.classList.add('active');
  // Trash-icoon alleen voor hashtag-channels (private gaat via admin)
  if (ch.kind === 'hashtag') {
    const btn = document.createElement('button');
    btn.className = 'icon-btn';
    btn.title = 'verwijder hashtag (slot ' + ch.idx + ')';
    btn.innerHTML = '<svg viewBox="0 0 24 24"><path d="M9 3v1H4v2h16V4h-5V3H9zm-3 5l1 13h10l1-13H6zm3 2h2v9H9v-9zm4 0h2v9h-2v-9z"/></svg>';
    btn.onclick = (e) => { e.stopPropagation(); removeChannelFromTree(ch.idx, ch.name); };
    li.appendChild(btn);
  }
  return li;
}
function sameChan(a,b){
  if (!a||!b) return false;
  // Channels worden geïdentificeerd door idx; kind is alleen UI-aanduiding.
  return a.idx === b.idx;
}

/* ============== view switching ============== */
function selectChannel(ch){
  STATE.view = 'chat';
  STATE.channel = ch;
  _hideAllViews();
  $('chat-view').style.display = 'flex';
  const dmSuffix = (ch.kind === 'dm' && ch.peer) ? ' (DM · ' + ch.peer + ')' : '';
  $('view-title').textContent = ch.name + dmSuffix;
  $('txt').placeholder = 'Bericht naar ' + ch.name + '…';
  loadChatHistory();
  renderTree();
  renderDetail();
}
function _hideAllViews(){
  $('chat-view').style.display = 'none';
  $('admin-view').style.display = 'none';
  $('reports-view').style.display = 'none';
  const cv = $('contacts-view');
  if (cv) cv.style.display = 'none';
}

function selectAdminView(sub){
  if (!STATE.me || STATE.me.role !== 'admin') return;
  STATE.view = 'admin';
  STATE.adminSub = sub;
  _hideAllViews();
  $('admin-view').style.display = 'block';
  const titles = {radio:'Radio', node:'Node', prefs:'Voorkeuren',
                  channels:'Channels', contacts:'Contacten', bots:'Bots',
                  housekeeping:'Housekeeping', users:'Gebruikers'};
  $('view-title').textContent = 'Admin — ' + (titles[sub] || sub);
  renderAdmin();
  renderTree();
  renderDetail();
}

function selectContactsManager(){
  STATE.view = 'contacts';
  _hideAllViews();
  $('contacts-view').style.display = 'block';
  $('view-title').textContent = 'DM — Contactpersonen';
  renderContactsManager();
  renderTree();
  renderDetail();
}

async function renderContactsManager(){
  const el = $('contacts-view');
  el.innerHTML = '<section><h2>Mijn contactpersonen</h2><div class="kv">…laden…</div></section>';
  let mine;
  try { mine = await api('/my/contacts'); } catch(e) { return; }

  const rows = mine.map(c => {
    const created = c.created_at ? new Date(c.created_at).toLocaleDateString() : '—';
    const safeName = escapeHTML(c.name || '').replace(/\\\\/g,'\\\\\\\\').replace(/\\x27/g,"\\\\\\x27");
    const status = c.known_to_companion
      ? '<span style="color:#161" title="bekend bij companion — DM werkt">✓</span>'
      : '<span style="color:#c80" title="niet bekend bij companion — DM werkt nog niet">⚠</span>';
    return '<tr>' +
      '<td>' + status + ' ' + escapeHTML(c.name || '?') + '</td>' +
      '<td><code style="font-size:11px;word-break:break-all" title="'+escapeHTML(c.pubkey)+'">' + escapeHTML(c.pubkey_prefix) + '…</code></td>' +
      '<td>' + escapeHTML(c.notes || '') + '</td>' +
      '<td>' + escapeHTML(created) + '</td>' +
      '<td><button class="small danger" onclick="removeMyContact(\\''+c.pubkey+'\\',\\''+safeName+'\\')">verwijder</button></td>' +
    '</tr>';
  }).join('');

  el.innerHTML = `
    <section><h2>Mijn contactpersonen (${mine.length})</h2>
      <table style="width:100%">
        <thead><tr>
          <th>Status · Naam</th><th>Pubkey (prefix · hover voor vol)</th><th>Notitie</th><th>Toegevoegd</th><th></th>
        </tr></thead>
        <tbody>${rows || '<tr><td colspan="5" style="color:#888">geen opgeslagen contacten</td></tr>'}</tbody>
      </table>
      <div class="note">
        <b>✓</b> = bekend bij companion (DM werkt). <b>⚠</b> = lokaal opgeslagen, maar de companion kent de pubkey nog niet → DM faalt met "not found".
        Wacht op een advert van die node (of zet "Auto-add adverts" aan in <i>Admin → Voorkeuren</i>) zodat de companion 'm leert kennen.
      </div>
    </section>

    <section><h2>Contactpersoon toevoegen</h2>
      <div class="row"><label>Naam</label><input id="mc-name" type="text" placeholder="bv 'Henk'"></div>
      <div class="row"><label>Pubkey</label><input id="mc-pk" type="text" placeholder="64 hex chars (32 bytes), bv 2e400317326bc8d4..." style="font-family:monospace"></div>
      <div class="row"><label>Notitie</label><input id="mc-notes" type="text" placeholder="optioneel"></div>
      <div class="row"><button onclick="addMyContact()">Toevoegen</button></div>
      <div class="note">Volledige 32-byte publieke sleutel (te vinden in Admin → Contacten of in een share/QR).</div>
      <div class="note">Per-user opgeslagen — andere web-gebruikers zien jouw contacten niet.</div>
    </section>`;
}

async function addMyContact(){
  const name = $('mc-name').value.trim();
  const pk   = $('mc-pk').value.trim().toLowerCase();
  const notes= $('mc-notes').value.trim();
  if (!name || !pk) { toast('naam + pubkey vereist','err'); return; }
  if (pk.length !== 64) { toast('pubkey moet 64 hex chars zijn (gaf '+pk.length+')','err'); return; }
  try {
    const r = await api('/my/contacts/add', {method:'POST', body:JSON.stringify({name, pubkey: pk, notes: notes || null})});
    toast(r.message || 'ok', 'ok');
    $('mc-name').value = ''; $('mc-pk').value = ''; $('mc-notes').value = '';
    await refreshMyContacts();
    renderContactsManager();
  } catch(e){}
}

async function removeMyContact(pubkey, name){
  if (!confirm('Contactpersoon "'+name+'" verwijderen?')) return;
  try {
    const r = await api('/my/contacts/remove', {method:'POST', body:JSON.stringify({pubkey: pubkey})});
    toast(r.message || 'ok', 'ok');
    await refreshMyContacts();
    renderContactsManager();
  } catch(e){}
}

async function refreshMyContacts(){
  try {
    STATE.myContacts = await api('/my/contacts');
    renderTree();
  } catch(e){}
}

function selectReport(sub){
  STATE.view = 'reports';
  STATE.reportSub = sub;
  _hideAllViews();
  $('reports-view').style.display = 'block';
  const titles = {overview:'Overzicht', repeaters:'Repeaters'};
  $('view-title').textContent = 'Rapportages — ' + (titles[sub] || sub);
  renderReports();
  renderTree();
  renderDetail();
}

/* ============== chat ============== */
async function loadChatHistory(){
  $('log').innerHTML = '';
  STATE.selectedMsg = null;
  STATE.msgIndex = {};
  STATE.pendingMsgs = [];
  try {
    let basePath;
    if (STATE.channel.kind === 'dm') {
      basePath = '/dm/' + encodeURIComponent(STATE.channel.peer) + '/history';
    } else {
      basePath = '/channels/' + STATE.channel.idx + '/history';
    }
    let path = basePath + '?limit=30';
    if (STATE.timeAnchorHours > 0) {
      path = basePath + '?limit=200';
    }
    const rows = await api(path);
    let toRender = rows;
    if (STATE.timeAnchorHours > 0) {
      const anchorMs = Date.now() - STATE.timeAnchorHours * 3600 * 1000;
      toRender = rows.filter(m => new Date(m.ts).getTime() <= anchorMs).slice(-30);
    }
    toRender.forEach(m => addMsg(m, /*skipFilter=*/false));
    applyFilter();
  } catch(e){}
}

async function loadOlder(){
  if (STATE.msgIndex && Object.keys(STATE.msgIndex).length === 0) {
    return loadChatHistory();
  }
  const ids = Object.keys(STATE.msgIndex).map(Number);
  if (ids.length === 0) return;
  const oldest = Math.min(...ids);
  let path;
  if (STATE.channel.kind === 'dm') {
    path = '/dm/' + encodeURIComponent(STATE.channel.peer) + '/history?limit=30&before_id=' + oldest;
  } else {
    path = '/channels/' + STATE.channel.idx + '/history?limit=30&before_id=' + oldest;
  }
  try {
    const rows = await api(path);
    if (rows.length === 0) {
      toast('geen oudere berichten', 'ok');
      return;
    }
    // Prepend in DOM (in volgorde, oudste bovenaan)
    const log = $('log');
    const wasAtBottom = log.scrollTop + log.clientHeight >= log.scrollHeight - 4;
    const sentinel = log.firstChild;
    rows.forEach(m => {
      const div = _buildMsgEl(m);
      log.insertBefore(div, sentinel);
    });
    applyFilter();
    // Scroll niet auto naar onder als we boven aan het kijken zijn
    if (wasAtBottom) log.scrollTop = log.scrollHeight;
  } catch(e){}
}

function shiftTime(deltaH){
  // deltaH negatief = ouder, positief = recenter
  STATE.timeAnchorHours = Math.max(0, STATE.timeAnchorHours - deltaH);
  updateNowButton();
  loadChatHistory();
}

function jumpToNow(){
  STATE.timeAnchorHours = 0;
  STATE.paused = false;
  updatePauseButton();
  updateNowButton();
  loadChatHistory();
}

function updateNowButton(){
  const btn = document.querySelector('.cc-now');
  if (!btn) return;
  if (STATE.timeAnchorHours > 0) {
    btn.textContent = 'nu (-' + STATE.timeAnchorHours + 'u)';
    btn.style.background = '#fc6';
  } else {
    btn.textContent = 'nu';
    btn.style.background = '';
  }
}

function togglePause(){
  STATE.paused = !STATE.paused;
  updatePauseButton();
  if (!STATE.paused) {
    // Replay queued msgs
    const q = STATE.pendingMsgs;
    STATE.pendingMsgs = [];
    q.forEach(m => addMsg(m));
  }
}

function updatePauseButton(){
  const btn = $('cc-pause-btn');
  if (!btn) return;
  if (STATE.paused) {
    btn.textContent = '▶ ' + (STATE.pendingMsgs.length || '');
    btn.classList.add('paused');
    btn.title = 'hervat live updates (' + STATE.pendingMsgs.length + ' wachtend)';
  } else {
    btn.textContent = '⏸';
    btn.classList.remove('paused');
    btn.title = 'pauzeer live updates';
  }
}

function applyFilter(){
  // Lokaal verbergen tijdens typen / live-msgs
  const f = STATE.filterText;
  document.querySelectorAll('#log .msg').forEach(el => {
    if (!f) { el.style.display = ''; return; }
    el.style.display = el.textContent.toLowerCase().includes(f) ? '' : 'none';
  });
}

let _searchTimer = null;
async function runServerSearch(q){
  // Server-side search door alle berichten in dit kanaal/dm
  if (!q) {
    // Filter leeg → reset naar gewone history
    return loadChatHistory();
  }
  $('log').innerHTML = '';
  STATE.msgIndex = {};
  try {
    const params = new URLSearchParams({q, limit: '200'});
    if (STATE.channel.kind === 'dm') {
      params.set('kind', 'dm');
      params.set('peer', STATE.channel.peer);
    } else {
      params.set('kind', 'channel');
      params.set('channel_idx', String(STATE.channel.idx));
    }
    const rows = await api('/messages/search?' + params.toString());
    rows.forEach(m => addMsg(m));
    if (rows.length === 0) {
      const div = document.createElement('div');
      div.className = 'msg sys';
      div.style.color = '#888';
      div.style.fontStyle = 'italic';
      div.textContent = '— geen resultaten voor "'+q+'" —';
      $('log').appendChild(div);
    }
  } catch(e){}
}

/* Extract afzendernaam uit channel-msg tekst.
   Companion firmware geeft pubkey_prefix vaak NIET mee op channels;
   afzenders prefixen hun naam zelf met "NAAM: tekst".
   Heuristiek: naam = alles tot eerste ':', max 64 chars, mag spaties bevatten,
   geen newlines, geen URL-achtige patronen ('://'). */
function extractSender(m){
  if (m.peer && m.peer !== '?' && m.peer !== 'self') return m.peer;
  const t = m.text || '';
  if (!t) return null;
  // Vermijd URLs: 'http://...' zou anders 'http' als naam pakken
  const urlIdx = t.indexOf('://');
  const colon = t.indexOf(':');
  if (colon <= 0 || colon > 64) return null;
  if (urlIdx >= 0 && urlIdx <= colon) return null;
  const head = t.substring(0, colon);
  if (head.indexOf(String.fromCharCode(10)) >= 0) return null;
  if (head.indexOf(String.fromCharCode(13)) >= 0) return null;
  const candidate = head.trim();
  if (!candidate) return null;
  // Geen control chars in naam
  for (let i = 0; i < candidate.length; i++) {
    if (candidate.charCodeAt(i) < 32) return null;
  }
  return candidate;
}

/* Strip "NAAM: " uit het zichtbare bericht zodat de body schoner is. */
function stripNamePrefix(m){
  const sender = extractSender(m);
  if (sender && (m.peer === null || m.peer === '?' || m.peer === undefined)
      && m.text && m.text.startsWith(sender + ':')) {
    return m.text.substring(sender.length + 1).trim();
  }
  return m.text || '';
}

function ackIcon(msg){
  if (!msg || msg.direction !== 'out') return '';
  const status = msg.ack_status;
  if (msg.kind === 'dm') {
    if (status === 'acked')   return '<span class="ack ack-acked" title="bevestigd">✓✓</span>';
    if (status === 'failed')  return '<span class="ack ack-failed" title="mislukt">!!</span>';
    if (status === 'sent')    return '<span class="ack ack-sent" title="verzonden, wachten op ack">✓</span>';
    return '';
  }
  // Channel-out: geen ack op protocol-niveau, maar wel implicit-repeat detectie
  if (msg.kind === 'channel') {
    if (status === 'repeated') return '<span class="ack ack-acked" title="opgepikt door mesh-repeater">↻</span>';
    if (status === 'sent')     return '<span class="ack ack-sent" title="verzonden">✓</span>';
  }
  return '';
}

function signalDot(meta){
  // Eén gekleurd bolletje op basis van SNR (of hops als geen SNR).
  let cls = null, label = '';
  if (typeof meta.snr === 'number') {
    if (meta.snr >= 7)       { cls = 'sig-good'; label = 'SNR ' + meta.snr.toFixed(1) + ' dB (uitstekend)'; }
    else if (meta.snr >= 0)  { cls = 'sig-ok';   label = 'SNR ' + meta.snr.toFixed(1) + ' dB (goed)'; }
    else if (meta.snr >= -7) { cls = 'sig-mid';  label = 'SNR ' + meta.snr.toFixed(1) + ' dB (matig)'; }
    else                     { cls = 'sig-bad';  label = 'SNR ' + meta.snr.toFixed(1) + ' dB (slecht)'; }
  } else if (typeof meta.hops === 'number') {
    if (meta.hops <= 0)        { cls = 'sig-good'; label = 'direct (0 hops)'; }
    else if (meta.hops === 1)  { cls = 'sig-ok';   label = '1 hop'; }
    else if (meta.hops === 2)  { cls = 'sig-mid';  label = '2 hops'; }
    else                       { cls = 'sig-bad';  label = meta.hops + ' hops'; }
  }
  if (!cls) return '';
  return '<span class="sig-dot '+cls+'" title="'+label+'">●</span>';
}

function _buildMsgEl(m){
  const div = document.createElement('div');
  div.className = 'msg ' + (m.direction === 'out' ? 'out' : 'in');
  const displayPeer = extractSender(m) || (m.direction === 'out' ? 'self' : '?');
  const displayText = stripNamePrefix(m);
  if (m.direction === 'in' && isMention(m)) div.classList.add('mention');

  let prefix = '';
  if (m.direction === 'out') prefix = ackIcon(m);
  else                       prefix = signalDot(extractMeta(m));

  div.innerHTML = prefix +
    '<span class="ts">'+escapeHTML(fmtTs(m.ts))+'</span>' +
    '<span class="peer">'+escapeHTML(displayPeer)+':</span>' +
    escapeHTML(displayText);
  div.onclick = () => selectMsg(m, div);
  m._sender = displayPeer;
  m._body = displayText;
  if (m.id) STATE.msgIndex[m.id] = {el: div, msg: m};
  return div;
}

function addMsg(m){
  const div = _buildMsgEl(m);
  $('log').appendChild(div);
  // Filter direct toepassen
  const f = STATE.filterText;
  if (f && !div.textContent.toLowerCase().includes(f)) {
    div.style.display = 'none';
  } else {
    $('log').scrollTop = $('log').scrollHeight;
  }
}

function selectMsg(m, el){
  // de-select alle anderen
  document.querySelectorAll('.msg.selected').forEach(e => e.classList.remove('selected'));
  el.classList.add('selected');
  STATE.selectedMsg = m;
  renderDetail();
}

/* socketio */
const sock = io({transports:['websocket','polling']});
sock.on('connect',    () => { $('conn-title').textContent='MeshCore Gateway · verbonden'; $('txt').disabled=false; $('btn').disabled=false; });
sock.on('disconnect', () => { $('conn-title').textContent='MeshCore Gateway · verbroken'; $('txt').disabled=true; $('btn').disabled=true; });
sock.on('connect_error', () => { setTimeout(()=>location.href='/login', 1500); });
sock.on('msg', (m) => {
  // Mention-detectie: alleen op kanaal-msgs (DMs zijn al gericht aan mij)
  if (m.kind === 'channel' && m.direction === 'in' && isMention(m)) {
    onMention(m);
  }
  if (STATE.view !== 'chat') return;
  // Filter op huidige view: channel of DM
  if (STATE.channel.kind === 'dm') {
    if (m.kind !== 'dm') return;
    if (m.peer !== STATE.channel.peer) return;
  } else {
    if (m.kind !== 'channel') return;
    if (m.channel_idx !== STATE.channel.idx) return;
  }
  if (STATE.timeAnchorHours > 0) return;
  if (STATE.filterText) return;  // tijdens search-modus geen live-updates
  if (STATE.paused) {
    STATE.pendingMsgs.push(m);
    updatePauseButton();
    return;
  }
  addMsg(m);
});

sock.on('msg-update', (u) => {
  // Update inline ack-icoon en bewaarde state voor latency in detail
  if (!u || !u.msg_id) return;
  const entry = STATE.msgIndex[u.msg_id];
  if (entry) {
    entry.msg.ack_status = u.ack_status;
    entry.msg.acked_at = u.acked_at;
    entry.msg.latency_s = u.latency_s;
    // Re-render alleen het ack-icoon (eerste span vervangen)
    const oldAck = entry.el.querySelector('.ack');
    const newAck = document.createElement('span');
    newAck.innerHTML = ackIcon(entry.msg);
    if (oldAck && newAck.firstChild) entry.el.replaceChild(newAck.firstChild, oldAck);
    else if (newAck.firstChild) entry.el.insertBefore(newAck.firstChild, entry.el.firstChild);
  }
  // Detail-pane bijwerken als deze msg geselecteerd is
  if (STATE.selectedMsg && STATE.selectedMsg.id === u.msg_id) {
    STATE.selectedMsg.ack_status = u.ack_status;
    STATE.selectedMsg.acked_at = u.acked_at;
    STATE.selectedMsg.latency_s = u.latency_s;
    renderDetail();
  }
});

$('chat-form').addEventListener('submit', (e) => {
  e.preventDefault();
  const t = $('txt').value.trim();
  if (!t) return;
  $('btn').disabled = true;
  let payload;
  if (STATE.channel.kind === 'dm') {
    payload = {kind:'dm', peer: STATE.channel.peer, text: t};
  } else {
    payload = {channel_idx: STATE.channel.idx, text: t};
  }
  sock.emit('send', payload, (ack) => {
    $('btn').disabled = false;
    if (!ack || !ack.ok) toast('verzenden mislukt: '+(ack && ack.err || '?'), 'err');
    $('txt').focus();
  });
  $('txt').value='';
});

/* ============== emoji picker ============== */
/* Emoji's worden gebouwd uit codepoints — voorkomt parse-issues en houdt
   de bron leesbaar. */
const EMOJIS = [
  0x1F600, 0x1F602, 0x1F923, 0x1F60D, 0x1F60E, 0x1F605, 0x1F61C, 0x1F914,
  0x1F62D, 0x1F62C, 0x1F633, 0x1F62E, 0x1F634, 0x1F62A, 0x1F644, 0x1F910,
  0x1F60A, 0x1F642, 0x1F643, 0x1F60F, 0x1F636, 0x1F610, 0x1F613, 0x1F614,
  0x1F44D, 0x1F44E, 0x1F44F, 0x1F64F, 0x1F4AA, 0x1F91D, 0x270C, 0x1F44C,
  0x2764, 0x1F494, 0x1F389, 0x2B50, 0x1F525, 0x2728, 0x1F381, 0x1F37B,
  0x2600, 0x1F327, 0x26C8, 0x2744, 0x1F308, 0x26A1, 0x1F30A, 0x1F33A,
  0x1F697, 0x1F6B2, 0x2708, 0x1F680, 0x1F3E0, 0x1F4F1, 0x1F4BB, 0x1F4F7,
  0x2705, 0x274C, 0x26A0, 0x1F514, 0x23F0, 0x1F4CD, 0x1F4DE, 0x1F4A1,
].map(cp => String.fromCodePoint(cp));

function buildEmojiGrid(){
  const grid = $('emoji-grid');
  if (grid.childElementCount > 0) return; // al gebouwd
  EMOJIS.forEach(e => {
    const b = document.createElement('button');
    b.type = 'button';
    b.textContent = e;
    b.onclick = () => insertEmoji(e);
    grid.appendChild(b);
  });
}

function toggleEmojiPicker(ev){
  ev.stopPropagation();
  buildEmojiGrid();
  $('emoji-picker').classList.toggle('show');
}

function insertEmoji(em){
  const inp = $('txt');
  if (inp.disabled) return;
  const start = inp.selectionStart ?? inp.value.length;
  const end   = inp.selectionEnd   ?? inp.value.length;
  inp.value = inp.value.substring(0, start) + em + inp.value.substring(end);
  inp.focus();
  const pos = start + em.length;
  inp.setSelectionRange(pos, pos);
}

document.addEventListener('click', () => $('emoji-picker').classList.remove('show'));

/* ============== hashtags ============== */
async function promptAddHashtag(){
  const raw = prompt('Naam voor hashtag-kanaal (bv "gezin" of "#weer"):');
  if (!raw) return;
  const name = raw.trim();
  if (!name || name === '#') { toast('lege naam', 'err'); return; }
  try {
    const r = await api('/admin/channels/add', {
      method:'POST',
      body: JSON.stringify({kind:'hashtag', name}),
    });
    await refresh();
    $('grp-chat').classList.remove('folded');
    // Backend retourneert r.name MET '#' prefix
    selectChannel({kind:'hashtag', idx:r.slot, name:r.name});
    toast('hashtag toegevoegd op slot ' + r.slot, 'ok');
  } catch(e){}
}
async function removeChannelFromTree(slot, displayName){
  const msg = "Hashtag-kanaal " + displayName + " (slot " + slot + ") verwijderen uit DB?\\n\\nLet op: de slot blijft op de companion bestaan tot je hem via Admin overschrijft.";
  if (!confirm(msg)) return;
  try {
    await api('/admin/channels/remove', {method:'POST', body:JSON.stringify({slot})});
    if (STATE.view==='chat' && STATE.channel.idx === slot) {
      selectChannel({kind:'public', idx:0, name:'Public'});
    }
    await refresh();
    toast('verwijderd', 'ok');
  } catch(e){}
}

/* ============== admin views (5 sub-pages) ============== */
function renderAdmin(){
  const sub = STATE.adminSub || 'radio';
  switch (sub) {
    case 'radio':        return renderAdminRadio();
    case 'node':         return renderAdminNode();
    case 'prefs':        return renderAdminPrefs();
    case 'channels':     return renderAdminChannels();
    case 'contacts':     return renderAdminContacts();
    case 'bots':         return renderAdminBots();
    case 'housekeeping': return renderAdminHousekeeping();
    case 'users':        return renderAdminUsers();
    default:             return renderAdminRadio();
  }
}

function renderAdminRadio(){
  const s = STATE.status || {radio:{}};
  $('admin-view').innerHTML = `
    <section><h2>Status</h2><div class="kv" id="adm-status"></div></section>
    <section><h2>Radio</h2>
      <div class="kv" id="adm-radio-cur"></div>
      <div class="row" style="margin-top:10px"><label>Frequentie</label><input id="r-freq" type="number" step="0.001"> MHz</div>
      <div class="row"><label>Bandwidth</label><input id="r-bw" type="number" step="0.01"> kHz</div>
      <div class="row"><label>Spreading</label><input id="r-sf" type="number" min="5" max="12"></div>
      <div class="row"><label>Coding</label><input id="r-cr" type="number" min="5" max="8"></div>
      <div class="row"><button onclick="setRadio()">Radio toepassen</button><span class="note">reboot vereist</span></div>
      <div class="row" style="margin-top:14px"><label>Tx-power</label><input id="r-tx" type="number"> dBm <button onclick="setTxPower()">Power toepassen</button></div>
      <div class="row" style="margin-top:14px">
        <label>Path-hash mode</label>
        <select id="r-phm">
          <option value="0">0  (1-byte hashes)</option>
          <option value="1">1  (2-byte hashes)</option>
          <option value="2">2  (3-byte hashes)</option>
          <option value="3">3  (4-byte hashes)</option>
        </select>
        <button onclick="setPathHashMode()">Toepassen</button>
        <span class="note">experimenteel — alle nodes in mesh moeten gelijk zijn</span>
      </div>
    </section>

    <section><h2>Advert verzenden</h2>
      <div class="row">
        <button onclick="sendAdvert(false)">Zero-hop</button>
        <button onclick="sendAdvert(true)">Flood</button>
        <span class="note">zero-hop = alleen directe buren · flood = via alle repeaters</span>
      </div>
    </section>`;
  const n = s.node || {};
  const r = s.radio || {};
  const battStr = (typeof n.battery_v === 'number')
    ? (n.battery_v.toFixed(3) + ' V (' + n.battery_mv + ' mV)')
    : (n.battery_mv != null ? n.battery_mv + ' mV' : null);
  $('adm-status').innerHTML = fmtKV({
    'naam':           n.name,
    'pubkey_prefix':  n.pubkey,
    'model':          n.model,
    'firmware':       n.firmware,
    'batterij':       battStr,
    'uptime node':    n.uptime_node,
    'uptime gateway': n.uptime_gw,
  });
  $('adm-radio-cur').innerHTML = fmtKV({
    'freq (MHz)':  r.freq,
    'bw (kHz)':    r.bw,
    'sf':          r.sf,
    'cr':          r.cr,
    'tx_power':    (r.tx_power!=null ? r.tx_power+' / '+r.max_tx_power+' dBm' : null),
    'last RSSI':   (r.last_rssi!=null ? r.last_rssi+' dBm' : null),
    'last SNR':    (r.last_snr!=null  ? r.last_snr+' dB'   : null),
    'noise floor': (r.noise_floor!=null ? r.noise_floor+' dBm' : null),
  });
  $('r-freq').value=s.radio.freq||''; $('r-bw').value=s.radio.bw||''; $('r-sf').value=s.radio.sf||''; $('r-cr').value=s.radio.cr||''; $('r-tx').value=s.radio.tx_power||'';
  if (typeof n.path_hash_mode === 'number') $('r-phm').value = String(n.path_hash_mode);
}

async function sendAdvert(flood){
  const kind = flood ? 'flood (door alle repeaters)' : 'zero-hop (alleen directe buren)';
  if (!confirm('Advert verzenden — ' + kind + '?')) return;
  try {
    const r = await api('/admin/advert', {method:'POST', body:JSON.stringify({flood})});
    toast(r.message || 'ok', 'ok');
  } catch(e){}
}

async function setPathHashMode(){
  const mode = parseInt($('r-phm').value);
  if (!confirm('Path-hash mode → '+mode+' (= '+(mode+1)+'-byte hashes)?\\nAlle nodes in de mesh moeten dezelfde mode hebben.')) return;
  try {
    const r = await api('/admin/path-hash-mode', {method:'POST', body:JSON.stringify({mode})});
    toast(r.message || 'ok', 'ok');
    refresh();
  } catch(e){}
}

function renderAdminNode(){
  const s = STATE.status || {radio:{},node:{}};
  $('admin-view').innerHTML = `
    <section><h2>Node</h2>
      <div class="row"><label>Naam</label><input id="n-name" type="text"><button onclick="setName()">Wijzigen</button></div>
      <div class="row"><label>Locatie</label><input id="n-lat" type="number" step="0.000001" placeholder="lat" style="flex:0;width:140px"><input id="n-lon" type="number" step="0.000001" placeholder="lon" style="flex:0;width:140px"><button onclick="setCoords()">Set</button><button onclick="clearCoords()" class="small">clear</button></div>
      <div class="row" style="margin-top:14px"><button onclick="rebootNode()" class="danger">Reboot companion</button><span class="note">~10s offline</span></div>
    </section>`;
  $('n-name').value=s.node?.name||''; $('n-lat').value=s.radio?.lat||''; $('n-lon').value=s.radio?.lon||'';
}

function renderAdminChannels(){
  const s = STATE.status || {channels:[]};
  $('admin-view').innerHTML = `
    <section><h2>Channels (slots)</h2>
      <table id="ch-tbl"><thead><tr><th>#</th><th>Type</th><th>Naam</th><th>Alias</th><th>Scope</th><th></th></tr></thead><tbody></tbody></table>
      <div class="row" style="margin-top:10px">
        <select id="ch-kind"><option value="private">private (128-bit AES)</option><option value="hashtag">hashtag (default PSK)</option></select>
        <input id="ch-slot" type="number" min="1" max="7" placeholder="slot (auto)" style="width:90px">
        <input id="ch-name" type="text" placeholder="naam">
        <input id="ch-key" type="text" placeholder="hex-key (private only, leeg = random)" style="flex:2">
        <button onclick="addChannel()">Toevoegen</button>
      </div>
      <div class="note">Slot 0 = Public (vast). Hashtag-kanalen gebruiken de standaard publieke PSK; private kanalen krijgen een unieke 128-bit AES-key.</div>
      <div class="note">Scope (per kanaal, optioneel): bv. "#europa" of "#nl-noord" — wordt vóór elke send als flood-scope gezet via set_flood_scope().</div>
    </section>`;
  const tb=document.querySelector('#ch-tbl tbody'); tb.innerHTML='';
  (s.channels||[]).forEach(c=>{
    const tr=document.createElement('tr');
    const scopeBtn = '<button class="small" onclick="editScope('+c.idx+')">scope</button>';
    const removeBtn = (c.idx===0?'':' <button class="small danger" onclick="removeChannel('+c.idx+')">remove</button>');
    tr.innerHTML='<td>'+c.idx+'</td><td>'+(c.kind||'?')+'</td><td>'+escapeHTML(c.name||'')+'</td><td>'+escapeHTML(c.alias||'')+'</td><td>'+escapeHTML(c.scope||'—')+'</td><td>'+scopeBtn+removeBtn+'</td>';
    tb.appendChild(tr);
  });
}

async function editScope(slot){
  const cur = (STATE.channels.find(x=>x.idx===slot) || {}).scope || '';
  const v = prompt('Scope voor slot '+slot+' (leeg = geen scope):', cur);
  if (v === null) return;
  try {
    const r = await api('/admin/channels/scope', {method:'POST', body:JSON.stringify({slot, scope: v})});
    toast(r.message || 'ok', 'ok');
    refresh();
  } catch(e){}
}

function renderAdminHousekeeping(){
  const s = STATE.status || {db:{count:0}};
  $('admin-view').innerHTML = `
    <section><h2>Housekeeping</h2>
      <div class="row"><label>DB-records</label><span id="db-count" class="kv">…</span></div>
      <div class="row" style="margin-top:10px"><label>Verwijder ouder dan</label><input id="hk-age" type="number" placeholder="aantal" style="width:90px"><select id="hk-unit"><option value="86400">dagen</option><option value="3600">uren</option><option value="60">minuten</option></select><button onclick="cleanOlder()" class="danger">Verwijder</button></div>
      <div class="row"><button onclick="cleanAll()" class="danger">Alles verwijderen</button><button onclick="vacuum()">VACUUM</button></div>
    </section>`;
  $('db-count').textContent = (s.db?.count ?? '?') + ' berichten';
}

function _formatPeriodHours(h){
  if (h >= 168)  return Math.round(h/168) + 'd';
  if (h >= 24)   return Math.round(h/24) + 'd';
  return h + 'u';
}

function _fmtAxisTime(secsAgo){
  // secs ago → "-Xh", "-Xm", "-Xd"
  if (secsAgo < 60)        return '-' + Math.round(secsAgo) + 's';
  if (secsAgo < 3600)      return '-' + Math.round(secsAgo/60) + 'm';
  if (secsAgo < 86400)     return '-' + Math.round(secsAgo/3600) + 'u';
  return '-' + Math.round(secsAgo/86400) + 'd';
}

async function renderAdminPrefs(){
  const el = $('admin-view');
  el.innerHTML = '<section><h2>Voorkeuren</h2><div class="kv">…laden…</div></section>';
  let p;
  try { p = await api('/admin/prefs'); } catch(e) { return; }

  const telOpts = '<option value="0">0 — uit</option>' +
                  '<option value="1">1 — on-request</option>' +
                  '<option value="2">2 — autonoom</option>' +
                  '<option value="3">3 — beide</option>';
  const advOpts = '<option value="0">0 — geen locatie</option>' +
                  '<option value="1">1 — exact</option>' +
                  '<option value="2">2 — geblurd</option>' +
                  '<option value="3">3 — alleen aan contacten</option>';

  el.innerHTML = `
    <section><h2>Contact-policy</h2>
      <div class="row">
        <label>Auto-add adverts</label>
        <select id="pf-mac">
          <option value="false">aan (auto)</option>
          <option value="true">uit (alleen handmatig)</option>
        </select>
      </div>
      <div class="note">Bepaalt of nieuwe contacten (clients én repeaters) automatisch worden opgeslagen wanneer hun advert binnenkomt. MeshCore biedt geen filter per type — alles-of-niets.</div>
      <div class="row" style="margin-top:14px">
        <button onclick="savePrefSingle('manual_add_contacts', $('pf-mac').value === 'true')">Opslaan</button>
      </div>
    </section>

    <section><h2>Locatie & adverts</h2>
      <div class="row">
        <label>Adv-loc-policy</label>
        <select id="pf-alp">${advOpts}</select>
        <button onclick="savePrefSingle('adv_loc_policy', parseInt($('pf-alp').value))">Opslaan</button>
      </div>
    </section>

    <section><h2>Telemetry</h2>
      <div class="row">
        <label>Base</label><select id="pf-tb">${telOpts}</select>
        <button onclick="savePrefSingle('telemetry_mode_base', parseInt($('pf-tb').value))">Opslaan</button>
      </div>
      <div class="row">
        <label>Locatie</label><select id="pf-tl">${telOpts}</select>
        <button onclick="savePrefSingle('telemetry_mode_loc', parseInt($('pf-tl').value))">Opslaan</button>
      </div>
      <div class="row">
        <label>Environment</label><select id="pf-te">${telOpts}</select>
        <button onclick="savePrefSingle('telemetry_mode_env', parseInt($('pf-te').value))">Opslaan</button>
      </div>
    </section>

    <section><h2>Berichten</h2>
      <div class="row">
        <label>Multi-acks</label>
        <input id="pf-ma" type="number" min="0" max="3" style="width:80px">
        <button onclick="savePrefSingle('multi_acks', parseInt($('pf-ma').value))">Opslaan</button>
        <span class="note">aantal extra ack-herhalingen voor DM's (0 = standaard)</span>
      </div>
      <div class="note">
        <b>Niet beschikbaar via SDK</b>: <i>auto-retry</i>, <i>auto-reset path</i> en <i>direct-msg-acks aan/uit</i> zijn geen losse settings in de companion-API. DM-retries worden door de firmware zelf afgehandeld op basis van <code>multi_acks</code> en de path-hash.
      </div>
    </section>
  `;
  $('pf-mac').value = p.manual_add_contacts ? 'true' : 'false';
  if (p.adv_loc_policy != null)      $('pf-alp').value = String(p.adv_loc_policy);
  if (p.telemetry_mode_base != null) $('pf-tb').value  = String(p.telemetry_mode_base);
  if (p.telemetry_mode_loc != null)  $('pf-tl').value  = String(p.telemetry_mode_loc);
  if (p.telemetry_mode_env != null)  $('pf-te').value  = String(p.telemetry_mode_env);
  if (p.multi_acks != null)          $('pf-ma').value  = String(p.multi_acks);
}

async function savePrefSingle(key, value){
  const body = {}; body[key] = value;
  try {
    const r = await api('/admin/prefs', {method:'POST', body:JSON.stringify(body)});
    toast(r.message || 'ok', 'ok');
  } catch(e){}
}

async function renderAdminContacts(){
  const el = $('admin-view');
  el.innerHTML = '<section><h2>Contacten</h2><div class="kv">…laden…</div></section>';
  let cs;
  try { cs = await api('/contacts'); } catch(e) { return; }

  const typeLabel = t => (t === 1 ? 'client' : t === 2 ? 'repeater' : t === 3 ? 'room' : '?');
  const rows = cs.map(c => {
    const last = c.last_advert ? new Date(c.last_advert*1000).toLocaleString() : '—';
    const acts = '<button class="small danger" onclick="removeContact(\\''+c.pubkey+'\\',\\''+escapeHTML(c.name||'').replace(/\\\\/g,"\\\\\\\\").replace(/'/g,"\\\\'")+'\\')">remove</button>';
    return '<tr>' +
      '<td>' + escapeHTML(c.name || '?') + '</td>' +
      '<td>' + typeLabel(c.type) + '</td>' +
      '<td><code style="font-size:11px">' + c.pubkey_prefix + '</code></td>' +
      '<td>' + escapeHTML(last) + '</td>' +
      '<td>' + acts + '</td>' +
    '</tr>';
  }).join('');

  el.innerHTML = `
    <section><h2>Bekende contacten (${cs.length})</h2>
      <table style="width:100%">
        <thead><tr>
          <th>Naam</th><th>Type</th><th>Pubkey</th><th>Laatste advert</th><th></th>
        </tr></thead>
        <tbody>${rows || '<tr><td colspan="5" style="color:#888">geen contacten</td></tr>'}</tbody>
      </table>
    </section>
  `;
}

async function removeContact(pubkey, name){
  if (!confirm('Contact "'+name+'" verwijderen uit de companion?')) return;
  try {
    const r = await api('/contacts/remove', {method:'POST', body:JSON.stringify({key: pubkey})});
    toast(r.message || 'ok', 'ok');
    refresh();
    renderAdminContacts();
  } catch(e){}
}

async function renderAdminBots(){
  const el = $('admin-view');
  el.innerHTML = '<section><h2>Bots</h2><div class="kv">…laden…</div></section>';
  let bots;
  try { bots = await api('/admin/bots'); } catch(e) { return; }

  // Build channel options from current channels
  const chanOpts = (STATE.channels || []).map(c => {
    const lbl = c.alias || c.name || ('slot ' + c.idx);
    return '<option value="'+c.idx+'">[' + c.idx + '] ' + escapeHTML(lbl) + '</option>';
  }).join('');

  const ICON_PENCIL = '<svg viewBox="0 0 24 24"><path d="M3 17.25V21h3.75L17.81 9.94l-3.75-3.75L3 17.25zM20.71 7.04a1 1 0 0 0 0-1.41l-2.34-2.34a1 1 0 0 0-1.41 0l-1.83 1.83 3.75 3.75 1.83-1.83z"/></svg>';
  const ICON_POWER  = '<svg viewBox="0 0 24 24"><path d="M13 3h-2v10h2V3zm4.83 2.17l-1.42 1.42A6.92 6.92 0 0 1 19 12a7 7 0 0 1-14 0c0-2.18 1-4.13 2.58-5.42L6.17 5.17A8.94 8.94 0 0 0 3 12a9 9 0 0 0 18 0c0-2.74-1.23-5.18-3.17-6.83z"/></svg>';
  const ICON_TRASH  = '<svg viewBox="0 0 24 24"><path d="M9 3v1H4v2h16V4h-5V3H9zm-3 5l1 13h10l1-13H6zm3 2h2v9H9v-9zm4 0h2v9h-2v-9z"/></svg>';

  const rows = bots.map(b => {
    const chanLbl = (() => {
      const c = (STATE.channels || []).find(x => x.idx === b.channel_idx);
      return c ? (c.alias || c.name || ('slot ' + c.idx)) : ('slot ' + b.channel_idx);
    })();
    const en = b.enabled
      ? '<span style="color:#161;font-weight:600">aan</span>'
      : '<span style="color:#888">uit</span>';
    const toggleClass = b.enabled ? 'toggle-on' : 'toggle-off';
    const toggleTitle = b.enabled ? 'uit zetten' : 'aan zetten';
    return '<tr>' +
      '<td>' + escapeHTML(b.name) + '</td>' +
      '<td>' + escapeHTML(chanLbl) + '</td>' +
      '<td><code style="font-size:13px">?'+escapeHTML(b.keyword)+'</code></td>' +
      '<td>' + en + '</td>' +
      '<td style="font-family:ui-monospace,monospace;font-size:12px;word-break:break-word">' +
        escapeHTML(b.reply || '') + '</td>' +
      '<td style="white-space:nowrap">' +
        '<button class="btn-icon" title="bewerken" onclick="botEditPrompt('+b.id+')">'+ICON_PENCIL+'</button>' +
        '<button class="btn-icon '+toggleClass+'" title="'+toggleTitle+'" onclick="botToggle('+b.id+','+!b.enabled+')">'+ICON_POWER+'</button>' +
        '<button class="btn-icon danger" title="verwijderen" onclick="botRemove('+b.id+')">'+ICON_TRASH+'</button>' +
      '</td>' +
    '</tr>';
  }).join('');

  el.innerHTML = `
    <section><h2>Bots (${bots.length})</h2>
      <table style="width:100%;table-layout:fixed">
        <colgroup>
          <col style="width:18%">
          <col style="width:14%">
          <col style="width:14%">
          <col style="width:8%">
          <col>
          <col style="width:120px">
        </colgroup>
        <thead><tr>
          <th>Naam</th><th>Kanaal</th><th>Keyword</th><th>Status</th><th>Reply</th><th></th>
        </tr></thead>
        <tbody>${rows || '<tr><td colspan="6" style="color:#888">geen bots</td></tr>'}</tbody>
      </table>
      <div class="note">Bots reageren alleen op berichten waarin <code>@[<naam-van-deze-node>]</code> én <code>?keyword</code> voorkomen.</div>
    </section>

    <section><h2>Bot toevoegen</h2>
      <div class="row"><label>Naam</label><input id="bt-name" type="text" placeholder="bv 'Tijd-bot'"></div>
      <div class="row"><label>Beschrijving</label><input id="bt-desc" type="text" placeholder="optioneel"></div>
      <div class="row"><label>Kanaal</label><select id="bt-chan">${chanOpts}</select></div>
      <div class="row"><label>Keyword</label><span style="font-family:monospace">?</span><input id="bt-kw" type="text" placeholder="bv 'tijd' (zonder ?)"></div>
      <div class="row" style="align-items:flex-start">
        <label>Reply-template</label>
        <textarea id="bt-reply" rows="3" placeholder="bv 'Het is nu {TIME}'" style="flex:1;padding:6px 8px;font:inherit;border:1px solid #ccc;border-radius:4px;font-family:ui-monospace,monospace;font-size:13px"></textarea>
      </div>
      <div class="row">
        <label></label>
        <span class="note">Variabelen: <code>{TIME}</code> · <code>{UPRADIO}</code> · <code>{UPNODE}</code> · <code>{HELP}</code></span>
      </div>
      <div class="row"><button onclick="botAdd()">Toevoegen</button></div>
    </section>`;
}

async function botAdd(){
  const name = $('bt-name').value.trim();
  const desc = $('bt-desc').value.trim();
  const chan = parseInt($('bt-chan').value);
  const kw   = $('bt-kw').value.trim();
  const rep  = $('bt-reply').value;
  if (!name || !kw || !rep) { toast('naam, keyword en reply vereist','err'); return; }
  try {
    const r = await api('/admin/bots/add', {method:'POST', body:JSON.stringify({
      name, description: desc || null, channel_idx: chan,
      keyword: kw, reply: rep, enabled: true,
    })});
    toast(r.message || 'ok', 'ok');
    ['bt-name','bt-desc','bt-kw','bt-reply'].forEach(i => $(i).value = '');
    renderAdminBots();
  } catch(e){}
}

async function botToggle(id, newState){
  try {
    await api('/admin/bots/update', {method:'POST', body:JSON.stringify({id, enabled: newState})});
    renderAdminBots();
  } catch(e){}
}

async function botRemove(id){
  if (!confirm('Bot verwijderen?')) return;
  try {
    const r = await api('/admin/bots/remove', {method:'POST', body:JSON.stringify({id})});
    toast(r.message || 'ok', 'ok');
    renderAdminBots();
  } catch(e){}
}

async function botEditPrompt(id){
  // Pak huidige bot uit de lijst (eenvoudige edit zonder modal)
  try {
    const bots = await api('/admin/bots');
    const b = bots.find(x => x.id === id);
    if (!b) return;
    const newName = prompt('Naam:', b.name);
    if (newName === null) return;
    const newKw = prompt('Keyword (zonder ?):', b.keyword);
    if (newKw === null) return;
    const newReply = prompt('Reply-template:', b.reply);
    if (newReply === null) return;
    const newChan = prompt('Kanaal (slot-nummer):', String(b.channel_idx));
    if (newChan === null) return;
    const r = await api('/admin/bots/update', {method:'POST', body:JSON.stringify({
      id, name: newName, keyword: newKw, reply: newReply,
      channel_idx: parseInt(newChan),
    })});
    toast(r.message || 'ok', 'ok');
    renderAdminBots();
  } catch(e){}
}

async function renderReports(){
  if (STATE.reportSub === 'repeaters') return renderReportRepeaters();
  return renderReportOverview();
}

async function renderReportOverview(){
  const el = $('reports-view');
  el.innerHTML = '<section><h2>Overzicht</h2><div class="kv">…laden…</div></section>';
  const hours = STATE.reportPeriodHours || 24;
  let data;
  try {
    data = await api('/reports/overview?hours=' + hours);
  } catch(e) { return; }

  // ----- Bar-chart -----
  const buckets = data.buckets || [];
  const N = buckets.length || 1;
  const rawMax = Math.max(1, ...buckets);
  // Rond max omhoog naar dichtstbijzijnde 10-tal (min 10 voor leesbaarheid)
  const yMax = Math.max(10, Math.ceil(rawMax / 10) * 10);

  const PAD_L = 36, PAD_R = 8, PAD_T = 8, PAD_B = 24;
  const PLOT_W = 560, PLOT_H = 110;
  const W = PAD_L + PLOT_W + PAD_R, H = PAD_T + PLOT_H + PAD_B;
  const BW = PLOT_W / N;

  // Y-as gridlines op 0, 1/5, 2/5, 3/5, 4/5, 5/5
  let yLines = '';
  for (let i = 0; i <= 5; i++) {
    const v = Math.round(yMax * i / 5);
    const y = PAD_T + PLOT_H - (v / yMax) * PLOT_H;
    yLines +=
      '<line x1="'+PAD_L+'" y1="'+y+'" x2="'+(PAD_L+PLOT_W)+'" y2="'+y+'" stroke="#eee" stroke-width="1"/>' +
      '<text x="'+(PAD_L-4)+'" y="'+(y+3)+'" fill="#888" text-anchor="end" font-size="10">'+v+'</text>';
  }

  // X-as gridlines + labels op 6 punten (1/6 deelpunten)
  const periodSecs = hours * 3600;
  let xLines = '';
  for (let i = 0; i <= 6; i++) {
    const x = PAD_L + (PLOT_W * i / 6);
    xLines += '<line x1="'+x+'" y1="'+PAD_T+'" x2="'+x+'" y2="'+(PAD_T+PLOT_H)+'" stroke="#f4f4f4"/>';
    if (i === 6) {
      xLines += '<text x="'+x+'" y="'+(H-6)+'" fill="#666" text-anchor="end" font-size="10">nu</text>';
    } else {
      const secsAgo = periodSecs * (1 - i/6);
      xLines += '<text x="'+x+'" y="'+(H-6)+'" fill="#888" text-anchor="middle" font-size="10">'+_fmtAxisTime(secsAgo)+'</text>';
    }
  }

  // Bars
  const bars = buckets.map((v,i) => {
    const h = (v / yMax) * PLOT_H;
    const x = PAD_L + i * BW + 1;
    const y = PAD_T + PLOT_H - h;
    return '<rect x="'+x+'" y="'+y+'" width="'+(BW-2)+'" height="'+h+'" fill="#2c5"/>';
  }).join('');

  const chart =
    '<svg width="'+W+'" height="'+H+'" style="display:block;max-width:100%">' +
    yLines + xLines + bars +
    '<line x1="'+PAD_L+'" y1="'+(PAD_T+PLOT_H)+'" x2="'+(PAD_L+PLOT_W)+'" y2="'+(PAD_T+PLOT_H)+'" stroke="#888"/>' +
    '</svg>';

  // ----- Top channels -----
  const chanRows = (data.top_channels || []).map(c => {
    const dbc = STATE.channels.find(x => x.idx === c.channel_idx);
    const naam = dbc ? (dbc.alias || dbc.name || ('CH'+c.channel_idx)) : ('CH'+c.channel_idx);
    return '<tr><td>'+escapeHTML(naam)+'</td><td style="text-align:right">'+c.count+'</td></tr>';
  }).join('');

  // ----- Ack-rate -----
  const ackRate = data.ack_rate;
  const ackPct = (typeof ackRate === 'number') ? Math.round(ackRate*100)+'%' : '—';
  const ackC = data.ack_count || {};

  el.innerHTML = `
    <section><h2>Totalen</h2><div class="kv">
      <div><span class="k">laatste 24u:</span>${data.totals.last_24h}</div>
      <div><span class="k">laatste 7d:</span>${data.totals.last_7d}</div>
      <div><span class="k">totaal in DB:</span>${data.totals.total}</div>
    </div></section>

    <section>
      <div style="display:flex;justify-content:space-between;align-items:center">
        <h2 style="margin:0;border:0;padding:0">Berichten per periode</h2>
        <div>
          <label style="font-size:0.85em;color:#666">periode</label>
          <select id="rp-period" onchange="setReportPeriod(this.value)">
            <option value="168">7 dagen</option>
            <option value="48">48 uur</option>
            <option value="24">24 uur</option>
            <option value="12">12 uur</option>
            <option value="4">4 uur</option>
            <option value="1">1 uur</option>
          </select>
        </div>
      </div>
      <div style="margin-top:10px;overflow-x:auto">${chart}</div>
      <div class="note">kolom: ${data.bucket_secs}s</div>
    </section>

    <section><h2>Top-kanalen (laatste 7 dagen)</h2>
      <table><thead><tr><th>Kanaal</th><th style="text-align:right">Aantal</th></tr></thead>
      <tbody>${chanRows || '<tr><td colspan="2" style="color:#888">geen data</td></tr>'}</tbody></table>
    </section>

    <section><h2>Ack-rate (DM, ${_formatPeriodHours(hours)})</h2><div class="kv">
      <div><span class="k">ack-rate:</span>${ackPct}</div>
      <div><span class="k">verzonden:</span>${ackC.sent ?? '—'}</div>
      <div><span class="k">bevestigd:</span>${ackC.acked ?? '—'}</div>
    </div>
    <div class="note">Channels acken niet in MeshCore-protocol — alleen DMs tellen mee.</div>
    </section>`;

  // Selecteer huidige periode in de dropdown
  const sel = $('rp-period');
  if (sel) sel.value = String(hours);
}

function setReportPeriod(h){
  STATE.reportPeriodHours = parseFloat(h);
  renderReports();
}

async function renderReportRepeaters(){
  const el = $('reports-view');
  el.innerHTML = '<section><h2>Repeaters</h2><div class="kv">…laden…</div></section>';
  let data;
  try {
    data = await api('/reports/repeaters');
  } catch(e) { return; }

  const rows = (data.repeaters || []).map(r => {
    const lastAdv = r.last_advert
      ? new Date(r.last_advert * 1000).toLocaleString()
      : '—';
    const loc = (typeof r.lat === 'number' && typeof r.lon === 'number' && (r.lat || r.lon))
      ? r.lat.toFixed(4) + ', ' + r.lon.toFixed(4)
      : '—';
    const hashCell = '<code style="background:#dfeefd;padding:1px 4px;border-radius:3px">'+r.hash_1b+'</code>';
    const opl = (r.out_path_len === -1 || r.out_path_len === 255) ? 'flood' : (r.out_path_len ?? '—');
    return '<tr>' +
      '<td>' + escapeHTML(r.name || '?') + '</td>' +
      '<td>' + r.type_label + '</td>' +
      '<td>' + hashCell + '</td>' +
      '<td><code style="font-size:11px">' + r.pubkey_prefix + '</code></td>' +
      '<td>' + escapeHTML(lastAdv) + '</td>' +
      '<td>' + escapeHTML(loc) + '</td>' +
      '<td>' + opl + '</td>' +
      '</tr>';
  }).join('');

  el.innerHTML = `
    <section><h2>Repeaters & Rooms (${data.count})</h2>
      <div class="note" style="margin-bottom:8px">Bron: contactenlijst van de companion (alle nodes met type repeater of room-server).</div>
      <table style="width:100%">
        <thead><tr>
          <th>Naam</th><th>Type</th><th>Hash</th><th>Pubkey-prefix</th>
          <th>Laatste advert</th><th>Locatie</th><th>Path</th>
        </tr></thead>
        <tbody>${rows || '<tr><td colspan="7" style="color:#888">geen bekende repeaters — wacht tot er adverts binnenkomen</td></tr>'}</tbody>
      </table>
    </section>`;
}

function renderAdminUsers(){
  $('admin-view').innerHTML = `
    <section><h2>Gebruikers</h2>
      <table id="usr-tbl"><thead><tr><th>Naam</th><th>Rol</th><th>Wachtwoord</th><th>Laatste login</th><th></th></tr></thead><tbody></tbody></table>
      <div class="row" style="margin-top:10px">
        <input id="usr-name" type="text" placeholder="gebruikersnaam" style="flex:1">
        <select id="usr-role"><option value="user">user</option><option value="admin">admin</option></select>
        <input id="usr-temp-pw" type="text" placeholder="tijdelijk wachtwoord (min 6)" style="flex:1.4">
        <button onclick="addUser()">Toevoegen</button>
      </div>
      <div class="note">Bij eerste login moet de gebruiker dit tijdelijke wachtwoord wijzigen.</div>
    </section>`;
  loadUsers();
}

async function loadUsers(){
  try {
    const users = await api('/admin/users');
    const tb = document.querySelector('#usr-tbl tbody');
    if (!tb) return;
    tb.innerHTML = '';
    users.forEach(u => {
      const tr = document.createElement('tr');
      const last = u.last_login ? new Date(u.last_login).toLocaleString() : '—';
      const pwState = u.has_password ? 'gezet' : '<i>nog niet gezet</i>';
      const btns = [];
      if (u.has_password) btns.push('<button class="small" onclick="resetUserPw(\\''+u.username+'\\')">reset pw</button>');
      btns.push('<button class="small danger" onclick="deleteUser(\\''+u.username+'\\')">verwijder</button>');
      tr.innerHTML = '<td>'+escapeHTML(u.username)+'</td><td>'+u.role+'</td><td>'+pwState+'</td><td>'+escapeHTML(last)+'</td><td>'+btns.join(' ')+'</td>';
      tb.appendChild(tr);
    });
  } catch(e){}
}

async function addUser(){
  const username = $('usr-name').value.trim();
  const role = $('usr-role').value;
  const tempPw = $('usr-temp-pw').value;
  if (!username) { toast('naam vereist','err'); return; }
  if (tempPw.length < 6) { toast('tijdelijk ww min 6 tekens','err'); return; }
  try {
    const r = await api('/admin/users/add', {method:'POST',
      body: JSON.stringify({username, role, temp_password: tempPw})});
    toast(r.message || 'ok', 'ok');
    $('usr-name').value = '';
    $('usr-temp-pw').value = '';
    loadUsers();
  } catch(e){}
}

async function deleteUser(username){
  if (!confirm('Gebruiker '+username+' verwijderen?')) return;
  try {
    const r = await api('/admin/users/delete', {method:'POST', body:JSON.stringify({username})});
    toast(r.message || 'ok', 'ok');
    loadUsers();
  } catch(e){}
}

async function resetUserPw(username){
  const tempPw = prompt('Tijdelijk wachtwoord voor '+username+' (min 6 tekens):');
  if (!tempPw) return;
  if (tempPw.length < 6) { toast('min 6 tekens','err'); return; }
  if (!confirm('Wachtwoord van '+username+' resetten naar tijdelijk ww?\\n'+username+' moet bij volgende login zelf nieuw ww instellen.')) return;
  try {
    const r = await api('/admin/users/reset-password', {method:'POST',
      body: JSON.stringify({username, temp_password: tempPw})});
    toast(r.message || 'ok', 'ok');
    loadUsers();
  } catch(e){}
}
function fmtKV(obj){return Object.entries(obj||{}).map(([k,v])=>'<div><span class="k">'+k+':</span>'+(v===null||v===undefined?'<i>—</i>':v)+'</div>').join('');}

async function setRadio(){
  const body={freq:parseFloat($('r-freq').value),bw:parseFloat($('r-bw').value),sf:parseInt($('r-sf').value),cr:parseInt($('r-cr').value)};
  if (!confirm('Radio → freq='+body.freq+' bw='+body.bw+' sf='+body.sf+' cr='+body.cr+'?\\nReboot vereist.')) return;
  const r=await api('/admin/radio',{method:'POST',body:JSON.stringify(body)}); toast(r.message||'ok','ok'); refresh();
}
async function setTxPower(){
  const dbm=parseInt($('r-tx').value);
  if (!confirm('tx-power → '+dbm+' dBm? Reboot vereist.')) return;
  const r=await api('/admin/txpower',{method:'POST',body:JSON.stringify({dbm})}); toast(r.message||'ok','ok'); refresh();
}
async function setName(){
  const name=$('n-name').value.trim(); if(!name) return;
  if (!confirm('Naam → '+name+'?')) return;
  const r=await api('/admin/name',{method:'POST',body:JSON.stringify({name})}); toast(r.message||'ok','ok'); refresh();
}
async function setCoords(){
  const lat=parseFloat($('n-lat').value),lon=parseFloat($('n-lon').value);
  const r=await api('/admin/coords',{method:'POST',body:JSON.stringify({lat,lon})}); toast(r.message||'ok','ok');
}
async function clearCoords(){
  if (!confirm('Coords wissen?')) return;
  const r=await api('/admin/coords',{method:'POST',body:JSON.stringify({clear:true})}); toast(r.message||'ok','ok'); refresh();
}
async function rebootNode(){
  if (!confirm('Companion rebooten?')) return;
  const r=await api('/admin/reboot',{method:'POST',body:'{}'}); toast(r.message||'ok','ok');
}
async function addChannel(){
  const kind = $('ch-kind').value;
  const name = $('ch-name').value.trim();
  const key  = $('ch-key').value.trim();
  const slotStr = $('ch-slot').value.trim();
  const slot = slotStr ? parseInt(slotStr) : null;
  if (!name) { toast('naam vereist','err'); return; }
  if (kind === 'hashtag' && key) {
    toast('hashtag-channel gebruikt de standaard PSK — laat key leeg', 'err');
    return;
  }
  const body = {kind, name};
  if (slot) body.slot = slot;
  if (key)  body.key = key;
  try {
    const r = await api('/admin/channels/add', {method:'POST', body:JSON.stringify(body)});
    toast(r.message + (r.generated_key ? '\\nKEY: '+r.generated_key : ''), 'ok');
    refresh();
  } catch(e){}
}
async function removeChannel(slot){
  if (!confirm('Slot '+slot+' uit DB-metadata verwijderen?')) return;
  await api('/admin/channels/remove',{method:'POST',body:JSON.stringify({slot})}); refresh();
}
async function cleanOlder(){
  const n=parseInt($('hk-age').value),unit=parseInt($('hk-unit').value);
  if (!n){toast('aantal vereist','err');return;}
  const seconds=n*unit;
  const c=await api('/admin/clean/preview?seconds='+seconds);
  if (!confirm(c.count+' berichten ouder dan dat — verwijderen?')) return;
  const r=await api('/admin/clean',{method:'POST',body:JSON.stringify({mode:'older',seconds})}); toast(r.message,'ok'); refresh();
}
async function cleanAll(){
  if (!confirm('ALLE berichten verwijderen?')) return;
  const r=await api('/admin/clean',{method:'POST',body:JSON.stringify({mode:'all'})}); toast(r.message,'ok'); refresh();
}
async function vacuum(){
  if (!confirm('VACUUM uitvoeren?')) return;
  const r=await api('/admin/vacuum',{method:'POST',body:'{}'}); toast(r.message,'ok');
}

/* ============== detail pane ============== */
function renderDetail(){
  const el = $('detail-content');

  // 1) Geselecteerd bericht heeft prioriteit
  if (STATE.view === 'chat' && STATE.selectedMsg) {
    const m = STATE.selectedMsg;
    const sender = m._sender || extractSender(m) || '—';
    const meta = extractMeta(m);
    const isIn = m.direction !== 'out';
    let signalRow = '';
    if (isIn) {
      // RSSI komt in veel firmware-versies niet door op channel/contact-msg
      // events; alleen tonen als 'r een echte waarde is.
      const rows = [];
      if (typeof meta.rssi === 'number') {
        rows.push('<div><span class="k">signaal:</span>'+fmtRssi(meta.rssi)+'</div>');
      }
      if (typeof meta.snr === 'number') {
        rows.push('<div><span class="k">SNR:</span>'+fmtSnr(meta.snr)+'</div>');
      }
      rows.push('<div><span class="k">hops:</span>'+fmtHops(meta.hops)+'</div>');
      signalRow = rows.join('');
    } else {
      // Outgoing: ack-status + latency
      const st = m.ack_status || 'sent';
      const stTxt = st === 'acked' ? '✓✓ bevestigd'
                  : st === 'failed' ? '!! mislukt'
                  : st === 'sent' ? '✓ verzonden (geen ack)'
                  : st;
      signalRow = '<div><span class="k">status:</span>'+escapeHTML(stTxt)+'</div>';
      if (typeof m.latency_s === 'number') {
        signalRow += '<div><span class="k">ack na:</span>'+m.latency_s.toFixed(2)+' s</div>';
      }
    }
    el.innerHTML = `
      <div class="detail-actions">
        <button onclick="replyToSelected()" ${(m.direction==='out')?'disabled style="opacity:0.5;cursor:not-allowed"':''}>Reply</button>
        <button class="sec" onclick="copySelected()">Copy</button>
      </div>
      <div class="detail-section"><h3>Bericht</h3><div class="kv">
        <div><span class="k">tijd:</span>${escapeHTML(fmtTs(m.ts))}</div>
        <div><span class="k">richting:</span>${m.direction==='out'?'uitgaand':'inkomend'}</div>
        <div><span class="k">afzender:</span>${escapeHTML(sender)}</div>
        <div><span class="k">kanaal:</span>${m.kind==='channel'?'CH'+m.channel_idx:'DM'}</div>
        ${signalRow}
      </div></div>
      <div class="detail-section"><h3>Inhoud</h3>
        <div class="msg-quote">${escapeHTML(m._body || m.text || '')}</div>
      </div>
      ${renderPathSection(m)}
      <div class="detail-section">
        <h3>raw payload</h3>
        <div class="msg-quote" style="font-size:11px">${escapeHTML(typeof m.raw === 'string' ? m.raw : JSON.stringify(m.raw, null, 2))}</div>
      </div>`;
    return;
  }

  // 2) Admin-view: system status
  if (STATE.view === 'admin') {
    const s = STATE.status||{node:{},db:{}};
    el.innerHTML = `
      <div class="detail-section"><h3>System</h3><div class="kv">
        <div><span class="k">node:</span>${escapeHTML(s.node.name||'?')}</div>
        <div><span class="k">pubkey:</span>${escapeHTML(s.node.pubkey||'?')}</div>
        <div><span class="k">uptime:</span>${escapeHTML(s.node.uptime||'?')}</div>
        <div><span class="k">batterij:</span>${s.node.battery??'?'}</div>
        <div><span class="k">DB-msgs:</span>${s.db.count??'?'}</div>
      </div></div>`;
    return;
  }

  // 3) Default: kanaal-info uit DB
  const c = STATE.channel;
  const dbc = STATE.channels.find(x => x.idx === c.idx);
  const info = {
    type: dbc ? dbc.kind : c.kind,
    slot: c.idx,
    naam: dbc ? dbc.name : c.name,
    alias: dbc ? (dbc.alias || '—') : '—',
  };
  el.innerHTML = `<div class="detail-section"><h3>Kanaal</h3><div class="kv">${
    Object.entries(info).map(([k,v])=>'<div><span class="k">'+k+':</span>'+escapeHTML(v||'?')+'</div>').join('')
  }</div></div>`;
}

/* Parse meshcore-py raw payload-fields uit een Message.
   Returnt {hops, rssi, snr, txt_type} — met null voor velden die niet
   in de payload zaten. */
function extractMeta(m){
  const meta = {hops: null, rssi: null, snr: null, txt_type: null, ack: null};
  let obj = m && m.raw;
  if (!obj) return meta;
  if (typeof obj === 'string') {
    try { obj = JSON.parse(obj); } catch(e) { return meta; }
  }
  if (!obj || typeof obj !== 'object') return meta;

  // hops = path_len. 255 = direct (geen hops, niet via mesh).
  if (typeof obj.path_len === 'number') {
    meta.hops = (obj.path_len === 255) ? 0 : obj.path_len;
  } else if (typeof obj.hops === 'number') {
    meta.hops = obj.hops;
  }
  // RSSI/SNR: alleen aanwezig als log-channel-decryptie de info kon koppelen
  if (typeof obj.RSSI === 'number') meta.rssi = obj.RSSI;
  else if (typeof obj.rssi === 'number') meta.rssi = obj.rssi;
  if (typeof obj.SNR === 'number')  meta.snr  = obj.SNR;
  else if (typeof obj.snr === 'number')  meta.snr  = obj.snr;
  if ('txt_type' in obj) meta.txt_type = obj.txt_type;
  return meta;
}

/* Cijfer + mooie 📶 voor RSSI met kleur-classificatie. */
function fmtRssi(rssi){
  if (typeof rssi !== 'number') return '—';
  let cls = 'rssi-good';
  if (rssi < -100) cls = 'rssi-bad';
  else if (rssi < -85) cls = 'rssi-mid';
  return '<span class="'+cls+'">📶 '+rssi+' dBm</span>';
}
function fmtSnr(snr){
  if (typeof snr !== 'number') return '—';
  return snr.toFixed(2) + ' dB';
}
function fmtHops(h){
  if (h === null || h === undefined) return '—';
  if (h === 0) return '0 (direct)';
  return String(h);
}

function replyToSelected(){
  const m = STATE.selectedMsg;
  if (!m || m.direction === 'out') return;
  const sender = m._sender || extractSender(m);
  if (!sender) {
    toast('geen afzender bekend om te @-en', 'err');
    return;
  }
  const cur = $('txt').value;
  const prefix = '@[' + sender + '] ';
  // Vervang als er al een @[..] aan het begin staat, anders prepend
  if (/^@\\[[^\\]]+\\]\\s*/.test(cur)) {
    $('txt').value = cur.replace(/^@\\[[^\\]]+\\]\\s*/, prefix);
  } else {
    $('txt').value = prefix + cur;
  }
  $('txt').focus();
  // Cursor aan eind
  const v = $('txt').value;
  $('txt').setSelectionRange(v.length, v.length);
}

function _renderHop(seg){
  // seg = {hash, name?} of een raw hex-string (legacy)
  if (typeof seg === 'string') {
    return '<span class="hop hop-unknown" title="onbekende repeater">'+escapeHTML(seg)+'</span>';
  }
  const hash = seg.hash || '';
  if (seg.name) {
    return '<span class="hop hop-known" title="'+escapeHTML(hash)+'">'+escapeHTML(seg.name)+'</span>';
  }
  return '<span class="hop hop-unknown" title="onbekende repeater">'+escapeHTML(hash)+'</span>';
}

function _renderOnePath(p, opts){
  opts = opts || {};
  const pl = p.path_len;
  const rssi = (typeof p.rssi === 'number') ? (p.rssi+' dBm') : '?';
  const snr  = (typeof p.snr  === 'number') ? (p.snr.toFixed(2)+' dB') : '?';
  const summary = (pl===255?0:pl) + ' hop' + (pl===1?'':'s') + ' · ' + rssi + ' · SNR ' + snr;

  // Bouw hop-chain: gebruik path_names als beschikbaar, anders ruwe path-hex
  let chainHtml;
  if (pl === 0 || pl === 255) {
    chainHtml = '<i style="color:#888">direct (0 hops)</i> <span class="hop-arrow">&rarr;</span> <span class="hop hop-self">jij</span>';
  } else if (Array.isArray(p.path_names) && p.path_names.length > 0) {
    chainHtml = p.path_names.map(_renderHop).join('<span class="hop-arrow">&rarr;</span>') +
                '<span class="hop-arrow">&rarr;</span><span class="hop hop-self">jij</span>';
  } else if (typeof p.path === 'string' && p.path.length > 0) {
    const hashSize = p.path_hash_size || 1;
    const segs = [];
    for (let i = 0; i + hashSize*2 <= p.path.length; i += hashSize*2) {
      segs.push(p.path.substring(i, i+hashSize*2));
    }
    chainHtml = segs.map(_renderHop).join('<span class="hop-arrow">&rarr;</span>') +
                '<span class="hop-arrow">&rarr;</span><span class="hop hop-self">jij</span>';
  } else {
    chainHtml = '<i style="color:#888">'+pl+' hop(s), pad-bytes niet meegestuurd</i>';
  }

  const openAttr = opts.open ? ' open' : '';
  return '<details class="path-row"'+openAttr+'>' +
         '<summary>'+summary+'</summary>' +
         '<div class="hop-chain">'+chainHtml+'</div>' +
         '</details>';
}

function renderPathSection(m){
  if (!m || m.kind !== 'channel' || m.direction === 'out') return '';

  let raw = m.raw;
  if (typeof raw === 'string') {
    try { raw = JSON.parse(raw); } catch(e) { raw = null; }
  }
  if (!raw || typeof raw !== 'object') {
    return '<div class="detail-section"><h3>Pad</h3>' +
      '<div class="kv"><div><i>geen path-info in payload</i></div></div></div>';
  }

  // Multi-path: paths-array met alle ontvangsten van dezelfde msg via
  // verschillende routes (zoals "Heard X Times" in de Android-app).
  if (Array.isArray(raw.paths) && raw.paths.length > 0) {
    // Sorteer op kortste pad eerst (best signal als tie-breaker)
    const sorted = raw.paths.slice().sort((a,b) => {
      const al = (typeof a.path_len === 'number') ? a.path_len : 999;
      const bl = (typeof b.path_len === 'number') ? b.path_len : 999;
      if (al !== bl) return al - bl;
      const sa = (typeof a.snr === 'number') ? a.snr : -999;
      const sb = (typeof b.snr === 'number') ? b.snr : -999;
      return sb - sa;
    });
    const header = sorted.length > 1
      ? ('Paden — gehoord ' + sorted.length + 'x')
      : 'Pad';
    // Eerste (= kortste) standaard open, rest dicht
    const rendered = sorted.map((p, i) => _renderOnePath(p, {open: i === 0})).join('');
    return '<div class="detail-section"><h3>'+header+'</h3>' +
           rendered + '</div>';
  }

  // path_len: 255 = "direct" (geen mesh-hops). Anders = aantal repeaters.
  let pathLen = raw.path_len;
  let directFlag = (pathLen === 255);
  if (directFlag) pathLen = 0;

  if (typeof pathLen !== 'number') {
    return '<div class="detail-section"><h3>Pad</h3>' +
      '<div class="kv"><div><i>path_len niet aanwezig in payload</i></div></div></div>';
  }

  if (pathLen === 0) {
    return '<div class="detail-section"><h3>Pad</h3>' +
      '<div class="kv"><div><i>' + (directFlag ? 'direct (geen tussen-hops)' : '0 hops') +
      '</i></div></div></div>';
  }

  // Hash-size afleiden — eerst expliciet veld, anders uit path/path_len.
  let hashSize = null;
  if (typeof raw.path_hash_size === 'number' && raw.path_hash_size > 0) {
    hashSize = raw.path_hash_size;
  } else if (typeof raw.path_hash_mode === 'number' && raw.path_hash_mode >= 0) {
    hashSize = raw.path_hash_mode + 1;
  } else if (typeof raw.path === 'string' && pathLen > 0) {
    const totalNibbles = raw.path.length;
    if (totalNibbles % (pathLen*2) === 0) {
      hashSize = totalNibbles / (pathLen*2);
    }
  }

  const path = raw.path;
  let segments;
  let extra = '';
  if (typeof path === 'string' && hashSize && path.length >= hashSize*2) {
    const segs = [];
    for (let i = 0; i + hashSize*2 <= path.length; i += hashSize*2) {
      segs.push('<code style="background:#f0f0f0;padding:2px 4px;border-radius:3px">'+escapeHTML(path.substring(i, i+hashSize*2))+'</code>');
    }
    segments = segs.join(' &rarr; ') + ' &rarr; <b>jij</b>';
    extra = '<div class="note" style="margin-top:4px">hash-size: '+hashSize+' byte(s)</div>';
  } else if (typeof path === 'string' && path.length > 0) {
    segments = '<code style="background:#f0f0f0;padding:2px 4px;border-radius:3px">'+escapeHTML(path)+'</code>';
    extra = '<div class="note" style="margin-top:4px">pad-bytes ongesplitst (geen hash-size bekend)</div>';
  } else {
    segments = '<i>'+pathLen+' hop(s), pad-hashes niet meegestuurd</i>' +
               '<div class="note" style="margin-top:4px">' +
               'Tip: zorg dat decrypt-channel-logs aan staat (gateway-log toont "[*] decrypt-channel-logs aan").</div>';
  }

  let attemptInfo = '';
  if (typeof raw.attempt === 'number') {
    attemptInfo = '<div class="note" style="margin-top:4px">attempt: '+raw.attempt+'</div>';
  }

  return '<div class="detail-section"><h3>Pad ('+pathLen+' hop'+(pathLen===1?'':'s')+')</h3>' +
         '<div style="font-size:11px;line-height:1.7">'+segments+'</div>' +
         extra + attemptInfo + '</div>';
}

function toggleRawDetail(h){
  const div = h.nextElementSibling;
  if (!div) return;
  const open = div.style.display !== 'none';
  div.style.display = open ? 'none' : 'block';
  h.innerHTML = 'raw payload ' + (open ? '▸' : '▾');
}

function copySelected(){
  const m = STATE.selectedMsg;
  if (!m) return;
  const text = m._body || m.text || '';
  if (navigator.clipboard) {
    navigator.clipboard.writeText(text).then(
      () => toast('gekopieerd', 'ok'),
      () => toast('kopiëren mislukt', 'err')
    );
  } else {
    // fallback voor non-https of oudere browsers
    const ta = document.createElement('textarea');
    ta.value = text; document.body.appendChild(ta);
    ta.select(); document.execCommand('copy'); ta.remove();
    toast('gekopieerd', 'ok');
  }
}

/* ============== refresh state ============== */
async function refresh(){
  try {
    const [s, c, mc] = await Promise.all([
      api('/admin/state'),
      api('/contacts').catch(() => []),
      api('/my/contacts').catch(() => []),
    ]);
    STATE.status = s;
    STATE.channels = s.channels;
    STATE.contacts = c;
    STATE.myContacts = mc;
    renderTree();
    renderHeaderStatus();
    // Bewust geen renderAdmin/Reports/Contacts hier — die rerenderen
    // zou form-inputs wissen tijdens typen. Sub-views verversen alleen
    // bij user-actie of bij explicit klik in tree.
    renderDetail();
  } catch(e){}
}

function renderHeaderStatus(){
  const n = (STATE.status && STATE.status.node) || {};
  const el = $('hdr-status');
  if (!el) return;
  let bv = '';
  let batClass = '';
  if (typeof n.battery_v === 'number') {
    bv = n.battery_v.toFixed(2) + ' V';
    if (n.battery_v >= 3.7)      batClass = 'bat-good';
    else if (n.battery_v >= 3.4) batClass = 'bat-low';
    else                          batClass = 'bat-crit';
  } else {
    bv = '—';
  }
  el.innerHTML =
    '<span class="item"><span class="lbl">node</span><b>' + escapeHTML(n.name || '?') + '</b></span>' +
    '<span class="item ' + batClass + '"><span class="lbl">bat</span>' + bv + '</span>' +
    '<span class="item"><span class="lbl">up</span>' + escapeHTML(n.uptime_node || '—') + '</span>';
}

/* ============== auth/account helpers ============== */
async function loadMe(){
  try {
    STATE.me = await api('/me');
    $('menu-username').textContent = STATE.me.username + ' (' + STATE.me.role + ')';
    if (STATE.me.role === 'admin') {
      $('menu-quit').style.display = '';
      // Toon admin-groep in tree (was display:none in HTML)
      const adm = $('grp-admin');
      if (adm) adm.style.display = '';
    } else {
      // Geen admin: verwijder de hele admin-groep uit de DOM
      const adm = $('grp-admin');
      if (adm) adm.parentNode.removeChild(adm);
    }
    // Geforceerde wachtwoord-wijziging bij eerste login (tijdelijk ww)
    if (STATE.me.must_change_password) {
      forcedChangePasswordPrompt();
    }
  } catch(e){}
}

async function forcedChangePasswordPrompt(){
  toast('Wachtwoord wijzigen verplicht', 'mention', 5000);
  while (STATE.me && STATE.me.must_change_password) {
    const oldp = prompt('VERPLICHT: huidig (tijdelijk) wachtwoord:');
    if (oldp === null) continue;  // geen cancel toegestaan
    const newp = prompt('Nieuw wachtwoord (min 6 tekens):');
    if (!newp || newp.length < 6) { toast('min 6 tekens', 'err'); continue; }
    const newp2 = prompt('Nieuw wachtwoord nogmaals:');
    if (newp !== newp2) { toast('komt niet overeen', 'err'); continue; }
    try {
      const r = await api('/change-password', {method:'POST',
        body: JSON.stringify({old: oldp, new: newp})});
      toast(r.message || 'ok', 'ok');
      STATE.me.must_change_password = false;
      return;
    } catch(e){
      // api() heeft al getoast met de fout — loop opnieuw
    }
  }
}

async function changePasswordPrompt(){
  const oldp = prompt('Huidig wachtwoord:');
  if (!oldp) return;
  const newp = prompt('Nieuw wachtwoord (min 6 tekens):');
  if (!newp) return;
  const newp2 = prompt('Nieuw wachtwoord nogmaals:');
  if (newp !== newp2) { toast('komt niet overeen', 'err'); return; }
  if (newp.length < 6) { toast('min 6 tekens', 'err'); return; }
  try {
    const r = await api('/change-password', {method:'POST', body:JSON.stringify({old:oldp, new:newp})});
    toast(r.message || 'ok', 'ok');
  } catch(e){}
}

/* ============== native notifications ============== */
async function ensureNotifPerm(){
  if (!('Notification' in window)) return false;
  if (Notification.permission === 'granted') return true;
  if (Notification.permission === 'denied') return false;
  try {
    const r = await Notification.requestPermission();
    return r === 'granted';
  } catch(e) { return false; }
}
// Vraag perm bij eerste user-interactie (autoplay/notif policy vereist gesture)
document.addEventListener('click', () => ensureNotifPerm(), {once:true});

function showNativeNotification(title, body){
  if (!('Notification' in window)) return false;
  if (Notification.permission !== 'granted') return false;
  // Alleen tonen als de tab niet zichtbaar is — in-app toast volstaat anders
  if (!document.hidden) return false;
  try {
    const n = new Notification(title, {body, tag:'meshcore-mention'});
    setTimeout(()=>n.close(), 7000);
    n.onclick = () => { window.focus(); n.close(); };
    return true;
  } catch(e) { return false; }
}

/* boot */
loadMe().then(refresh).then(()=>{
  selectChannel({kind:'public', idx:0, name:'Public'});
  // Filter: server-side search door alle berichten (gedebounced)
  const fi = $('chat-filter');
  if (fi) fi.addEventListener('input', () => {
    STATE.filterText = fi.value.trim().toLowerCase();
    if (_searchTimer) clearTimeout(_searchTimer);
    _searchTimer = setTimeout(() => runServerSearch(STATE.filterText), 300);
  });
});
setInterval(refresh, 30000);          // tree/admin: 30s
setInterval(refreshHeaderOnly, 10000); // header: 10s voor live batterij+uptime

async function refreshHeaderOnly(){
  // Lichtere variant: alleen de status-velden voor de header
  try {
    const s = await api('/admin/state');
    STATE.status = s;
    renderHeaderStatus();
  } catch(e){}
}
</script>
</body></html>"""


CHAT_HTML = """<!DOCTYPE html>
<html lang="nl"><head>
<meta charset="utf-8">
<title>MeshCore Gateway</title>
<style>
  *{box-sizing:border-box}
  body{font-family:system-ui,sans-serif;margin:0;display:flex;flex-direction:column;height:100vh;background:#fafafa}
  header{padding:10px 16px;background:#2c5;color:#fff;font-weight:600;display:flex;justify-content:space-between;align-items:center}
  header a{color:#cfc;text-decoration:none;font-size:0.85em}
  header a:hover{text-decoration:underline}
  #log{flex:1;overflow-y:auto;padding:8px 16px;font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:13px;background:#fff}
  .msg{margin:3px 0;line-height:1.5}
  .out{color:#070}
  .out::before{content:"\\2192  ";color:#7a7;font-weight:bold}
  .in::before{content:"   "}
  .ts{color:#999;margin-right:8px}
  .peer{font-weight:600;margin-right:6px}
  .sys{color:#999;font-style:italic}
  form{display:flex;border-top:1px solid #ddd;padding:8px;background:#f0f0f0}
  #txt{flex:1;padding:10px;font:inherit;border:1px solid #ccc;border-radius:4px}
  button{padding:10px 20px;font:inherit;background:#2c5;color:#fff;border:0;border-radius:4px;margin-left:8px;cursor:pointer;font-weight:600}
  button:hover{background:#1b4}
  button:disabled{background:#aaa;cursor:wait}
</style></head>
<body>
<header>
  <span id="title">MeshCore Gateway — Public</span>
  <span><span id="conn" style="opacity:0.7">verbinden…</span> · <a href="/admin">admin</a> · <a href="/logout">logout</a></span>
</header>
<div id="log"></div>
<form id="f">
  <input id="txt" autocomplete="off" placeholder="Bericht naar Public…" autofocus disabled>
  <button id="btn" type="submit" disabled>Stuur</button>
</form>
<script src="https://cdn.socket.io/4.7.5/socket.io.min.js"></script>
<script>
  const log  = document.getElementById('log');
  const f    = document.getElementById('f');
  const txt  = document.getElementById('txt');
  const btn  = document.getElementById('btn');
  const conn = document.getElementById('conn');

  function escapeHTML(s){return String(s).replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;","\\"":"&quot;","'":"&#39;"})[c]);}
  function add(cls, ts, peer, text){
    const div = document.createElement('div');
    div.className = 'msg ' + cls;
    div.innerHTML = '<span class="ts">'+ts+'</span><span class="peer">'+escapeHTML(peer||'?')+':</span>' + escapeHTML(text);
    log.appendChild(div);
    log.scrollTop = log.scrollHeight;
  }
  function addSys(text){
    const div = document.createElement('div');
    div.className = 'msg sys';
    div.textContent = '— ' + text + ' —';
    log.appendChild(div);
    log.scrollTop = log.scrollHeight;
  }

  const sock = io({transports:['websocket','polling']});
  sock.on('connect',    () => { conn.textContent='verbonden'; conn.style.opacity=1; txt.disabled=false; btn.disabled=false; txt.focus(); });
  sock.on('disconnect', () => { conn.textContent='verbroken'; conn.style.opacity=0.7; txt.disabled=true; btn.disabled=true; });
  sock.on('connect_error', (e) => { conn.textContent='auth fout — login opnieuw'; setTimeout(()=>location.href='/login',1500); });

  sock.on('history', (rows) => {
    log.innerHTML = '';
    rows.forEach(m => add(m.direction, m.ts, m.peer, m.text));
    addSys('einde historie');
  });
  sock.on('msg', (m) => add(m.direction, m.ts, m.peer, m.text));

  f.addEventListener('submit', (e) => {
    e.preventDefault();
    const t = txt.value.trim();
    if (!t) return;
    btn.disabled = true;
    sock.emit('send', {channel_idx: 0, text: t}, (ack) => {
      btn.disabled = false;
      if (!ack || !ack.ok) addSys('verzenden mislukt: ' + (ack && ack.err || 'onbekende fout'));
      txt.focus();
    });
    txt.value = '';
  });
</script>
</body></html>"""


ADMIN_HTML = """<!DOCTYPE html>
<html lang="nl"><head>
<meta charset="utf-8">
<title>MeshCore Gateway — Admin</title>
<style>
  *{box-sizing:border-box}
  body{font-family:system-ui,sans-serif;margin:0;background:#fafafa;color:#222}
  header{padding:10px 16px;background:#2c5;color:#fff;font-weight:600;display:flex;justify-content:space-between;align-items:center}
  header a{color:#cfc;text-decoration:none;font-size:0.85em}
  main{max-width:900px;margin:0 auto;padding:16px}
  section{background:#fff;border-radius:8px;padding:16px;margin-bottom:16px;box-shadow:0 1px 2px rgba(0,0,0,0.05)}
  h2{margin:0 0 12px;font-size:1.1em;color:#2c5;border-bottom:1px solid #eee;padding-bottom:6px}
  table{width:100%;border-collapse:collapse;font-size:0.9em}
  th,td{text-align:left;padding:6px 8px;border-bottom:1px solid #f0f0f0}
  th{background:#f8f8f8;font-weight:600}
  .row{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin:6px 0}
  .row label{flex:0 0 110px;font-size:0.9em;color:#555}
  input,select{padding:6px 8px;font:inherit;border:1px solid #ccc;border-radius:4px}
  input[type=number]{width:90px}
  input[type=text]{flex:1;min-width:180px}
  button{padding:6px 14px;font:inherit;background:#2c5;color:#fff;border:0;border-radius:4px;cursor:pointer;font-weight:600}
  button:hover{background:#1b4}
  button.danger{background:#c53}
  button.danger:hover{background:#a32}
  button.small{padding:3px 8px;font-size:0.85em}
  .kv{font-family:ui-monospace,monospace;font-size:13px;color:#444}
  .kv span.k{display:inline-block;width:140px;color:#888}
  .ok{color:#070}
  .err{color:#c33}
  .note{font-size:0.85em;color:#888;margin-top:4px}
  #toast{position:fixed;bottom:20px;right:20px;padding:10px 16px;background:#222;color:#fff;border-radius:4px;opacity:0;transition:opacity 0.2s;pointer-events:none;max-width:420px}
  #toast.show{opacity:0.95}
</style></head>
<body>
<header>
  <span>MeshCore Gateway — Admin</span>
  <span><a href="/">chat</a> · <a href="/logout">logout</a></span>
</header>
<main>

  <section>
    <h2>Status</h2>
    <div class="kv" id="status">…laden…</div>
  </section>

  <section>
    <h2>Radio</h2>
    <div class="kv" id="radio-current">…</div>
    <div class="row" style="margin-top:10px">
      <label>Frequentie</label><input id="r-freq" type="number" step="0.001"> MHz
    </div>
    <div class="row">
      <label>Bandwidth</label><input id="r-bw" type="number" step="0.01"> kHz
    </div>
    <div class="row">
      <label>Spreading</label><input id="r-sf" type="number" min="5" max="12">
    </div>
    <div class="row">
      <label>Coding</label><input id="r-cr" type="number" min="5" max="8">
    </div>
    <div class="row"><button onclick="setRadio()">Radio toepassen</button>
      <span class="note">reboot vereist om actief te worden</span></div>

    <div class="row" style="margin-top:14px">
      <label>Tx-power</label><input id="r-tx" type="number" min="0" max="30"> dBm
      <button onclick="setTxPower()">Power toepassen</button>
    </div>
  </section>

  <section>
    <h2>Node</h2>
    <div class="row">
      <label>Naam</label><input id="n-name" type="text">
      <button onclick="setName()">Naam wijzigen</button>
    </div>
    <div class="row">
      <label>Locatie</label>
      <input id="n-lat" type="number" step="0.000001" placeholder="lat" style="flex:0;width:140px">
      <input id="n-lon" type="number" step="0.000001" placeholder="lon" style="flex:0;width:140px">
      <button onclick="setCoords()">Set</button>
      <button onclick="clearCoords()" class="small">clear</button>
    </div>
    <div class="row" style="margin-top:14px">
      <button onclick="rebootNode()" class="danger">Reboot companion</button>
      <span class="note">verbinding gaat tijdelijk weg (~10s)</span>
    </div>
  </section>

  <section>
    <h2>Channels</h2>
    <table id="ch-tbl"><thead><tr><th>#</th><th>Naam</th><th>Type</th><th>Alias</th><th></th></tr></thead><tbody></tbody></table>
    <div class="row" style="margin-top:14px">
      <input id="ch-slot" type="number" min="1" max="7" placeholder="slot" style="flex:0;width:80px">
      <input id="ch-name" type="text" placeholder="naam">
      <input id="ch-key" type="text" placeholder="hex-key (optioneel, leeg = random)" style="flex:2">
      <button onclick="addChannel()">Toevoegen</button>
    </div>
    <div class="note">Slots 1-7 zijn voor private channels. Slot 0 = Public.</div>
  </section>

  <section>
    <h2>Housekeeping</h2>
    <div class="row">
      <label>DB-records</label><span id="db-count" class="kv">…</span>
    </div>
    <div class="row" style="margin-top:10px">
      <label>Verwijder ouder dan</label>
      <input id="hk-age" type="number" placeholder="aantal" style="flex:0;width:90px">
      <select id="hk-unit"><option value="86400">dagen</option><option value="3600">uren</option><option value="60">minuten</option></select>
      <button onclick="cleanOlder()" class="danger">Verwijder</button>
    </div>
    <div class="row">
      <button onclick="cleanAll()" class="danger">Alles verwijderen</button>
      <button onclick="vacuum()">VACUUM</button>
    </div>
  </section>

</main>

<div id="toast"></div>

<script>
let CURRENT = {};

function toast(msg, cls){
  const t = document.getElementById('toast');
  t.textContent = msg;
  t.className = 'show' + (cls?' '+cls:'');
  setTimeout(()=>t.className='', 4000);
}

async function api(path, opts){
  opts = opts || {};
  opts.headers = Object.assign({'Content-Type':'application/json'}, opts.headers||{});
  const r = await fetch(path, opts);
  let body = null;
  try { body = await r.json(); } catch(e) {}
  if (!r.ok) {
    toast((body && body.detail) || ('HTTP '+r.status), 'err');
    throw new Error(r.status);
  }
  return body;
}

function fmtKV(obj){
  return Object.entries(obj||{}).map(([k,v])=>'<div><span class="k">'+k+':</span>'+ (v===null||v===undefined?'<i>—</i>':v) +'</div>').join('');
}

async function refresh(){
  try {
    const s = await api('/admin/state');
    CURRENT = s;
    document.getElementById('status').innerHTML = fmtKV({
      'naam': s.node.name, 'pubkey_prefix': s.node.pubkey,
      'batterij': s.node.battery, 'uptime gateway': s.node.uptime
    });
    document.getElementById('radio-current').innerHTML = fmtKV({
      'freq (MHz)': s.radio.freq, 'bw (kHz)': s.radio.bw,
      'sf': s.radio.sf, 'cr': s.radio.cr,
      'tx_power (dBm)': s.radio.tx_power+' / max '+s.radio.max_tx_power
    });
    document.getElementById('r-freq').value = s.radio.freq||'';
    document.getElementById('r-bw').value = s.radio.bw||'';
    document.getElementById('r-sf').value = s.radio.sf||'';
    document.getElementById('r-cr').value = s.radio.cr||'';
    document.getElementById('r-tx').value = s.radio.tx_power||'';
    document.getElementById('n-name').value = s.node.name||'';
    document.getElementById('n-lat').value = s.radio.lat||'';
    document.getElementById('n-lon').value = s.radio.lon||'';

    const tbody = document.querySelector('#ch-tbl tbody');
    tbody.innerHTML = '';
    s.channels.forEach(c => {
      const tr = document.createElement('tr');
      const type = c.is_public?'public':(c.has_key?'private':'?');
      tr.innerHTML = '<td>'+c.idx+'</td><td>'+escape(c.name||'')+'</td><td>'+type+'</td><td>'+escape(c.alias||'')+'</td>'
        + '<td>'+(c.idx===0?'':'<button class="small danger" onclick="removeChannel('+c.idx+')">remove</button>')+'</td>';
      tbody.appendChild(tr);
    });
    document.getElementById('db-count').textContent = s.db.count + ' berichten';
  } catch(e){}
}

function escape(s){ return String(s).replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;","\\"":"&quot;"}[c])); }

async function setRadio(){
  const body = {
    freq: parseFloat(document.getElementById('r-freq').value),
    bw:   parseFloat(document.getElementById('r-bw').value),
    sf:   parseInt(document.getElementById('r-sf').value),
    cr:   parseInt(document.getElementById('r-cr').value),
  };
  if (!confirm('Radio wijzigen naar freq='+body.freq+' bw='+body.bw+' sf='+body.sf+' cr='+body.cr+'?\\nReboot vereist.')) return;
  const r = await api('/admin/radio', {method:'POST', body:JSON.stringify(body)});
  toast(r.message || 'ok', 'ok');
  refresh();
}
async function setTxPower(){
  const dbm = parseInt(document.getElementById('r-tx').value);
  if (!confirm('Tx-power → '+dbm+' dBm?\\nReboot vereist.')) return;
  const r = await api('/admin/txpower', {method:'POST', body:JSON.stringify({dbm})});
  toast(r.message || 'ok', 'ok');
  refresh();
}
async function setName(){
  const name = document.getElementById('n-name').value.trim();
  if (!name) return;
  if (!confirm('Naam wijzigen in '+name+'?')) return;
  const r = await api('/admin/name', {method:'POST', body:JSON.stringify({name})});
  toast(r.message || 'ok', 'ok');
  refresh();
}
async function setCoords(){
  const lat = parseFloat(document.getElementById('n-lat').value);
  const lon = parseFloat(document.getElementById('n-lon').value);
  const r = await api('/admin/coords', {method:'POST', body:JSON.stringify({lat,lon})});
  toast(r.message || 'ok', 'ok');
}
async function clearCoords(){
  if (!confirm('Coords wissen?')) return;
  const r = await api('/admin/coords', {method:'POST', body:JSON.stringify({clear:true})});
  toast(r.message || 'ok', 'ok');
  refresh();
}
async function rebootNode(){
  if (!confirm('Companion rebooten? Verbinding gaat ~10s weg.')) return;
  const r = await api('/admin/reboot', {method:'POST', body:'{}'});
  toast(r.message || 'gestuurd', 'ok');
}
async function addChannel(){
  const slot = parseInt(document.getElementById('ch-slot').value);
  const name = document.getElementById('ch-name').value.trim();
  const key  = document.getElementById('ch-key').value.trim();
  if (!slot || !name) { toast('slot + naam verplicht', 'err'); return; }
  const r = await api('/admin/channels/add', {method:'POST', body:JSON.stringify({slot, name, key: key||null})});
  toast(r.message + (r.generated_key ? '\\nKEY: '+r.generated_key : ''), 'ok');
  refresh();
}
async function removeChannel(slot){
  if (!confirm('Channel slot '+slot+' uit DB-metadata verwijderen?')) return;
  const r = await api('/admin/channels/remove', {method:'POST', body:JSON.stringify({slot})});
  toast(r.message || 'ok', 'ok');
  refresh();
}
async function cleanOlder(){
  const n = parseInt(document.getElementById('hk-age').value);
  const unit = parseInt(document.getElementById('hk-unit').value);
  if (!n) { toast('aantal verplicht','err'); return; }
  const seconds = n*unit;
  // eerst tellen
  const c = await api('/admin/clean/preview?seconds='+seconds);
  if (!confirm(c.count + ' berichten ouder dan dat — verwijderen?')) return;
  const r = await api('/admin/clean', {method:'POST', body:JSON.stringify({mode:'older', seconds})});
  toast(r.message || 'ok', 'ok');
  refresh();
}
async function cleanAll(){
  if (!confirm('ALLE berichten uit de DB verwijderen?')) return;
  const r = await api('/admin/clean', {method:'POST', body:JSON.stringify({mode:'all'})});
  toast(r.message || 'ok', 'ok');
  refresh();
}
async function vacuum(){
  if (!confirm('VACUUM uitvoeren?')) return;
  const r = await api('/admin/vacuum', {method:'POST', body:'{}'});
  toast(r.message || 'ok', 'ok');
}

refresh();
</script>
</body></html>"""


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
        resp.set_cookie(COOKIE_NAME, token, httponly=True, samesite="lax")
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
        resp.set_cookie(COOKIE_NAME, token, httponly=True, samesite="lax")
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
        return HTMLResponse(APP_HTML)

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
            "must_change_password": bool(s.get("must_change", False)),
        }

    # -------- Admin routes ------------------------------------------------

    import time as _time
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
        _auth_or_401(request)
        contacts = getattr(mc, "contacts", None) or {}
        items = []
        for pk_hex, c in contacts.items():
            if not isinstance(pk_hex, str) or not pk_hex:
                continue
            ctype = c.get("type") if isinstance(c, dict) else None
            # Filter: alleen repeaters / room-servers (type 2 of 3)
            if ctype not in (2, 3):
                continue
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
            })
        items.sort(key=lambda x: (x["type"] or 99, (x.get("name") or "").lower()))
        return {"count": len(items), "repeaters": items}

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

    @app.get("/contacts/export")
    async def contacts_export(request: Request, key: Optional[str] = None):
        """Geeft de hex card-data van een contact (of jezelf als key=None)."""
        _auth_or_401(request)
        fn = _resolve_cmd("export_contact")
        if fn is None:
            raise HTTPException(501, "export_contact niet beschikbaar")
        try:
            ev = await fn(key) if key else await fn()
        except Exception as e:  # noqa: BLE001
            raise HTTPException(500, f"export_contact faalde: {e}")
        payload = getattr(ev, "payload", ev)
        if not isinstance(payload, dict):
            return {"ok": True, "card": str(payload)}
        # mc kan dit als 'card' of 'data' returneren — accepteer beide
        card = payload.get("card") or payload.get("data") or payload.get("export")
        return {"ok": True, "card": card, "raw": payload}

    @app.post("/contacts/import")
    async def contacts_import(request: Request, payload: dict):
        _admin_or_403(request)
        card = (payload.get("card") or "").strip()
        if not card:
            raise HTTPException(400, "card (hex) vereist")
        # Probeer hex-string, anders raw bytes
        try:
            card_data = bytes.fromhex(card)
        except ValueError:
            raise HTTPException(400, "card moet hex zijn")
        fn = _resolve_cmd("import_contact")
        if fn is None:
            raise HTTPException(501, "import_contact niet beschikbaar")
        try:
            res = await fn(card_data)
        except Exception as e:  # noqa: BLE001
            raise HTTPException(500, f"import_contact faalde: {e}")
        return {"ok": True, "result": str(res), "message": "contact geïmporteerd"}

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
    async def admin_state(request: Request):
        _auth_or_401(request)
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

        return {
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
                ok = await send_dm_fn(peer, text)
            else:
                idx = int(data.get("channel_idx", 0))
                ok = await send_channel(idx, text)
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
