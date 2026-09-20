# Handoff — MeshCore Gateway Web Client

Stand: versie 1.1.057. Deze notitie is bedoeld om het project in een nieuwe
AI-/dev-omgeving te kunnen voortzetten. De broncode staat in
`github.com/1nvolver/meshcore-webclient` (branch `main`) — clone die repo,
dan heb je alles. Voor de draaiende omgeving zie sectie 9.

> **Voor per-versie wijzigingen / changelog** zie `CHANGELOG.md`. Dit bestand
> is een referentie-doc (architectuur, schema, voltooide features, caveats,
> dev-workflow, backlog) — niet chronologisch.

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
| `static/js/08-qr.js` | QR import/export (v1.1.035): generieke modal-helper (`_qrOpenModal`/`_qrCloseModal`), `showMyQR` (export eigen card via `/contacts/export` → QR-canvas + copy-hex), `showImportQR` (camera-scan via `getUserMedia` + jsQR; foto-upload als fallback; POST naar `/contacts/import`). Geladen vóór bootstrap.js zodat bootstrap nog steeds laatste blijft. |
| `static/vendor/qrcode-generator.min.js` | Kazuhiko Arase, MIT, ~21KB. QR-encoder voor de export-modal. |
| `static/vendor/jsQR.min.js` | cozmo, Apache-2.0, ~257KB. QR-decoder voor camera-scan en foto-upload. |
| `bot.py` | DB-driven bot-framework. Hooks op de dispatch, leest bots uit DB (TTL-cache 30s), variable-resolver `{TIME}/{UPRADIO}/{UPNODE}/{HELP}`. |
| `db.py` | SQLAlchemy async, alle modellen + helpers. `SCHEMA_VERSION` + auto-migraties in `init_db()`. |
| `Dockerfile` | Container-image (python:3.12-slim, tini, non-root `app` uid/gid 1000 + groep `dialout`, `/data`-volume, `HEALTHCHECK` op `/healthz`, OCI-labels). |
| `docker-compose.yml` | **Lokaal** bouwen/testen (`build: .`). |
| `portainer-stack.yml` | **Productie** — pullt `ghcr.io/1nvolver/meshcore-webclient:${MESHCORE_TAG:-latest}`, named volume `meshcore-data`, `group_add: ["20"]` voor serial-toegang. |
| `.github/workflows/ci.yml` | CI: job `checks` (py-ast, `node --check`, jinja render-smoke, `compose config`, APP_VERSION↔HANDOFF-sync) + job `build` (buildx → GHCR, `linux/amd64`, alleen push vanaf `main`/`v*`-tag). |
| `.dockerignore` | Houdt DB, `.git`, docs en venv uit de build-context. |
| `meshcore-gateway.service.example` | systemd-unit template (native installatie). |
| `docs/` | Referentiedocs **overgenomen van upstream** (github.com/meshcore-dev/MeshCore): `cli_commands.md`, `companion_protocol.md`, `payloads.md`, `qr_codes.md`. Bron van waarheid voor repeater-CLI-syntax en protocol-details — raadplegen vóór je een commando-wrapper bouwt. Niet zelf bijhouden; ververs ze uit upstream. |
| `README.md` | Volledige gebruikershandleiding (setup, container, systemd, caveats, update-procedure). |
| `CHANGELOG.md` | Per-versie wijzigingen (chronologisch, append-only). Voorheen `HANDOFF new.md`. |
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
- **Auth**: pbkdf2_sha256, sessies in-memory dict `_SESSIONS` (gateway-restart
  = opnieuw inloggen). Cookie is persistent (sliding 7d, `SESSION_MAX_AGE`)
  zodat tab-close de sessie NIET wist — sinds v1.1.047. `_session()` checkt
  inactiviteit; middleware `_sliding_session_cookie` refresht cookie +
  `last_seen` (throttled per 60s). Rollen `admin`/`user`; admin-tak in de
  tree volledig verborgen voor users.
- **Channels**: slot 0 = Public (vast). Slots 1-7 = `hashtag` (key =
  `sha256("#naam")[:16]`, secret weglaten bij `set_channel`) of `private`
  (16-byte AES, zelf gegenereerd). `Channel.kind` onderscheidt ze.
- **Flood-scope, twee niveaus** (v1.1.045): per-kanaal scope (`Channel.scope`,
  DB) en companion-wide default scope (op de companion zelf, via SDK
  `set_default_flood_scope` / `get_default_flood_scope`). `_apply_channel_scope`
  in `gateway.py` valt bij ontbrekende channel-scope terug op
  `state.default_scope` (gecachet bij connect + bij POST /admin/radio/default-scope)
  zodat 't gedrag deterministisch is: channel-scope overrulet default,
  geen channel-scope = default geldt.
- **DB-migraties**: forward-only, additief. `init_db()` doet `create_all` +
  handmatige `ALTER TABLE` / `DROP+recreate` per versie-stap.
- **Static-asset cache-buster**: `?v={{VERSION}}` op `<link>` + `<script>` URLs
  in `templates/index.html`. Elke `APP_VERSION` bump invalideert browser-cache —
  geen hard-refresh meer nodig na een update.
- **CI/CD is pull-based** (v1.1.049): GitHub Actions bouwt en pusht naar GHCR,
  Portainer pullt. Bewust géén build-on-host: de Portainer-host hoeft geen
  build-context, geen git en geen buildkit te hebben, en het image dat draait
  is bit-voor-bit hetzelfde als wat CI heeft getest. Auth met de automatische
  `GITHUB_TOKEN` — geen secrets in de repo. Alleen `linux/amd64` gebouwd
  (target-host is x86); arm64 erbij is één regel in `platforms:` maar kost
  QEMU-buildtijd.
- **Threading is hybride**: expliciet via `Message.parent_id` (DB-persisted, set
  door Reply-knop) + heuristisch via `@[X]`-mention-detectie client-side
  (binnen 30 min, niet persisted). Render bouwt boom uit beide signalen.
- **Callsign-prefix** voor uitgaande berichten: zit in de message-text als
  `[CS] ...` (niet in sender-name). Bewust — node-name aanpassen per send zou
  USB-traffic + advert-confusie veroorzaken.

---

## 4. DB-schema (`SCHEMA_VERSION = "15"`)

Modellen in `db.py`: `Message`, `Meta`, `Channel`, `Hashtag` (deprecated sinds
v4), `User`, `UserContact`, `Bot`, `UserFavoriteRepeater`, `RepeaterCredential`.

Migratiegeschiedenis: v2 Channel · v3 Hashtag (verlaten) · v4 `Channel.kind` ·
v5 User · v6 `User.must_change_password` · v7 `Channel.scope` · v8 Message
ack-tracking (`expected_ack`/`ack_status`/`acked_at`) · v9 UserContact ·
v10 `UserContact.pubkey` volledige 32-byte hex (tabel gedropt+herbouwd) ·
v11 Bot · v12 UserFavoriteRepeater (per-user favoriete repeaters/rooms;
composite-key `username + pubkey`) · **v13 `User.callsign`** (VARCHAR(64),
default `''`, vrij Unicode incl. emoji) · **v14 `Message.parent_id`**
(Integer, nullable, indexed — threading; gezet bij outgoing als user Reply
heeft geklikt) · **v15 `RepeaterCredential`** (nieuwe tabel
`repeater_credentials`: `pubkey` PK + `password` + `updated_by` + `updated_at`;
puur additief, `create_all` maakt 'm aan, geen ALTER).

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
  favoriet-ster (DB-opslag, bovenaan gesorteerd) en sinds v1.1.056 een
  prullenbak-icoon per rij dat het contact van de companion verwijdert
  (`POST /admin/contacts/remove`; favoriet-markering blijft staan). Ping-knop per row die
  `req_status_sync` aanroept en duration + SNR-there (uit status-payload) +
  SNR-here (best-effort via RX_LOG-buffer-correlatie) toont.
- **Housekeeping — stale companion-contacten** (gegeneraliseerd in v1.1.039):
  admin → housekeeping heeft een date-picker ("Sinds datum…") + type-checkboxes
  (clients/repeaters/rooms; clients standaard uit) + "Favorieten overslaan"-flag.
  Backend: `GET /admin/contacts/stale?days=N&types=2,3&skip_favorites=1` voor
  preview, `POST /admin/contacts/cleanup` voor verwijderen. Legacy
  `/admin/repeaters/stale` + `/admin/repeaters/cleanup` blijven werken met
  hun oude default (28d, {2,3}, skip_favs=true) — geen breaking change.
  Helper `_stale_contact_candidates(age_secs, type_set, skip_favorites)` in
  `web.py` is de enige plek met de selectie-logica; hij geeft sinds
  v1.1.052 `(items, stats)` terug, waarbij `stats` per reden telt hoeveel
  contacten afvielen. De UI toont dat onder de uitslag.
- **OTA repeater-management**: vanuit het repeater-rapport een row klikken
  selecteert 'm; in het detail-paneel verschijnt het Manage-paneel met:
  login-form (admin-wachtwoord → `send_login_sync`), request-status-knop
  (`req_status_sync` toont naam/bat/uptime/boot-tijd + optioneel lokale
  klok als ingelogd), een acties-blok (sync tijd / advert / reboot), en
  een vrije CLI-tab. Sessie-tracking per (web-user, repeater) in-memory,
  client-side TTL-hint 120s. Logout via `send_logout`. CLI-responses
  worden weggevangen uit `on_contact_msg` op basis van `txt_type ≠ 0` zodat
  ze niet als DM in de historie belanden.
- **Deployment** (v1.1.049 omgezet naar containers): GitHub Actions bouwt en
  pusht naar GHCR, Portainer pullt. Zie sectie 9 voor de draaiende omgeving.
  `docker-compose.yml` is nog puur lokaal bouwen/testen; het
  systemd-template blijft bestaan voor een native installatie.
  `--reset-admin` is een one-shot zonder USB-claim (ook in de container:
  `docker compose run --rm gateway python gateway.py --reset-admin`).
- **Callsign per user** (v1.1.031): self-serve via avatar-menu, 1-3+ tekens of
  emoji. Wordt als `[CS] ` voor uitgaande berichten geprependt. Optioneel —
  leeg = uit. Sessie-sync zodat verandering meteen werkt zonder her-login.
- **Threading** (v1.1.033-034): expliciete Reply-knop persisteert `parent_id`;
  client-side mention-heuristiek koppelt msgs met `@[X]`-prefix aan recente
  msgs van X (30 min window). Badge `💬N` tussen tijd en afzender op msgs met
  replies; klik filtert de chat naar root + descendants. Globale toggle in
  avatar-menu (localStorage). Reply-banner boven chat-input toont wat je
  reply't, met ×-annuleer.
- **Repeater-wachtwoord onthouden** (v1.1.050): checkbox bij het login-form
  slaat het admin-wachtwoord op in `repeater_credentials`. Eén rij per
  repeater, gedeeld door alle admins. Bij een volgende login verschijnt
  "verbind (opgeslagen wachtwoord)" + een "vergeet"-knop. Het wachtwoord
  verlaat de server nooit — de API geeft alleen `has_saved_password`.
  **Platte tekst in de DB**, zie caveats.
- **Sorteerbaar repeater-overzicht** (v1.1.050): klikbare kolomkoppen
  (naam/type/hash/pubkey/advert/locatie/path), cyclus asc → desc → uit.
  Favorieten staan altijd bovenaan, ongeacht de sortering; lege waarden
  zakken altijd naar onderen. Alleen de `<th>`-rij wordt hertekend zodat de
  zoek-input focus houdt.
- **Status direct na verbinden** (v1.1.050): een geslaagde repeater-login
  triggert meteen `repeaterRequestStatus()`.
- **Auto-retry op repeater-commando's** (v1.1.050): `_repeaterCmdWithRetry()`
  doet 3 pogingen met 1500ms pauze; de CLI-history toont `retry 1/2…`,
  `retry 2/2…` en uiteindelijk de respons of `failed na 3 pogingen`.
  Gedeeld door de actie-knoppen en de vrije CLI-tab.
- **Repeaters-paneel naar Admin-tak** (v1.1.030): zat eerder onder Rapportages,
  is nu admin-only (zowel UI-hide als backend route-check). Niet-admins zien
  alleen Rapportages → Overzicht.
- **Mobile-responsive UI** (v1.1.022-029 fase A, v1.1.042 fase B): 3
  breakpoints (≤767px / 768-1199 / ≥1200) + landscape-tablet sub-rule (1024-1199
  landscape). Mobile: hamburger-drawer voor tree, bottom-sheet voor detail,
  ≥40px tap targets, 16px input-font (geen iOS-zoom). Body gebruikt `100dvh`.
  Header truncate met ellipsis; portrait ≤480px verbergt battery+uptime.
  **Fase B (v1.1.042):**
  Tabellen worden kaart-stijl op ≤767px (`table:not(.no-card) tr` = card,
  `td[data-label]::before` = label-prefix). Alle dynamische tabellen in
  02-tree.js / 04-admin.js / 05-reports.js hebben nu `data-label="Kolom"`
  per `<td>`. Chat-controls: filter krijgt eigen rij, tijd-knoppen flex:1 1 0
  voor evenredige verdeling. Admin-forms: row-inputs forceeerd naar 100%
  breedte op mobile (overrult inline-styled widths). Landscape tablet
  (1024-1199, orientation:landscape): detail-pane inline ipv overlay zodat
  je tree+main+detail tegelijk ziet (zoals desktop, maar compacter).
- **Static-asset cache-buster** (v1.1.032): `?v={{VERSION}}` op CSS/JS URLs.
  Versie-bump invalideert browser-cache automatisch.
- **QR import/export van contacten** (v1.1.035, formaat opgewaardeerd in
  v1.1.037 naar officieel `meshcore://contact/add?...`): self-serve export
  van eigen card via avatar-menu → "Mijn QR (deel contact)…". Modal toont QR
  + copy-URL-knop. Import via **DM → Contactpersonen** (sinds v1.1.037 niet
  meer onder Admin → Contacten): "Importeer contact via QR…" — camera-scan
  (`getUserMedia` + jsQR; vereist HTTPS of localhost) of foto-upload. Bij
  succesvolle officieel-formaat-import wordt de contact automatisch óók in
  `/my/contacts` opgeslagen (auto-add met name+pubkey uit de URL). Vendor-libs
  lokaal in `static/vendor/`. De oude per-pubkey handmatig-toevoeg-form is in
  v1.1.037 verwijderd — alles via QR.
- **Privé-kanalen eigen tree-tak** (v1.1.037): aparte `grp-private` tussen
  `grp-chat` (Public + hashtag) en `grp-dm`. Channel-rendering splitst op
  `kind`: hashtag in `#tree-channels`, private in `#tree-private`. Admin-only
  "Beheren…"-item bovenaan + `+ privé-kanaal`-link; non-admin ziet alleen de
  channels (om in te chatten). De Channels-tab onder Admin is vervangen door
  een aparte view onder Privé (selectPrivChansManager). Backend: bestaande
  `/admin/channels/add` en `/admin/channels/remove` + nieuwe
  `/admin/channels/{idx}/export` en `/admin/channels/import` (zie boven).

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
- **Companion-commando's geven een Event terug, geen exception bij fouten.**
  `remove_contact` en verwanten leveren `command_ok` of `command_error` (of
  `None` bij time-out). Een weigering is dus géén exception — `await fn(...)`
  zonder het resultaat te checken telt een mislukking als succes. Dat was
  precies de bug in v1.1.050 en eerder (zie CHANGELOG v1.1.051). Gebruik
  `_event_is_ok(ev)` in `web.py` voor elk nieuw commando dat je toevoegt.
- **`mc.contacts` GROEIT alleen — de SDK verwijdert er nooit iets uit.**
  `MeshCore._update_contacts` merget binnenkomende contacten in de dict en laat
  sleutels die de companion niet meer meldt gewoon staan. Een `get_contacts()`
  ruimt dus níéts op. Gebruik altijd `gateway.refresh_contacts(mc, prune=True)`
  (pruned op basis van de CONTACTS-payload) en
  `gateway.forget_contact_locally()` na een bevestigde verwijdering. Dit was de
  hoofdoorzaak achter "verwijderde repeaters komen terug"; zie CHANGELOG
  v1.1.057.
- **`mc.contacts` is een cache, geen live view.** Hij wordt ververst door
  `repeater_cache_loop` in `gateway.py` (elke 5 min) en sinds v1.1.051 ook
  direct na een opruimronde via `_refresh_contacts_cache()`. Muteer je de
  contactenlijst op de companion, ververs 'm dan expliciet — anders leest de
  UI minutenlang achterhaalde data en lijkt de actie niet gewerkt te hebben.
- **Kloksync schrijft naar de radio.** `time_sync_loop` in `gateway.py` stelt
  de companion-klok bij zodra de afwijking > `MESHCORE_TIME_SYNC_THRESHOLD`
  (default 30s), bij start en elke 6 uur. Uitzetten kan met
  `MESHCORE_TIME_SYNC=0`. `set_companion_clock()` weigert te schrijven als de
  host-tijd vóór `HOST_CLOCK_SANITY_EPOCH` (2026-01-01) ligt — anders zou een
  gateway zonder RTC/NTP de radio bij elke boot verkeerd zetten. Draai je op
  hardware zonder betrouwbare tijd, zet de sync dan uit.
- **`last_advert` komt van de klok van de COMPANION, niet van de gateway.**
  Alle leeftijdsberekeningen (housekeeping, het repeater-overzicht) zetten dat
  af tegen `time.time()` van de host. Loopt de companion voor of achter, dan
  schuiven álle leeftijden mee en kan een advert zelfs in de toekomst liggen
  (negatieve leeftijd → telt als "te recent" → niets is ooit stale). Dat is
  precies wat er na de firmware-upgrade naar 1.17.1 gebeurde; zie CHANGELOG
  v1.1.053. Sinds die versie meet `_companion_clock_skew()` het verschil en
  waarschuwt de UI. Een klok-sync repareert **bestaande** tijdstempels niet —
  die blijven scheef tot elke node opnieuw geadverteerd heeft.
- **Bot-cache TTL 30s**: admin-wijzigingen aan bots zijn pas na ≤30s actief.
- **In-memory sessies**, geen CSRF, geen rate-limiting op `/login`. Acceptabel
  voor home-LAN, niet voor blootstelling op internet. Cookie is sinds v1.1.047
  persistent (7d sliding) — handig op vertrouwde apparaten, ongewenst op
  gedeelde. Logout-knop wist 'm netjes; bij gateway-restart logt iedereen uit.
- **`refresh()` skipt `renderDetail()` als het repeater-manage-paneel open is**
  (sinds v1.1.044). Reden: dat paneel heeft uncontrolled password-input,
  CLI-input en gescrollde history die elke 30s gereset werden. Het paneel is
  event-driven (login/status/cli) — geen periodieke server-data om te tonen.
  Als je later content toevoegt die wél periodiek vers moet zijn: voeg een
  smal `refreshRepeaterPanelData()` toe dat alleen de status-velden bijwerkt
  zonder full re-render.
- **`User.allowed_views`** kolom bestaat maar is **inactief**. Sinds v1.1.030
  is alle admin-only functionaliteit (incl. Repeaters) verplaatst naar de
  Admin-tak met pure role-based check; per-user fine-grained menu-permissies
  zijn niet meer op de roadmap. Kolom blijft staan voor eventueel later
  gebruik; verwijderen kost een migratie.
- **Callsign-prefix** zit in de message-text als `[XX] tekst` — ontvangers
  zien "NodeNaam: [XX] tekst", niet "NodeNaam (XX): tekst". Aanpassen van de
  node-name per send zou USB-traffic en advert-confusie veroorzaken (bewust
  afgewezen). Validatie: max 16 codepoints, geen control-chars.
- **Threading-heuristiek** is `_inferredParent` only — niet persisted naar DB.
  Bij refresh wordt-ie opnieuw afgeleid uit `@[X]`-prefixes binnen 30 min. Bij
  msg-history ouder dan dat window zijn replies dus niet meer aan elkaar
  gekoppeld (tenzij Reply-knop is gebruikt, want die zet `parent_id` in DB).
  Verder: `extractMentionTarget` matcht alleen `@[NAAM]` aan het begin van de
  tekst — `@[NAAM]` halverwege wordt niet als reply gezien.
- **Reply naar eigen msg werkt niet**: de Reply-knop disabled zichzelf op
  outgoing msgs. Wel kan je een out-msg manueel mention'en — heuristiek kan
  dan ook eigen out-msgs als parent matchen.
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
- **QR camera-scan vereist HTTPS of localhost**: `navigator.mediaDevices.getUserMedia`
  is door browsers geblokkeerd op plain-HTTP-non-localhost (secure-context-policy).
  Op een LAN-Pi via `http://192.168.x.x` werkt scannen dus niet — gebruik dan
  foto-upload of zet TLS op (reverse-proxy). Export-QR werkt overal.
- **QR-payload-formaten (sinds v1.1.037):**
  - **Contact**: officieel MeshCore-formaat `meshcore://contact/add?name=<urlencoded>&public_key=<64hex>&type=<int>` (zie https://docs.meshcore.io/qr_codes/). Volledig interop met de MeshCore Android-app. Backend `/contacts/export` bouwt deze URL uit `get_self_info` (eigen card) of `mc.contacts[pk]` (andermans card). Import via `/contacts/import` accepteert deze URL én — backward-compat — de oude `meshcore://<rawhex>` van v1.1.036 (via `import_contact(bytes)`).
  - **Channel**: officieel `meshcore://channel/add?name=<urlencoded>&secret=<32hex>`. Backend endpoints: `GET /admin/channels/{idx}/export` (admin-only; gebruikt `get_channel(idx)` om de 16-byte secret op te halen) en `POST /admin/channels/import` (admin-only; parse URL, pak volgend vrij slot 1-7, `set_channel(slot, name, secret)`, DB-spiegel als `private`).
- **jsQR bundle is groot** (~257KB). Geen aparte minified versie van upstream;
  zit standaard in `dist/jsQR.js` als webpack-bundled output. Op snelle LAN's
  prima, op slow mobile data eerste page-load ~+0.3s. Lazy-load is denkbaar
  maar zou de `<script src>`-volgorde-aanname doorbreken — laat zo.
- **Container + USB**: de app draait als uid 1000 met supplementaire groep 20
  (`dialout`). Op een host waar `/dev/ttyACM0` van een andere groep is, faalt
  het openen van de poort — fix via `group_add` in de compose, niet via
  `privileged: true`. `/dev/ttyACM0` is bovendien niet stabiel bij replug of
  meerdere USB-serieel-apparaten; `/dev/serial/by-id/...` links in `devices:`
  is robuuster, maar Docker resolvet die symlink alleen bij containerstart —
  na een replug moet de container herstarten.
- **`useradd -g 1000` was een latente build-breker** in de oude Dockerfile:
  groep 1000 bestaat niet in `python:3.12-slim`, dus de build faalde zodra
  iemand 'm daadwerkelijk zou bouwen. Sinds v1.1.049 wordt de groep expliciet
  aangemaakt. Was nooit opgevallen omdat er lokaal geen build gedraaid is.
- **De CI kan de app niet smoke-testen**: geen USB-device op een runner. De
  checks zijn syntax + render + compose-validatie, niet meer dan dat. Een
  groene build betekent "het image bouwt en de bestanden parsen", niet
  "de gateway praat met de radio".
- **`latest` op GHCR is een bewegend doel**: pin in Portainer een versie-tag
  (`MESHCORE_TAG=1.1.049`) als je wilt bepalen wanneer je update.
- **Rollback is beperkt door forward-only migraties**: terug naar een oudere
  image-tag werkt alleen als het DB-schema niet vooruit is gemigreerd. Vandaar
  de backup-stap in de update-procedure.
- **Opgeslagen repeater-wachtwoorden staan als PLATTE TEKST in de DB**
  (tabel `repeater_credentials`, sinds v1.1.050). Dat kan niet anders met een
  hash — het wachtwoord moet letterlijk naar de repeater — maar het betekent
  wel: wie `meshcore.db` of een backup ervan heeft, heeft de
  repeater-wachtwoorden. De rij geldt voor álle admins, niet per user.
  Wil je 't ooit versleutelen: `db.get_repeater_password` en
  `db.set_repeater_password` zijn de enige twee plekken die de waarde
  aanraken — key uit een env-var, en klaar. Let wel: dan zijn bestaande
  opgeslagen wachtwoorden onleesbaar en moet je ze opnieuw invoeren.
- **Een geweigerd opgeslagen wachtwoord wordt niet automatisch gewist.**
  Bewust: een firmware-hik of een `no_response` zou anders je opslag
  opruimen. De UI meldt het en je overschrijft 'm handmatig.
- **De retry-logica kan een commando dubbel uitvoeren.** Een timeout betekent
  "geen antwoord", niet "niet aangekomen": de repeater kan 'm wél hebben
  uitgevoerd terwijl de respons onderweg sneuvelde. Voor `clock`, `get …` en
  `advert` is dubbel onschadelijk. `reboot` is daarom uitgezonderd van de
  retry (`REP_CMD_NO_RETRY` in `05-reports.js`). Voeg daar commando's aan toe
  als er later niet-idempotente wrappers bijkomen — de fase-B-forms
  (`set radio`, `set name`, position) zijn overschrijvend en dus wél veilig
  om te herhalen, maar denk er per geval over na.
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
`templates/index.html`. Sinds v1.1.032 wordt `{{VERSION}}` óók als
`?v=...`-query op alle CSS/JS-URLs gezet → versie-bump invalideert browser-cache
zonder dat user hard-refresh moet doen.

`LOGIN_HTML` / `SETUP_HTML` zijn nog inline Python-strings (klein, `{err}` via
`.replace()`). Sinds v1.1.031 hebben ze ook viewport meta + 16px input-font
voor mobile-bruikbaarheid.

Sinds v1.1.049 draait dezelfde lint-loop ook in CI (`.github/workflows/ci.yml`,
job `checks`), plus een `docker compose config`-validatie en een check dat het
nieuwe `APP_VERSION` ook echt in `HANDOFF.md` staat. Vergeet je die sync, dan
faalt de build — bewust, want de versie-string is de enige koppeling tussen
image-tag, cache-buster en docs.

**Deploy-procedure (container, sinds v1.1.049):** push naar `main` → CI draait
de checks en pusht het image → Portainer *Pull and redeploy*. DB-backup vooraf
(zie `README.md`, sectie "Draaien op Portainer"), want schema-migraties zijn
forward-only. Migratie loopt automatisch bij startup; hard refresh is niet
nodig dankzij de cache-buster.

**Native installatie** (systemd, zonder container): de oude procedure staat nog
in `README.md` sectie "Updates / nieuwe versie deployen" — DB-backup → code
pullen → service restart.

---

## 8. Eerstvolgende stappen (niet gedaan, geprioriteerd)

> Afgeronde items uit eerdere sessies (web.py-split, JS-modularisatie,
> /admin/state-cache, watchdog supervisor-restart, mobile fase A incl.
> portrait + 100dvh, Repeaters→Admin, callsign, threading) staan niet meer
> in deze lijst — zie `CHANGELOG.md`.

**Repeater-management — Fase B (convenience-forms):**
1. Wrapper-knoppen/forms voor de overige veelgebruikte CLI-commando's:
   - Radio-settings (`set radio <freq> <bw> <sf> <cr>`) + tx-power.
   - Advert-intervallen (auto-flood + zero-hop).
   - Owner-info (`get name` / `set name X`).
   - Position (lat/lon).
   - Admin-wachtwoord wijzigen.
   - Region-management.
   Exacte syntax-bron: `meshcore-cli/REPEATER_COMMANDS.md` op GitHub.
2. Telemetry-paneel via `req_telemetry_sync` (LPP-decoded).
3. ~~Sessie-keepalive of zichtbare countdown van repeater-login.~~ (v1.1.046 — zichtbare countdown + expliciete verleng-knop. Auto-keepalive bewust niet — USB-traffic + onzekere firmware-TTL.)

**Container / CI (alleen bij trigger):**
4. **arm64 erbij** als er ooit een Pi als target komt: in
   `.github/workflows/ci.yml` bij de build-stap `platforms: linux/amd64`
   uitbreiden naar `linux/amd64,linux/arm64`. Kost fors meer buildtijd
   (QEMU-emulatie), daarom nu bewust alleen amd64.
5. **Image-size**: de `python:3.12-slim` + pip-install laag is ~250MB. Een
   multi-stage build met `--user`-installs zou dat kunnen halveren; niet
   gedaan want irrelevant bij een pull per paar weken.

**Onderhoud / robuustheid (alleen bij trigger):**
6. Smoke-tests voor `db.py`-helpers, password-hashing, schema-migraties —
   pas relevant bij grotere schema-wijziging.
7. Threading-performance: huidige `isInThread` is O(N) per check (lineaire
   scan in `STATE.msgs`). Voor typische 30-100 msgs prima; bij 1000+ in
   één view zou een hash-map met parent-lookup nodig zijn.

**Laag — alleen bij groei / minder vertrouwd netwerk:**
8. CSRF-tokens op admin-POSTs, rate-limiting op `/login`, persistente sessies.
   Extra relevant nu de app op een altijd-draaiende server staat in plaats van
   een laptop.

**Mogelijk overbodig (overwegen op te ruimen):**
9. `User.allowed_views`-kolom (zie sectie 6). Sinds Repeaters in Admin staat
    is er geen use-case meer; kolom kost niets maar verwart toekomstige
    lezers. Drop via een migratie kost ~10 minuten.

---

---

## 9. Draaiende omgeving (sinds 2026-08-30)

| | |
|---|---|
| **Werkmap** | `~/Library/CloudStorage/OneDrive-Flight815B.V/Development/Python/Meshcore/WebClient` (OneDrive) |
| **Repo** | `github.com/1nvolver/meshcore-webclient`, publiek, één branch `main` |
| **Registry** | `ghcr.io/1nvolver/meshcore-webclient` — package publiek, Portainer logt niet in |
| **Host** | `homeserver`, x86/amd64, Docker + Portainer |
| **Stack** | `meshcore-gateway`, compose = `portainer-stack.yml` |
| **UI** | `http://homeserver:8180/` — host-poort 8180 → container-poort 8080 (8080 was op die host al bezet) |
| **USB** | `/dev/ttyACM0`, `root:dialout`, dialout-GID 20 → `group_add: ["20"]` |
| **Data** | named volume `meshcore-data` → `/data/meshcore.db` |
| **Firmware** | repeaters én companion op **v1.17.1** (sinds 2026-09-20) |

Aandachtspunten bij deze omgeving:

- **De productie-DB is leeg begonnen.** De `meshcore.db` in de werkmap is
  lokale dev-historie en is bewust niet meegemigreerd. Wil je 'm alsnog
  overzetten: zie `README.md`, "Data, backup en de database".
- **De DB staat niet in git en niet in het image** — `*.db` staat in zowel
  `.gitignore` als `.dockerignore`, en er is nooit een `.db` gecommit
  (gecontroleerd met `git log --all -- '*.db'`). Houd dat zo: er zitten
  berichten, contacten en wachtwoord-hashes in.
- **Firmware-afhankelijkheden zijn niet gepind.** De app praat met wat er op
  de radio staat. Bij een firmware-upgrade zijn dit de plekken die stiekem
  kunnen breken: de repeater-CLI-syntax (`gateway.py` / `05-reports.js`), de
  `txt_type ≠ 0`-filter in `on_contact_msg`, en de RX_LOG-correlatie. Check
  `docs/cli_commands.md` van de bijbehorende upstream-versie na een upgrade.
- **Alleen `linux/amd64` wordt gebouwd.** Een Pi als target vereist de
  `platforms:`-uitbreiding uit sectie 8 item 4, of een lokale build.
- **De werkmap hierboven is de enige geldige.** Een eerdere locatie op een
  andere cloud-drive is vervallen; die komt in geen enkel actueel bestand meer
  voor. Kom je 'm ergens tegen in oude commit-diffs, negeer 'm.

---

&copy; Flight 815 B.V.
