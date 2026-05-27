# Handoff — MeshCore WebClient sessie 2026-05-25 / 26

**Project:** `/Users/dhammel/Library/CloudStorage/OneDrive-Flight815B.V/Development/Python/Meshcore/WebClient`
**Branch:** `refactor/split-web` (eerder gemerged naar `main`; user heeft feitelijk maar 1 branch)
**Eindstand bij dit handoff-moment:** **v1.1.034** (threading fase 2 — badges + Reply-flow + filter-view + toggle)

Authoritative project-doc: zie `HANDOFF.md` in de repo — die is bijgewerkt tot en met v1.1.022 inclusief mobile-fase-A. v1.1.023/024 zijn cosmetische fixes erbovenop.

---

## Wat er deze sessie is gebeurd (chronologisch, per version-bump)

### v1.1.018 — web.py opgesplitst
- `APP_HTML` (Python triple-quoted string van ~2640 regels) uit `web.py` getrokken.
- Nieuw: `templates/index.html` (Jinja2), `static/app.css`, `static/app.js`.
- FastAPI: `Jinja2Templates` + `StaticFiles` toegevoegd. `requirements.txt` / `pyproject.toml`: `jinja2>=3.1`. Dockerfile: `COPY templates/ static/` toegevoegd.
- `LOGIN_HTML` / `SETUP_HTML` (klein) bleven inline Python-strings — bewuste keuze.

### v1.1.019 — bugfix
- `templates.TemplateResponse("index.html", {...})` faalde met `unhashable type: 'dict'`. Oorzaak: nieuwe Starlette-signature wil `(request, name, context)`. Fix: `templates.TemplateResponse(request, "index.html", {"VERSION": APP_VERSION})`.

### v1.1.020 — /admin/state cache + watchdog supervisor-restart
- **Cache:** module-level dict in `web.py`, TTL `_ADMIN_STATE_CACHE_TTL_SECS = 5.0`. HTTP-middleware `_admin_state_cache_invalidator` invalideert automatisch na elke 2xx-respons op `POST/PUT/PATCH/DELETE /admin/*` (behalve `/admin/state` zelf). UI kan `?fresh=1` meegeven om cache te bypassen. Helper: `_invalidate_admin_state_cache()`.
- **Watchdog:** nieuwe drempel `WATCHDOG_HARD_FAIL_THRESHOLD` (default 5, ≈5 min stilte; env-var `MESHCORE_WATCHDOG_HARD_FAIL`). Bij overschrijden zet `state.watchdog_restart_requested=True` + `stop.set()`. `main()` exit met code 75 (EX_TEMPFAIL) → supervisor (systemd `Restart=always` / docker-compose `restart: unless-stopped`) start opnieuw. Bewuste keuze om geen in-place `mc`-reconnect te doen (zou refactor van alle refs in dispatch/bots/webapp vereisen).
- `meshcore-gateway.service.example` aangepast: `Restart=always`.
- User testte op desktop, watchdog-test groen ("dit werkt zoals verwacht").

### v1.1.021 — JS modulariseren
- `static/app.js` (2361 regels) opgesplitst in **7 plain `<script>`-files** in `static/js/`:
  - `01-core.js` (136 r) — STATE, helpers, mentions, api, layout helpers
  - `02-tree.js` (226 r) — tree-rendering + view-switching (`selectChannel/Admin/Report/Contacts`)
  - `03-chat.js` (415 r) — chat, emoji, hashtags, **socketio-handlers en chat-form submit**
  - `04-admin.js` (606 r) — alle admin-views + simple setters + users
  - `05-reports.js` (500 r) — rapportages + complete repeater-management
  - `06-detail.js` (322 r) — detail-paneel + path-visualisatie
  - `07-bootstrap.js` (156 r) — `refresh`/`refreshHeaderOnly`/`refreshAndRerender`, auth/account, native notifs, **init `loadMe().then(refresh)...` + de twee `setInterval`-loops** — moet als laatste laden
- **Bewuste keuze: plain scripts, geen ES modules.** Reden: ~80 inline `onclick="..."`-handlers in templates en dynamic HTML eisen dat handler-functies globals zijn; ES modules zouden voor elke handler een `window.fn = fn`-shim vereisen.
- Lint-loop nu: `for f in static/js/*.js; do node --check "$f"; done`.
- User testte alle paneels, commit gedaan.

### v1.1.022 — mobile-responsive fase A
- 3 breakpoints: mobile ≤767px, tablet 768-1199, desktop ≥1200.
- **Mobile:** hamburger linksboven → tree als slide-in drawer (84vw, max 320px) met backdrop; detail-pane als bottom-sheet; tap-targets ≥40px; inputs 16px font (geen iOS-zoom).
- **Tablet:** tree zichtbaar smaller (200px), detail als slide-in overlay van rechts (340px).
- **Desktop:** ongewijzigd (3-koloms zoals voorheen).
- **JS-helpers** in `01-core.js`: `toggleMobileDrawer/closeMobileDrawer/openMobileDetail/closeMobileDetail/closeMobileOverlays`. Hooks in `02-tree.js` (alle `select*` → drawer-close) en `03-chat.js`/`05-reports.js` (`selectMsg`/`selectRepeater` → detail-open).
- **Template:** hamburger-knop in header, backdrop-div, ×-knop in detail-pane.
- **Tabellen:** voor fase A horizontaal scrollbaar als fallback. Kaart-stijl is fase B (zie `HANDOFF.md` sectie 8 item 6).

### v1.1.023 — bugfixes na user-test mobile
- Detail-pane sloot niet bij click-buiten → backdrop nu ook actief bij `body.detail-open` (op viewports waar detail overlay is). Backdrop-klik sluit drawer én detail. `openMobileDetail()` opent niets als er geen selectie is (anders lege overlay bij kanaal-switch).
- User-feedback: "detail sluiten werkt nu".

### v1.1.024 — viewport + drijvende X
- **`<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">`** toegevoegd. Was vergeten in oorspronkelijke template → Chrome DevTools mobile-emulatie zag de viewport als 980px en triggerde géén media queries. Daarom kreeg user geen hamburger te zien.
- Detail-× als drijvende ronde knop met witte achtergrond + schaduw (tablet 36px, mobile 40px). `padding-top` van `.detail-content` opgevoerd zodat action-buttons niet onder de X vallen.

### v1.1.025 — portrait-overflow fixes
User stuurde screenshots: in portrait mode (iPhone) viel rechterkant van header af én detail-panel had horizontale overflow (Reply/Copy actiebuttons en hop-pillen NL-WRM-ZENDMAST.COM staken eraf).

CSS-fixes (geen Python/JS-wijzigingen):
- **Vangnet:** `html{overflow-x:hidden}` om onverwachte horizontale overflow van fixed-positioned children (drawer, bottom-sheet) te clippen.
- **Header:** `header{overflow:hidden}` als clip. Op ≤767px: `.hdr-status` nu `flex:1 1 auto;min-width:0;justify-content:flex-end;overflow:hidden`, items elk `min-width:0;text-overflow:ellipsis;max-width:38vw`. `.title{max-width:42vw;min-width:0;flex:0 1 auto}` (was `0 0 auto` waardoor 'ie niet kromp). `.hamburger` en `.avatar` `flex:0 0 auto`.
- **Detail-actions:** buttons nu `flex:1 1 0;min-width:0` (default `min-width:auto` voorkomt dat flex-items onder hun intrinsic content krimpen — klassieke flex-overflow valstrik). Plus `flex-wrap:wrap` op container.
- **Hop-chain:** `.hop-chain{overflow-wrap:anywhere}` en `.hop{max-width:100%;word-break:break-all;vertical-align:top}`. Lange hostnames binnen een pil breken nu af ipv te overflowen.

Wacht op user-test op iPhone (geen formaat opgegeven; screenshots leken ±375-390px breed). De `100dvh`-fix voor iOS URL-bar **is niet toegepast** — bij dit issue ging het om horizontale overflow, niet om verticale viewport-ruimte.

### v1.1.026 — portrait header cleanup + login/setup mobile
Na v1.1.025 user-feedback: header past wel in breedte maar alle items getruncated met ellipsis ("MeshCore Gateway W..." / "NL-020-Inv" / "3.7(" / "40m"). Visueel rommelig. Plus: login/setup pages schaalden niet op iPhone.

- **Header (≤480px):** battery + uptime verborgen via `:nth-child(2),(3){display:none}`. Title `max-width:55vw`, node-name `max-width:35vw`. Battery/uptime blijven beschikbaar in admin/reports.
- **LOGIN_HTML + SETUP_HTML** (inline strings in web.py): viewport meta toegevoegd, input/button `font-size:16px` (geen iOS-zoom), `min-height:44px` op submit-button, `padding:12px`. Body krijgt minder margin op smal scherm.

### v1.1.027 — avatar overlap node-name (portrait)
v1.1.026's `title:55vw + node:35vw` was te ruim. Op 375px: 12 padding + ~36 hamburger + 206 title + 131 node + 34 avatar = 419px > 375. Avatar werd visueel op de laatste karakters van de node-naam getekend (flex met `header{overflow:hidden}` clipt maar laat items wel tegen elkaar drukken).

Fix: title 40vw, node-name 32vw (samen 270px), plus `margin-right:6px` op `.hdr-status` voor breathing-room naar avatar. Berekening staat als comment in CSS.

### v1.1.028 — avatar-menu zichtbaar
v1.1.025's `header{overflow:hidden}` (vangnet tegen item-overflow) clipte ook het uitgeklapte avatar-menu (`.menu{position:absolute;top:36px}` valt onder de header-rand). User zag alleen een wit hoekje.

Verwijderd. `.title`, `.hdr-status` en `.hdr-status .item` hebben al elk hun eigen `overflow:hidden`+ellipsis, dus tekst-overflow wordt op item-niveau opgevangen — vangnet op header is niet nodig.

### v1.1.029 — 100dvh ipv 100vh (mobile URL-bar)
User testte op echte OnePlus 13 (Chrome Android): DevTools-emulatie zag er goed uit maar IRL drukte de Chrome URL-bar het chat-form onder het scherm. `body{height:100vh}` rekent niet met dynamic UI chrome.

Fix: `body{height:100vh;height:100dvh}` — `100dvh` is dynamic viewport height die wel rekening houdt met URL-bar; `100vh` blijft als fallback voor <Chrome 108 / <iOS 15.4.

### v1.1.030 — Repeaters van Rapportages naar Admin (admin-only)
User: "alleen alles onder admin is voor admins beschikbaar, maar dan moet het item repeaters wel onder admin komen te staan". Geen per-user `allowed_views`-UI nodig — gewoon role-based split. Repeaters logisch admin-only (favorites, repeater-management, stale-cleanup).

Status `allowed_views`-veld: blijft in DB (`User.allowed_views`), in sessie en in `/me`-payload, maar **wordt niet actief gebruikt voor enforcement**. Frontend tree-render checkt `STATE.me.role==='admin'` voor Admin-groep; rest is open voor authed users. Het veld kan in de toekomst weer geactiveerd worden voor fijnmazige rechten, voor nu is 't dode code.

Wijzigingen:
- **`templates/index.html`**: `<li>Repeaters</li>` verplaatst van `grp-reports` naar `grp-admin` (tussen Contacten en Bots). `onclick` van `selectReport('repeaters')` → `selectAdminView('repeaters')`, `data-report` → `data-sub`.
- **`static/js/02-tree.js`**: `selectAdminView` heeft nu een special-case voor `'repeaters'` die `#reports-view` toont (ipv `#admin-view`) en `renderReportRepeaters()` aanroept. `renderReportRepeaters` zelf is ongewijzigd — target blijft `#reports-view`. Title wordt nu "Admin — Repeaters". `STATE.selectedRepeater` wordt gereset bij switch.
- **`static/js/06-detail.js`**: conditie voor manage-paneel: `STATE.view==='admin' && STATE.adminSub==='repeaters'` (was `reports`/`repeaters`).
- **`static/js/07-bootstrap.js`**: `refreshAndRerender()` admin-branch dispatch: `adminSub==='repeaters'` → `renderReportRepeaters()`, anders `renderAdmin()`.
- **`web.py`**: `_auth_or_401` vervangen door `_admin_or_403` op:
  - `GET /reports/repeaters`
  - `GET /reports/repeaters/favorites`
  - `POST /reports/repeaters/favorites`
  - `DELETE /reports/repeaters/favorites/{pubkey}`

URL-paden van repeater-routes zijn niet hernoemd (zouden `/admin/repeaters/...` moeten heten voor consistentie) — kost extra werk en URL-breakage. Voor nu auth-check verzwaard, URL onveranderd.

Bestaande `/admin/repeaters/*` routes (stale, cleanup, ping, login, logout, session, cmd, manage_status) waren al `_admin_or_403`.

### v1.1.031 — Callsign-prefix voor uitgaande berichten
Self-serve identifier-prefix die voor elke uitgaande tekst gezet wordt zodat ontvangers zien welke web-user 'm verstuurd heeft. Format: `[XXX] tekst`. Vrij Unicode (incl. emoji), max 16 codepoints, leeg = uit.

- **DB (`db.py`)**: nieuwe `User.callsign: Mapped[str]` kolom (VARCHAR(64) default ''). `SCHEMA_VERSION` 12 → 13. Migratie via `ALTER TABLE users ADD COLUMN callsign VARCHAR(64) DEFAULT ''` voor bestaande DB's. Helper `set_user_callsign(username, callsign)`.
- **Sessie (`web.py`)**: `_new_session` schrijft callsign in sessie. Nieuwe helper `_session_from_environ(environ)` zodat de sio-handler de sessie via cookie kan opzoeken.
- **`/me`-endpoint**: bevat nu `callsign`-veld.
- **`/me/callsign` endpoint** (POST, self-serve): valideert (max 16 codepoints, geen control chars), schrijft DB, synct alle actieve sessies van die user zodat de prefix meteen actief is zonder her-login.
- **sio `send`-handler**: pakt sessie uit `sio.get_environ(sid)`, prependt `[CS] ` voor de tekst als callsign gezet is. Geldt voor zowel kanaal- als DM-berichten.
- **UI**: avatar-menu krijgt item "Callsign instellen…" (of "Callsign: XXX (wijzig…)") tussen username en wachtwoord-link. `changeCallsignPrompt()` en `updateCallsignMenuLabel()` in `07-bootstrap.js`.

Geen admin-UI voor andere users' callsigns toegevoegd — user koos self-serve. Het bestaande `allowed_views`-veld blijft inactief.

**Bekende grens:** receiver ziet ruwweg "NodeNaam: [DMH] tekst" — de callsign zit IN de message-text, niet in de sender-name. Aanpassing van node-name per send zou USB-traffic + advert-confusie veroorzaken (afgewezen optie).

### v1.1.032 — cache-buster op static assets
Na deploy van 1.1.031 zag user "callsign-knop doet niets" — bleek browser-cache (oude 07-bootstrap.js). `<link>` en `<script>` tags krijgen nu `?v={{VERSION}}` query, zodat elke `APP_VERSION` bump browser-cache forceert te invalideren.

Tegen-effect: cache-hit-rate gaat omlaag voor terugkerende users (elke versie laden ze JS opnieuw), maar bij een gateway-app waar js totaal ~80KB is, verwaarloosbaar.

### v1.1.033 — Threading fase 1: parent_id (DB + backend)
Eerste fase van threading-feature. Alleen backend + DB-laag — frontend toont nog niks. Doel: parent_id-veld kunnen opslaan bij outgoing msgs zodat fase 2/3 erop kan voortbouwen.

- **DB (`db.py`)**: nieuwe `Message.parent_id: Mapped[Optional[int]]` (Integer, nullable, indexed). `SCHEMA_VERSION` 13 → 14. Migratie: `ALTER TABLE messages ADD COLUMN parent_id INTEGER DEFAULT NULL` + `CREATE INDEX IF NOT EXISTS ix_messages_parent_id`. Bestaande msgs krijgen NULL = top-level.
- **`save_message`**: accepteert nu `parent_id` kwarg.
- **`gateway.py` `Message`**: slot+init krijgt `parent_id`. `db_handler` reikt 'm door naar save_message. `send_and_dispatch` accepteert kwarg en plakt 'm op de outgoing Message. De send-lambdas in `main()` voor web/bot accepteren optionele `parent_id` kwarg (backward compat: bots geven 'm niet mee).
- **`web.py`**:
  - `_row_to_dict` voegt `parent_id` toe aan history-payload.
  - sio `send`-handler leest `data.get("parent_id")` (int of None) en geeft mee aan `send_channel`/`send_dm_fn`.
  - sio `msg`-emit (live broadcast naar clients) bevat ook `parent_id`.

**Niet gewijzigd:** frontend kent `parent_id` nog niet, render of UI ongewijzigd. Outgoing msgs kunnen pas met parent_id verzonden worden zodra fase 2 een Reply-flow heeft (`STATE.replyTo` + payload-uitbreiding in 03-chat.js send).

**Volgende fasen:**
- Fase 2: frontend mention-heuristiek (zonder DB) + badge tussen ts en sender + Reply-flow die parent_id mee-stuurt
- Fase 3: filter-view (alleen thread tonen) + localStorage toggle

### v1.1.034 — Threading fase 2+3 (frontend)
Frontend voor threading. Fase 3 (filter-view + toggle) meteen meegenomen omdat 't klein bleek; geen aparte release nodig.

**Data-flow:**
- `STATE.msgs` houdt alle msgs in huidige chat-view (root-array, gevuld door `loadChatHistory` en `addMsg`).
- `STATE.replyCounts` is `{rootId: aantal-descendants}`, opnieuw berekend door `buildReplyTree(msgs)` na elke change. Heuristisch parent op `m._inferredParent` (niet persisted; alleen voor render).
- `STATE.replyTo = {id, sender}` als user op Reply heeft geklikt — gebruikt door send-handler om `parent_id` mee te sturen.
- `STATE.threadFilter = rootId` als thread-view actief is; null = vlakke chat.
- `STATE.threadingEnabled` geladen uit `localStorage.threading_on` (default `true`).

**Helpers (01-core.js):**
- `extractMentionTarget(text)` — match `@[X]` aan begin.
- `buildReplyTree(msgs)` — chrono-sort, vult `_inferredParent` voor msgs zonder expliciete `parent_id` als ze met `@[X]` beginnen en X binnen 30 min een msg heeft. Bouwt `STATE.replyCounts` door descendants per root te tellen. Cycli-bescherming via max-50-hops loop.
- `isInThread(m, rootId)` — wandelt parent-chain (explicit/inferred) terug, true als rootId in pad zit.
- `setThreadingEnabled(on)` — schrijft `localStorage`.

**Render (03-chat.js):**
- `_buildMsgEl` voegt `<span class="thread-badge">` tussen ts en peer (placeholder, gevuld door `_refreshThreadBadge`). Klik op badge roept `enterThreadView(rootId)` aan; klik op msg-body (niet badge) doet de normale `selectMsg`.
- `refreshAllThreadBadges()` na elke `addMsg` of bij toggle-flip.
- `enterThreadView(rootId)` verbergt alle msgs niet in de thread, plaatst `<div id="thread-banner">` boven het log met snippet + back-link.
- `exitThreadView()` herstelt alles.
- `loadChatHistory` en `loadOlder` vullen `STATE.msgs` en roepen `buildReplyTree` aan vóór render.

**Reply-flow (06-detail.js + 03-chat.js):**
- `replyToSelected` (al bestaande knop in detail-pane): zet nu óók `STATE.replyTo`, toont `#reply-banner` boven `#chat-form`. De `@[Sender]` prefix in input blijft (backward-compat met clients die alleen mentions snappen).
- `cancelReply()` (× knop in banner) wist STATE.replyTo en stript `@[..]` prefix uit input.
- chat-form submit-handler stuurt `parent_id: STATE.replyTo.id` mee in payload, en wist STATE.replyTo na verzenden.

**Toggle (07-bootstrap.js + template):**
- Avatar-menu krijgt item "Threading-indicators: aan/uit" tussen Callsign en Wachtwoord.
- `toggleThreadingPref()` flipt `STATE.threadingEnabled`, schrijft localStorage, refresh't badges, en verlaat eventuele thread-view als 'ie wordt uitgezet.

**CSS (`static/app.css`):**
- `.thread-badge` — pil-vormig, blauwe achtergrond, klein font.
- `#thread-banner` — gele balk boven log.
- `#reply-banner` — lichte achtergrond boven chat-form met × knop.

**Bekende grens:** parent-chain-lookup in `isInThread` is O(N) per msg (lineair scan in STATE.msgs). Voor typische 30-100 msgs prima; bij 1000+ in één view zou een hash-map nodig zijn. Niet nu.

**Test-suggestie:** stuur een msg, klik Reply op een ontvangen msg, stuur nog één → badge `💬1` verschijnt bij de geantwoorde msg. Klik badge → filter naar die thread. Toggle in menu uit → badges verdwijnen.

---

## Fase B mobile (later, ook in HANDOFF.md sectie 8 item 6)

- Tabellen → kaart-stijl op mobile. Vereist `data-label="..."` op elke `<td>` in dynamische HTML — moet door alle table-renders in `02-tree.js` / `04-admin.js` / `05-reports.js`.
- Chat-controls compacter (filter + tijd-knoppen wrappen nu lelijk op smal scherm).
- Admin-forms grondiger (al wel deels gedaan met `flex-direction:column` op `.row`).
- Landscape-tablet tweaks.

## Andere onderwerpen die in HANDOFF.md sectie 8 staan

- JS verder modulariseren (huidige 7-files-split is voldoende voor nu — user heeft hint genegeerd dit te doen).
- Smoke-tests voor `db.py`.
- Per-user `allowed_views` afmaken (UI + enforcement).
- QR-import/export contacten (endpoints bestaan al).
- Repeater Fase B (wrapper-forms voor radio/owner/position/region/admin-pw).
- Telemetry-paneel (`req_telemetry_sync`).

---

## Belangrijke context voor de volgende agent

- **De user gebruikt `uv`** (niet pip) voor deps. `pyproject.toml` + `uv.lock` zijn de bron van waarheid. `requirements.txt` wordt parallel onderhouden voor Docker-builds.
- **User-voorkeur:** kort en bondig, vragen stellen bij twijfel, eerlijk over onzekerheid. (Staat in user-preferences.)
- **Lint-loop JS:** `for f in static/js/*.js; do node --check "$f"; done`.
- **Lint-loop Python:** `python3 -c "import ast; [ast.parse(open(f).read()) for f in ('gateway.py','db.py','web.py','bot.py')]"`.
- **Jinja2-render smoke:** vanuit sandbox doet `PYTHONPATH=/usr/lib/python3/dist-packages python3` + `from jinja2 import Environment, FileSystemLoader; env=Environment(loader=FileSystemLoader('templates')); env.get_template('index.html').render(VERSION='x', request=None)`. Werkt zonder de hele FastAPI-stack.
- **Versie-conventie:** bump de patch (`z` in `x.y.z`) in `APP_VERSION` (`web.py`) bij elke door user gevraagde wijziging. Synchroniseer met `HANDOFF.md` regel 1.
- **De repo werkt op 1 branch** — geen feature-branch-discipline nodig.
- **Sandbox-limitaties:** `rm` op bestaande files in de WebClient-folder faalt soms met permission denied — file-delete door user laten doen. `.git/index.lock` van IDE blokkeert soms `git add` — user moet IDE-git stoppen.
- **App-smoke vanuit sandbox onmogelijk** (geen USB-device, geen `meshcore`-deps); user test handmatig na elke wijziging.
- **Caveman-skill** werd gebruikt voor één korte statusupdate eerder in de sessie; user kan 'm opnieuw aanvragen ("caveman aan").

---

## Suggested skills

- `engineering:debug` — voor diagnose van het openstaande portrait-issue (iOS `100vh` vs `100dvh`, URL-bar gedrag).
- `engineering:code-review` — voor de toekomstige fase-B tabellen-rewrite (gestructureerde aanpak nodig om alle table-renders consistent te krijgen).
- `caveman` — alleen als user zelf vraagt om beknopte modus.
