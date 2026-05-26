# Handoff — MeshCore Gateway Web Client

Stand: versie 1.1.021. Deze notitie is bedoeld om het project in een nieuwe
AI-/dev-omgeving te kunnen voortzetten. De broncode-bestanden gaan apart mee.

---

## 1. Wat het is

Python-gateway die een via USB aangesloten **MeshCore companion-radio**
(Seeed XIAO nRF52840, companion-firmware) ontsluit via een web-UI + minimale
CLI. Slaat al het mesh-verkeer op in SQLite, multi-user met rollen, admin-paneel
voor radio/kanalen/contacten, bot-framework, rapportages.

Stack: Python 3.12+, `meshcore` (officiële SDK), FastAPI + python-socketio,
SQLAlchemy async + aiosqlite, uvicorn. Geen build-step; één venv.

---

## 2. Bestanden

| Bestand | Rol |
|---|---|
| `gateway.py` | Entry-point. Connectie + companion-handshake, `Dispatch`-bus, CLI-loop (alleen bij TTY), watchdog, repeater-cache, RX_LOG-enrichment (twee ring-buffers: `_recent_rxlogs` voor GRP_TXT channel-enrichment + `_recent_rxlogs_all` voor o.a. ping SNR-here-correlatie), implicit-ack, `send_and_dispatch()`, per-kanaal flood-scope, `on_contact_msg` filtert `txt_type ≠ 0` (CLI-responses) uit DM-historie, webserver-/bot-startup. |
| `web.py` | FastAPI + Socket.IO. Auth (pbkdf2, in-memory sessies), alle REST/socket-endpoints. `APP_VERSION` staat hier bovenaan. `LOGIN_HTML` en `SETUP_HTML` zijn nog inline (klein, één pagina). De single-page-app is uit `web.py` getrokken naar `templates/index.html` + `static/app.css` + `static/app.js` — `/` rendert via `Jinja2Templates`, `/static` via `StaticFiles`. |
| `templates/index.html` | Jinja2-template voor de single-page-app. `{{VERSION}}` wordt server-side ingevuld. |
| `static/app.css` | Alle styling van de SPA (was inline `<style>` in `APP_HTML`). |
| `static/js/01-core.js` | STATE, $/escapeHTML/fmtTs/toast/api, mentions-helpers, layout-helpers (toggleCollapse/Menu/Group/quitApp). Globals voor alle volgende files. |
| `static/js/02-tree.js` | renderTree/renderDmTree, view-switching (selectChannel, selectAdminView, selectReport, selectContactsManager + helpers). |
| `static/js/03-chat.js` | Chat-historie, time-nav, pauze, search, msg-bouw/selectie, emoji-picker, hashtag-add/remove, socketio-handlers (`sock.on('msg' ...)`, `'msg-update'`, chat-form submit). |
| `static/js/04-admin.js` | Alle admin-views (Radio/Node/Channels/Housekeeping/Prefs/Contacts/Bots/Users) + simple setters (setRadio/TxPower/Name/Coords/etc). |
| `static/js/05-reports.js` | Rapportages-view + complete repeater-management (rapport, ping, login, status, acties, CLI-tab, favorieten). |
| `static/js/06-detail.js` | Detail-paneel (RSSI/SNR/hops/pad-visualisatie, copy/reply, raw-detail toggle). |
| `static/js/07-bootstrap.js` | refresh/refreshHeaderOnly/refreshAndRerender, auth/account (loadMe, change-password modals), native notifs, init-call: `loadMe().then(refresh).then(...)` + `setInterval(refresh, 30000)` + `setInterval(refreshHeaderOnly, 10000)`. Laad-volgorde-kritisch — dit moet als laatste. |
| `bot.py` | DB-driven bot-framework. Hooks op de dispatch, leest bots uit DB (TTL-cache 30s), variable-resolver `{TIME}/{UPRADIO}/{UPNODE}/{HELP}`. |
| `db.py` | SQLAlchemy async, alle modellen + helpers. `SCHEMA_VERSION` + auto-migraties in `init_db()`. |
| `Dockerfile`, `docker-compose.yml`, `.dockerignore` | Container (python:3.12-slim, non-root, USB-device passthrough, `/data`-volume). |
| `meshcore-gateway.service.example` | systemd-unit template (native installatie). |
| `README.md` | Volledige gebruikershandleiding (setup, container, systemd, caveats). |
| `requirements.txt` / `pyproject.toml` / `.python-version` | Deps; Python `>=3.12`. |

---

## 3. Architectuurkeuzes

- **Eén dispatch-bus** (`Dispatch` in `gateway.py`). Inkomende én uitgaande
  berichten worden een `Message`-object (`direction` in/out, `kind` channel/dm,
  `channel_idx`, `sender`, `text`, `raw`, `expected_ack`, `ack_status`, `db_id`).
  `dispatch.fire(msg)` → handlers: `print_handler`, `db_handler`, `web_handler`,
  `bot_handler`. Daarnaast `dispatch.fire_update(payload)` voor ack-status-updates
  naar de web-UI. Alle send-paden lopen via `send_and_dispatch()` — één plek.
- **CLI is bewust minimaal** en draait alléén als `stdin` een TTY is (headless
  detectie — cruciaal voor systemd/Docker, anders sluit de app direct af).
- **Web-UI is single-page**, opgesplitst in `templates/index.html` +
  `static/app.css` + `static/app.js`. Geen framework, geen bundler — browser
  laadt files direct via FastAPI `StaticFiles`. Versie-substitutie via Jinja2
  (`{{VERSION}}`). Lint nu direct via `node --check static/app.js`.
- **Auth**: pbkdf2_sha256, sessies in-memory (restart = opnieuw inloggen).
  Rollen `admin`/`user`; admin-tak in de tree volledig verborgen voor users.
- **Channels**: slot 0 = Public (vast). Slots 1-7 = `hashtag` (key =
  `sha256("#naam")[:16]`, secret weglaten bij `set_channel`) of `private`
  (16-byte AES, zelf gegenereerd). `Channel.kind` onderscheidt ze.
- **DB-migraties**: forward-only, additief. `init_db()` doet `create_all` +
  handmatige `ALTER TABLE` / `DROP+recreate` per versie-stap.

---

## 4. DB-schema (`SCHEMA_VERSION = "12"`)

Modellen in `db.py`: `Message`, `Meta`, `Channel`, `Hashtag` (deprecated sinds
v4), `User`, `UserContact`, `Bot`, `UserFavoriteRepeater`.

Migratiegeschiedenis: v2 Channel · v3 Hashtag (verlaten) · v4 `Channel.kind` ·
v5 User · v6 `User.must_change_password` · v7 `Channel.scope` · v8 Message
ack-tracking (`expected_ack`/`ack_status`/`acked_at`) · v9 UserContact ·
v10 `UserContact.pubkey` volledige 32-byte hex (tabel gedropt+herbouwd) ·
v11 Bot · v12 UserFavoriteRepeater (per-user favoriete repeaters/rooms;
composite-key `username + pubkey`).

`Message.peer` = 12-char pubkey-prefix. `UserContact.pubkey` en
`UserFavoriteRepeater.pubkey` = volledige 64-char hex. Die inconsistentie
vereist op enkele plekken conversie.

---

## 5. Voltooide functionaliteit

- **Fase 1-2**: connectie + companion-handshake-check, auto port-detect,
  status, CLI-REPL, SQLite-historie, `/history`.
- **Fase 3**: multi-channel (public/hashtag/private), admin-commando's,
  housekeeping (clean/vacuum).
- **Fase 4**: FastAPI + Socket.IO web-UI; drie-koloms-layout (collapsible
  tree / chat / detail); login + multi-user + rollen; first-login met
  tijdelijk-wachtwoord → geforceerde wijziging; admin-paneel opgesplitst in
  Radio / Node / Voorkeuren / Channels / Contacten / Bots / Housekeeping /
  Gebruikers; Rapportages (berichten-per-uur-grafiek met periode-picklist,
  top-kanalen, ack-rate, repeater-overzicht).
- **Berichten-UX**: server-side zoek door alle berichten, paginatie ("laad
  oudere"), tijd-navigatie-knoppen, pauze/play, emoji-picker, selecteerbare
  berichten met detail-paneel (RSSI/SNR/hops/pad).
- **Path-visualisatie**: `set_decrypt_channel_logs(True)` aan; we koppelen
  `RX_LOG_DATA` zélf op `pkt_hash` aan channel-msgs (meshcore-py's eigen
  koppeling werkt niet). Multi-path "Heard X times", repeater-naam-resolving
  via een contact-cache (pubkey-prefix → adv_name).
- **Ack-tracking**: DM = echt protocol-ack (`✓`/`✓✓`); channel = implicit-ack
  via RX_LOG-tijdcorrelatie (`↻`). Inline indicatoren + live WebSocket-update.
- **Mentions**: highlight + beep + toast + (achtergrond-tab) native notificatie.
- **DM**: tree-tak met per-user opgeslagen contactpersonen; `✓/⚠`-status of
  de companion de contact kent.
- **Bots**: admin-defined, reageren alleen op `@[<node-naam>] ?keyword`.
- **Repeater-rapport**: zoekbalk (filtert op naam/pubkey/hash), per-user
  favoriet-ster (DB-opslag, bovenaan gesorteerd). Ping-knop per row die
  `req_status_sync` aanroept en duration + SNR-there (uit status-payload) +
  SNR-here (best-effort via RX_LOG-buffer-correlatie) toont.
- **Housekeeping — stale repeaters**: knop in admin → housekeeping toont
  kandidaten (type 2|3, geen favoriet bij wélke user dan ook,
  `last_advert > 28d`); na bevestiging verwijderen via `remove_contact`.
  Drempel staat als constante `STALE_REPEATER_AGE_SECS` in `web.py`.
- **OTA repeater-management**: vanuit het repeater-rapport een row klikken
  selecteert 'm; in het detail-paneel verschijnt het Manage-paneel met:
  login-form (admin-wachtwoord → `send_login_sync`), request-status-knop
  (`req_status_sync` toont naam/bat/uptime/boot-tijd + optioneel lokale
  klok als ingelogd), een acties-blok (sync tijd / advert / reboot), en
  een vrije CLI-tab. Sessie-tracking per (web-user, repeater) in-memory,
  client-side TTL-hint 120s. Logout via `send_logout`. CLI-responses
  worden weggevangen uit `on_contact_msg` op basis van `txt_type ≠ 0` zodat
  ze niet als DM in de historie belanden.
- **Deployment**: Docker + docker-compose + systemd-template; `--reset-admin`
  is een one-shot zonder USB-claim.

---

## 6. Belangrijke caveats / fragiele plekken

- **`web.py`** was eerst ~3600 regels met alle HTML/CSS/JS inline. Sinds
  v1.1.018 staat de SPA in `templates/index.html` + `static/app.css` +
  `static/js/01..07-*.js` (v1.1.021 splitste de JS verder op) en is `web.py`
  ~1700 regels Python. Lint loopt per JS-file direct via `node --check`.
- **RX_LOG → msg-koppeling** en **implicit-ack** zijn tijd-correlatie-heuristieken
  (binnen 10-15s). Bij druk verkeer kan een verkeerd pad/ack matchen.
- **Naam-parsing** van channel-afzenders is heuristisch: companion geeft vaak
  geen `pubkey_prefix` op channel-events; we pakken "NAAM:" uit de tekst.
- **RSSI** komt in sommige firmware-versies niet door op channel-events (alleen
  SNR). UI toont alleen wat aanwezig is.
- **Bot-cache TTL 30s**: admin-wijzigingen aan bots zijn pas na ≤30s actief.
- **In-memory sessies**, geen CSRF, geen rate-limiting op `/login`. Acceptabel
  voor home-LAN, niet voor blootstelling op internet.
- **`User.allowed_views`** kolom bestaat maar wordt niet via UI beheerd en niet
  afgedwongen — per-user menu-permissies zijn dus half-af (alleen role-based
  admin-hide werkt).
- **`txt_type ≠ 0` wordt niet meer als DM opgeslagen** (filter in
  `on_contact_msg`). Reden: CLI-responses van repeaters mogen niet in de
  DM-historie verschijnen. Als jouw firmware ooit gesigneerde DM's met
  `txt_type=2` of iets dergelijks stuurt, raken die nu zoek; filter dan
  specifieker maken (bijv. `in (1, 3)`).
- **Repeater-ping SNR-here is best-effort**: pakt het laatste rxlog-entry
  uit `_recent_rxlogs_all` dat tijdens de ping-window arriveerde. Bij druk
  RF-verkeer kan dat de verkeerde meting zijn.
- **Repeater OTA-sessie**: lokale TTL is 120s; firmware-side kan korter zijn.
  Een `not_logged_in`-respons gooit de UI terug op het login-form.
- **CLI-response-waiter** in `/admin/repeaters/cmd` neemt het eerste
  `CONTACT_MSG_RECV` van de target-pubkey-prefix binnen 12s. Bij gelijktijdig
  ander verkeer van diezelfde repeater kan dat het verkeerde antwoord zijn.
- **Repeater-CLI-syntax**: geverifieerd via meshcore-cli REPEATER_COMMANDS.md:
  `time <epoch_seconds>` zet de klok (NIET `clock sync <epoch>` — dat is een
  meshcore-cli alias). `clock` (no args) leest 'm. `advert` doet een
  flood-advert. `reboot` herstart. Andere parameters (radio/owner/position/
  region/password) hebben nog geen wrapper-knop; gebruik daarvoor de CLI-tab.
- Geen automated tests.

---

## 7. Dev-workflow

Geen build-step. Na elke wijziging controleren:

```bash
# Python-syntax van alle modules
python3 -c "import ast; [ast.parse(open(f).read()) for f in ('gateway.py','db.py','web.py','bot.py')]"

# JS-syntax check — sinds v1.1.021 staat de SPA-JS in 7 files in static/js/:
for f in static/js/*.js; do node --check "$f"; done
```

Sinds v1.1.018 leeft de SPA in `templates/index.html` + `static/app.css` +
(sinds v1.1.021) `static/js/01..07-*.js`. De 7 modules zijn **plain scripts**
(géén ES modules) en worden in volgorde geladen via afzonderlijke
`<script src>`-tags in `templates/index.html`. Reden: ~80 inline
`onclick="..."`-handlers in de templates + dynamic HTML vereisen dat
handler-functies globals zijn — ES modules zouden voor elke handler een
`window.fn = fn`-shim vereisen. Bootstrap.js (07) moet als laatste: daar
zitten de `loadMe().then(refresh)`-init + de `setInterval`-loops.

JS mag apostroffen en kale `\n` bevatten, en alle `{...}` zijn gewoon JS —
Jinja2 gebruikt `{{ ... }}` voor zijn placeholders en grijpt nooit losse
`{...}`. Versienummer: bump de **z** in `APP_VERSION` (`web.py`) bij elke
gevraagde wijziging; Jinja2 vult 'm in via `{{VERSION}}` in
`templates/index.html`.

`LOGIN_HTML` / `SETUP_HTML` zijn nog inline Python-strings (klein, `{err}` via
`.replace()`).

---

## 8. Eerstvolgende stappen (niet gedaan, geprioriteerd)

**Medium — onderhoud/robuustheid:**
1. ~~`web.py` opsplitsen~~ — gedaan in v1.1.018 (templates/ + static/, Jinja2)
   en v1.1.021 (JS opgesplitst in `static/js/01..07-*.js`, plain scripts).
2. ~~Server-side caching van `/admin/state`~~ — gedaan in v1.1.020.
   TTL `_ADMIN_STATE_CACHE_TTL_SECS = 5.0`, cache wordt automatisch
   geïnvalideerd door een HTTP-middleware na elke succesvolle 2xx-respons op
   `POST/PUT/PATCH/DELETE /admin/*` (behalve `/admin/state` zelf). UI mag
   `?fresh=1` meegeven om de cache te bypassen. Tip bij toekomstige
   wijzigingen die buiten de admin-routes om de node-state veranderen:
   `_invalidate_admin_state_cache()` aanroepen.
3. Smoke-tests voor `db.py`-helpers, password-hashing, schema-migraties.

**Functioneel — eerder besproken, uitgesteld:**
4. Per-user menu-permissies afmaken: UI in Admin → Gebruikers om
   `allowed_views` te zetten + frontend de tree daarop laten filteren.
5. QR import/export van contacten (`export_contact`/`import_contact` bestaan
   al als endpoints in `web.py`, UI is bewust nog niet gebouwd). Camera-scan
   vereist een externe JS-lib (jsQR).
6. Mobiel-responsive maken (drie-koloms-layout → één kolom + hamburger;
   tabellen → kaart-stijl). Ingeschat ~5-7 uur.
7. ~~Watchdog auto-reconnect bij USB-disconnect~~ — gedaan in v1.1.020 als
   "supervisor-restart". Na `WATCHDOG_HARD_FAIL_THRESHOLD` opeenvolgende
   mislukte heartbeats (default 5 × 60s ≈ 5 min) zet de watchdog
   `state.watchdog_restart_requested=True` + `stop.set()`. `main()` exit
   daarna met code 75 (EX_TEMPFAIL) → systemd `Restart=always` /
   docker-compose `restart: unless-stopped` brengt 'm opnieuw op. Geen
   in-place `mc`-reconnect (vereist swap van alle refs in dispatch / bots /
   webapp — te complex). Drempel overschrijfbaar via env-var
   `MESHCORE_WATCHDOG_HARD_FAIL`. systemd-template: `Restart=always`.

**Repeater-management — Fase B (convenience-forms):**
8. Wrapper-knoppen/forms voor de overige veelgebruikte CLI-commando's:
   - Radio-settings (`set radio <freq> <bw> <sf> <cr>`) + tx-power.
   - Advert-intervallen (auto-flood + zero-hop).
   - Owner-info (`get name` / `set name X`).
   - Position (lat/lon).
   - Admin-wachtwoord wijzigen.
   - Region-management.
   Exacte syntax-bron: `meshcore-cli/REPEATER_COMMANDS.md` op GitHub.
9. Telemetry-paneel via `req_telemetry_sync` (LPP-decoded).
10. Sessie-keepalive of zichtbare countdown van repeater-login.

**Laag — alleen bij groei / minder vertrouwd netwerk:**
11. CSRF-tokens op admin-POSTs, rate-limiting op `/login`, persistente sessies.

---

&copy; Flight 815 B.V.
