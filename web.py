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
  .admin-content{padding:14px;overflow-y:auto;max-width:760px}
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
      <div class="tree-group" id="grp-admin" style="display:none">
        <div class="tree-group-h" onclick="toggleGroup('grp-admin')">Admin</div>
        <ul>
          <li onclick="selectAdminView('radio')"        data-sub="radio">Radio</li>
          <li onclick="selectAdminView('node')"         data-sub="node">Node</li>
          <li onclick="selectAdminView('channels')"     data-sub="channels">Channels</li>
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
  view: 'chat',           // 'chat' | 'admin'
  adminSub: 'radio',      // 'radio' | 'node' | 'channels' | 'housekeeping' | 'users'
  channel: {kind:'public', idx:0, name:'Public'},  // {kind, idx, name}
  channels: [],           // from /admin/state
  status: null,           // last /admin/state
  selectedMsg: null,      // currently selected msg in chat view
  me: null,               // {username, role, allowed_views, must_change_password}
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
  $('chat-view').style.display='flex';
  $('admin-view').style.display='none';
  $('view-title').textContent = ch.name;
  $('txt').placeholder = 'Bericht naar ' + ch.name + '…';
  loadChatHistory();
  renderTree();
  renderDetail();
}
function selectAdminView(sub){
  if (!STATE.me || STATE.me.role !== 'admin') return;
  STATE.view = 'admin';
  STATE.adminSub = sub;
  $('chat-view').style.display = 'none';
  $('admin-view').style.display = 'block';
  const titles = {radio:'Radio', node:'Node', channels:'Channels',
                  housekeeping:'Housekeeping', users:'Gebruikers'};
  $('view-title').textContent = 'Admin — ' + (titles[sub] || sub);
  renderAdmin();
  renderTree();
  renderDetail();
}

/* ============== chat ============== */
async function loadChatHistory(){
  $('log').innerHTML='';
  STATE.selectedMsg = null;
  try {
    const path = '/channels/' + STATE.channel.idx + '/history';
    const rows = await api(path);
    rows.forEach(m => addMsg(m));
  } catch(e){}
}

/* Extract afzendernaam uit channel-msg tekst.
   Companion firmware geeft pubkey_prefix vaak NIET mee op channels;
   afzenders prefixen hun naam zelf met "NAAM: tekst". */
function extractSender(m){
  if (m.peer && m.peer !== '?' && m.peer !== 'self') return m.peer;
  const t = m.text || '';
  const colon = t.indexOf(':');
  if (colon > 0 && colon < 32) {
    const candidate = t.substring(0, colon).trim();
    if (candidate && !candidate.includes(' ')) return candidate;
  }
  return null;
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

function addMsg(m){
  const div = document.createElement('div');
  div.className = 'msg ' + (m.direction === 'out' ? 'out' : 'in');
  const displayPeer = extractSender(m) || (m.direction === 'out' ? 'self' : '?');
  const displayText = stripNamePrefix(m);
  if (m.direction === 'in' && isMention(m)) div.classList.add('mention');
  div.innerHTML = '<span class="ts">'+escapeHTML(fmtTs(m.ts))+'</span><span class="peer">'+escapeHTML(displayPeer)+':</span>'+escapeHTML(displayText);
  div.onclick = () => selectMsg(m, div);
  // bewaar de geparseerde sender voor reply
  m._sender = displayPeer;
  m._body = displayText;
  $('log').appendChild(div);
  $('log').scrollTop = $('log').scrollHeight;
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
  if (m.kind !== 'channel') return;
  // Mention-detectie ALTIJD, ongeacht actieve view — zo mis je 'm niet als
  // je net in admin zit of in een ander kanaal.
  if (m.direction === 'in' && isMention(m)) {
    onMention(m);
  }
  // Display-filter alleen voor de huidige view
  if (STATE.view !== 'chat') return;
  if (m.channel_idx === STATE.channel.idx) {
    addMsg(m);
  }
});

$('chat-form').addEventListener('submit', (e) => {
  e.preventDefault();
  const t = $('txt').value.trim();
  if (!t) return;
  $('btn').disabled = true;
  const payload = {channel_idx: STATE.channel.idx, text: t};
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
    case 'channels':     return renderAdminChannels();
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
      // Outgoing: ack-tracking komt in stap 4f
      signalRow = '<div><span class="k">status:</span><i>verzonden</i></div>';
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
      <div class="detail-section">
        <h3 style="cursor:pointer" onclick="toggleRawDetail(this)">raw payload &#x25B8;</h3>
        <div class="msg-quote" style="display:none;font-size:11px">${escapeHTML(typeof m.raw === 'string' ? m.raw : JSON.stringify(m.raw, null, 2))}</div>
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
    const s = await api('/admin/state');
    STATE.status = s;
    STATE.channels = s.channels;
    renderTree();
    renderHeaderStatus();
    if (STATE.view==='admin') renderAdmin();
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


def setup_web(*, mc, send_channel: SendChannelFn, dispatch_obj, gateway_state, stop_event) -> tuple:
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
        return {
            "ts": _iso_utc(m.ts),
            "direction": m.direction,
            "peer": m.peer or ("self" if m.direction == "out" else "?"),
            "text": m.text,
            "kind": m.kind,
            "channel_idx": m.channel_idx,
            "raw": m.raw,
        }

    @app.get("/channels/{idx}/history")
    async def channel_history_endpoint(request: Request, idx: int, limit: int = 30):
        _auth_or_401(request)
        rows = await db.channel_history(idx, limit)
        return [_row_to_dict(m) for m in rows]

    @app.get("/hashtags/history")
    async def hashtag_history_endpoint(request: Request, tag: str, limit: int = 30):
        _auth_or_401(request)
        rows = await db.public_history_with_tag(tag, limit)
        return [_row_to_dict(m) for m in rows]

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

    @app.post("/admin/quit")
    async def admin_quit(request: Request):
        _admin_or_403(request)
        # Trigger het main() stop-event — gateway sluit zichzelf netjes af.
        stop_event.set()
        return {"ok": True, "message": "afsluiten"}

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
        text = (data or {}).get("text", "").strip()
        idx = int((data or {}).get("channel_idx", 0))
        if not text:
            return {"ok": False, "err": "leeg bericht"}
        try:
            ok = await send_channel(idx, text)
            return {"ok": bool(ok)}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "err": str(e)}

    # ---------- Dispatch hook: broadcast inkomende én uitgaande ----------

    async def web_handler(msg) -> None:
        # Alleen channels in deze fase; DM's komen in 4c.
        if msg.kind != "channel":
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
                "ts": _dt.now(_tz.utc).isoformat(),
                "direction": msg.direction,
                "peer": msg.sender or ("self" if msg.direction == "out" else "?"),
                "text": msg.text,
                "kind": msg.kind,
                "channel_idx": msg.channel_idx,
                "raw": raw_out,
            },
        )

    dispatch_obj.register("channel", web_handler)

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
