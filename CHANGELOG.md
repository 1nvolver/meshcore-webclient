# Changelog — MeshCore Gateway Web Client

Per-versie wijzigingen, chronologisch (nieuwste onderaan).

- **Huidige versie:** v1.1.049 (zie laatste sectie).
- **Voor architectuur, DB-schema, voltooide features en backlog:** zie `HANDOFF.md`.
- **Voor end-user setup / deploy / update-procedure:** zie `README.md`.

Versie-conventie: `x.y.z` waar `z` bumpt bij elke door de user gevraagde
wijziging. Pure doc-wijzigingen (zoals deze rename) krijgen geen versie-bump.

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

### v1.1.035 — QR import/export van contacten
HANDOFF §8 item 2 afgewerkt. Endpoints (`/contacts/export`, `/contacts/import`) bestonden al sinds eerdere fasen — UI was bewust nog niet gebouwd omdat er externe libs nodig waren. Nu opgeleverd met lokale vendor-libs (geen CDN).

**Vendor-libs in `static/vendor/`** (geen externe CDN, werkt offline op LAN-Pi):
- `qrcode-generator.min.js` — Kazuhiko Arase, MIT, ~21KB. Encoder voor de export-modal.
- `jsQR.min.js` — cozmo, Apache-2.0, ~257KB. Decoder voor camera-scan + foto-upload.

**Nieuwe JS-file `static/js/08-qr.js`** (~245 regels) — geladen vóór `07-bootstrap.js` in `templates/index.html` zodat bootstrap als laatste blijft.
- `_qrOpenModal(title, html)` / `_qrCloseModal()` — generieke modal-overlay (backdrop-klik + ESC sluiten; één tegelijk actief).
- `_qrRenderToCanvas(canvas, text)` — `qrcode(0, 'M')` auto-grootte, 4-px cellen, witte margin.
- `showMyQR()` — fetcht `/contacts/export` (geen key = eigen card), opent modal met QR, copy-hex-knop, node-naam.
- `showImportQR()` — modal met twee paden: camera-scan en foto-upload.
- `_qrStartScan()` / `_qrStopScan()` — `getUserMedia({facingMode: {ideal: 'environment'}})` (achterkant-cam preference), `requestAnimationFrame`-loop met `jsQR(..., {inversionAttempts:'dontInvert'})`. Stopt stream bij modal-close.
- `_qrHandleFile(ev)` — `<input type=file accept=image/*>` → `Image` → canvas → `jsQR(..., {inversionAttempts:'attemptBoth'})` (ook geïnverteerde QR's; foto's hebben vaker rare belichting).
- `_qrOnDecoded(text)` — strip whitespace, valideer hex-only + even-length + min 16 chars, POST `/contacts/import`, sluit modal, `refreshAndRerender()`.

**Templates / CSS:**
- `templates/index.html`: vendor-scripts toegevoegd vóór de app-modules. Geen `?v={{VERSION}}`-cache-buster op vendor (content verandert niet tussen onze versies). Avatar-menu kreeg item "Mijn QR (deel contact)…" tussen Threading en Wachtwoord.
- `static/js/04-admin.js` (`renderAdminContacts`): knop "Importeer via QR…" boven de contacten-tabel.
- `static/app.css`: generiek modal-systeem (`.modal-overlay`, `.modal-box`, `.modal-head`, `.modal-body`, `.modal-actions`), QR-display (witte rand, pixelated rendering), QR-scanner (`.qr-scan-wrap` met `<video>`, status-text-coloring), responsive sizes (44px tap-targets op ≤767px). ~35 regels.

**Bewuste keuzes (eerder besproken met user):**
- Lokale vendor-libs in `static/vendor/` ipv CDN — gateway moet offline werken op LAN-Pi.
- Export alleen via avatar-menu (self-serve eigen card). Geen QR-knop per contact in admin-tabel — user wilde minimaal beginnen.
- Geen handmatige hex-text-area in import-modal — alleen camera + foto-upload.
- Geen aparte URL-scheme of base64-prefix — QR-payload is raw hex. Interop met `meshcore://`-URI's van andere clients is een latere zorg.
- Error-correction 'M' (~15%) — ruimte voor toekomstige logo-overlay in de QR.

**Bekende grenzen** (zie HANDOFF §6):
- Camera-scan vereist HTTPS of localhost (browser-secure-context-policy); op `http://192.168.x.x` werkt scannen niet — foto-upload wel.
- jsQR-bundle is ~257KB, geen aparte minified versie van upstream.
- Hex-only validatie betekent dat eventuele andere MeshCore-clients met URI-prefix QR's afgewezen worden tot iemand dat nodig heeft.

**Lint:** Python + alle 8 JS-files schoon. Jinja2-render-smoke: vendor + 08-qr.js + "Mijn QR" aanwezig in output. App-smoke door user op productie.

### v1.1.036 — bugfix: export gebruikt 'uri'-veld (meshcore-py CONTACT_URI)
User test op productie: "Mijn QR" toonde "Geen card-data van companion ontvangen" en niets in de log. Oorzaak: meshcore-py's `export_contact` returnt een `CONTACT_URI`-event met `payload = {"uri": "meshcore://<hex>"}` (zie `meshcore/reader.py` regel 344 e.v., `PacketType.CONTACT_URI`). v1.1.035 zocht alleen op `card`/`data`/`export` keys — die zijn er nooit.

**`web.py /contacts/export`:**
- Leest nu `payload.get("uri")` eerst, derive `card` (hex) door `meshcore://` prefix te strippen. Backward-compat: leest ook nog steeds `card`/`data`/`export` voor oudere SDK-versies of toekomstige varianten.
- Accepteert ook `payload` als plain `str` (= URI) of `bytes` (= raw bytes → `.hex()`).
- Returnt nu **zowel** `uri` als `card` in de respons.
- Logt expliciet bij `ERROR`-event en bij onverwacht payload-formaat — voorheen ging dit stil. Module-logger `meshcore.web` via `logging.getLogger(...)`; uvicorn-stack pikt dit op naar stdout/journal/docker-logs zonder extra config.
- Detecteert ook `getattr(ev.type, 'name', '')` om ERROR-events apart te behandelen ipv "0 keys"-payload misverstand.

**`web.py /contacts/import`:**
- Accepteert nu zowel `card` (hex) als `uri` ("meshcore://..."). URI-prefix wordt server-side gestript voor `bytes.fromhex()`.
- Exception bij `import_contact()` wordt gelogd met full traceback (was alleen HTTPException-detail).

**`web.py` infra:**
- `import logging` toegevoegd; module-logger `log = logging.getLogger("meshcore.web")`. Geen prints elders aangepast (project gebruikt logging tot nu toe niet — laag-impact toevoeging).

**`static/js/08-qr.js`:**
- `showMyQR()`: zet de **URI** in de QR-payload (niet de losse hex). Interop: andere MeshCore-clients (cli, mobile apps) die `meshcore://...` scannen herkennen dit nu. Backward-compat: fallback naar hex als backend om welke reden dan ook geen `uri` geeft.
- Modal toont nu "Payload (N chars): ..." (was "Card-hex"). "Kopieer hex" werd "Kopieer".
- `_qrOnDecoded()`: strip `meshcore://`-prefix als die in de gescande string staat; valideert dan de hex. Stuurt de **ruwe** gescande string mee als `uri`-veld naar `/contacts/import` zodat backend-log laat zien wat er werkelijk gescand werd (handig voor toekomstige interop-issues).

**Niet gewijzigd:** UI-flow (avatar → Mijn QR; admin → Contacten → Importeer via QR…), modal-systeem, vendor-libs.

**Geen lint-regressies.** Render-smoke nog steeds groen. Test op productie noodzakelijk om te bevestigen dat de URI uit `meshcore-py` zoals verwacht binnenkomt.

### v1.1.037 — Officieel QR-formaat + Privé-tree + UI-herstructurering

Drie samenhangende user-stories opgepakt in één bump:
1. **QR-formaten opwaarderen** naar het officiële MeshCore-formaat (zie [docs.meshcore.io/qr_codes](https://docs.meshcore.io/qr_codes/)) — onze QR's worden nu herkend door de Android-app, en omgekeerd.
2. **UI-herstructurering**: QR-contact-import verhuist van Admin → Contacten naar DM → Contactpersonen. Hex-form weg. Auto-add aan `/my/contacts` na import.
3. **Privé-kanalen** krijgen een eigen tree-tak met QR import/export, aanmaken en verwijderen.

Plus een modal-CSS-fix omdat knoppen op telefoons met ronde hoeken net onder de viewport-rand vielen.

**Backend (`web.py`):**
- `/contacts/export` herschreven: bouwt nu `meshcore://contact/add?name=...&public_key=<64hex>&type=<int>` zelf via `get_self_info` (eigen card) of `mc.contacts` (key-argument). De oude meshcore-py `export_contact()` SDK-call wordt niet meer gebruikt — die geeft een verschillend formaat (`meshcore://<rawhex>`) dat niet interopt.
- `/contacts/import` herschreven met formaat-detectie: officieel URL (`meshcore://contact/add?...`) → parse query-params → `add_contact(contact_dict)` met minimale velden (`public_key`, `type`, `adv_name`, `flags=0`, `out_path=""`, `out_path_len=-1` voor flood); legacy raw-hex → bestaande `import_contact(bytes)` (backward-compat met v1.1.036-QR's).
- Nieuwe `GET /admin/channels/{idx}/export`: roept `get_channel(idx)` aan, bouwt `meshcore://channel/add?name=...&secret=<32hex>` uit `channel_name` + `channel_secret` (16 bytes hex).
- Nieuwe `POST /admin/channels/import`: parse URL, pak volgend vrije slot 1-7, `set_channel(slot, name, bytes.fromhex(secret))`, DB-spiegel via `upsert_channel(kind='private')`. Faalt met 409 als alle slots vol zijn.
- Beide channel-endpoints zijn admin-only (`_admin_or_403`).

**Frontend tree-structuur (`templates/index.html` + `static/js/02-tree.js`):**
- Nieuwe `grp-private` tree-group tussen Chat en DM met `<ul id="tree-private">` en een `<li id="li-priv-mgr">Beheren…</li>` + `<div id="priv-add-link">+ privé-kanaal</div>`. Beide laatstgenoemde zijn `display:none` in HTML; `loadMe()` zet ze `display:''` voor admins, niet voor non-admins.
- `renderTree()` splitst nu channels: public + hashtag → `#tree-channels` (Chat-tak); private → `#tree-private` (Privé-tak).
- Nieuwe `selectPrivChansManager()` (admin-only check) + `renderPrivChansManager()` met tabel van privé-kanalen, QR-knop + remove-knop per row, "Importeer via QR…"-knop bovenaan en een aanmaken-form (naam + optioneel slot + optionele 32-hex sleutel, leeg = random). Gebruikt bestaand `/admin/channels/add` met `kind=private`.
- `_hideAllViews()` en `refreshAndRerender()` weten van de nieuwe `'privchans'` view.
- Nieuwe div `#privchans-view` in template.

**DM → Contactpersonen (`02-tree.js`):**
- Sectie "Contactpersoon toevoegen" met name+pubkey-form **verwijderd**. Inclusief de `addMyContact()`-functie. Toevoegen gaat nu uitsluitend via QR-import.
- Bovenaan een "Importeer contact via QR…"-knop (admin-only zichtbaar; non-admins zien een note).
- Het oude "Importeer via QR…"-knopje uit `renderAdminContacts` is vervangen door een note die naar DM → Contactpersonen verwijst.

**`static/js/08-qr.js`:**
- `showMyQR()`: gebruikt `resp.uri` (= officieel URL) als QR-payload. Modal toont "Kopieer URL" ipv "Kopieer hex".
- `_qrOnDecoded()`: detecteert channel-URL's (`meshcore://channel/...`) en geeft fout-toast — die horen via Privé → Beheren. Officieel contact-URL en legacy raw-hex worden beide naar `/contacts/import` gestuurd.
- Na succesvolle import met `resp.name` + `resp.public_key` (officieel formaat): automatisch POST naar `/my/contacts/add` zodat de contact ook in de eigen lijst staat. Toast bevestigt allebei.
- Nieuwe `showChannelQR(slot)` — fetcht `/admin/channels/{slot}/export`, opent modal met QR + copy-URL.
- Nieuwe `showImportChannelQR()` — modal met camera-scan + foto-upload, decoder gaat via `_qrOnChannelDecoded()` ipv contact-decoder.
- `_qrStartScan(isChannel)` en `_qrHandleFile(ev, isChannel)` accepteren nu een channel-modus-vlag; scan-state houdt `isChannel` bij voor de decode-routing.

**Modal-CSS fix (`static/app.css`):**
- Overlay-padding 20px (was 12px) en `env(safe-area-inset-bottom)` voor notch-vrije onderrand. Modal-box gebruikt `max-height:calc(100dvh - 40px)` voor dynamic-viewport-aware sizing.
- `.modal-actions` als sticky footer binnen `.modal-body` (`position:sticky;bottom:0`) — knoppen blijven zichtbaar ook bij scrollende content. `justify-content:center`. Border-top als visuele scheiding.
- Knoppen: `min-height:44px` (was 36px desktop / 44px mobile — nu overal 44), `min-width:120px`, `padding:10px 18px`. Op mobile: `min-width:100%` zodat één knop per regel = makkelijker tappen.

**Bewuste keuzes:**
- Beide formaten naast elkaar accepteren bij contact-import (officieel + legacy) — clients die nog v1.1.036-QR's hebben rondgestuurd moeten geen breaking change zien.
- Channel-import altijd op het volgende vrije slot, geen UI om slot te kiezen. Vereenvoudigt de flow; user kan na import altijd nog herorden via "verwijder" + "nieuwe import".
- Privé-kanaal-beheer onder eigen tree-tak, niet langer onder Admin → Channels. Admin-tak's Channels-sub blijft bestaan voor hashtag-management + scope-edits (voorlopig — opruimen kan later als het rommelig wordt).
- Geen handmatige sleutel-text-area voor channel-import; alleen QR-flow. Het URL-formaat is sowieso korter dan een typische rawhex-card.

**Bekende grenzen / TODO:**
- Auto-add aan `/my/contacts` faalt stil als de contact al bestaat (409). De api()-toast laat dat zien; modal blijft niet hangen.
- Channel-import pakt altijd het eerste vrije slot — geen optie om bewust een specifiek slot te vullen. Backend `_set_channel_on_node` ondersteunt het wel; UI doet het niet.
- Companion's `set_channel` met dezelfde naam op een ander slot maakt geen waarschuwing — dubbele kanalen kunnen ontstaan als user dezelfde QR meerdere keren importeert.
- De Admin → Channels tab toont ook nog steeds privé-kanalen (inclusief remove). User wilde private uit het admin-deel hebben — admin tab is nu nog gemengd. Schoonmaken kan in een volgende bump als de Privé-tak in gebruik blijkt te zijn.

**Lint:** Python + alle 8 JS-files schoon. Jinja2-smoke checkt grp-private, tree-private, li-priv-mgr, privchans-view, selectPrivChansManager, 08-qr.js. Render OK.

### v1.1.038 — QR-scan robuuster + tree-cleanup

Twee kleine fixes na user-test van v1.1.037:

**Privé → '+ privé-kanaal' link verwijderd** (`templates/index.html`, `07-bootstrap.js`):
- Redundant — het "Beheren…"-item bovenaan de Privé-tak biedt al alle functionaliteit (aanmaken-form + verwijderen + QR-import/export). De extra link voegde alleen visuele ruis toe.

**Camera-scan robuuster** (`static/js/08-qr.js`):
- **Idempotent**: `_qrStartScan` doet niets als een scan al draait. Tweede klik op "Camera scannen" startte voorheen een nieuwe `getUserMedia`-call op de oude stream — op Android Chrome veroorzaakt die call een korte autofocus-flits die door user als "lijkt op een foto maken" werd ervaren. Knop wordt nu ook `disabled` zolang scan loopt.
- **`inversionAttempts: 'attemptBoth'`** (was `'dontInvert'`) in de video-scan-loop. Hetzelfde regime als de file-upload-decoder. Helpt bij Android-scherm-reflecties die de QR effectief inverteren (lichte modules op donkerder achtergrond). Iets duurder qua CPU maar onmerkbaar op moderne devices bij 720p@30fps.
- **Hogere ideal video-resolutie**: `{width: {ideal: 1280}, height: {ideal: 720}}` in de constraints. Voorheen liet browser default 480p toepassen — bij een telefoonscherm dat 30cm van de camera staat en een typische QR van 250×250 px op het scherm, geeft 480p maar ~120×120 px voor de QR zelf, vaak te weinig voor jsQR's locator-finder. 720p verdubbelt dat. `ideal` is een hint; browser/HW kan downscalen.
- **`video.readyState >= HAVE_CURRENT_DATA (2)` + `videoWidth > 0`** ipv `=== HAVE_ENOUGH_DATA (4)`. Sommige browsers blijven op `readyState=3` hangen totdat de eerste frame écht doorkomt; de oude check sloeg dan altijd het draw-image gedeelte over.
- **Status-feedback tijdens scan**: status-text toont nu "Zoeken naar QR-code… (frames: N, res: WxH)" elke 30 frames (~1s). Maakt zichtbaar dat de loop draait en geeft de daadwerkelijke videoresolutie ter diagnose. Bij stilstand op "Wachten op camera-frames…" weet user dat de video-pipeline klem zit.

Lint groen, render-smoke groen.

### v1.1.039 — Admin → Contacten weg + generieke stale-cleanup

User-test van v1.1.038 verliep goed (QR aanmaken/scannen/opslaan werkt). Vervolg-wens: Admin → Contacten verwijderen (overlap met DM → Contactpersonen) en de Stale-repeater-housekeeping uitbreiden tot een algemener stale-contacten-tool met datum-picker en type-keuze.

**Admin → Contacten weg** (`templates/index.html`, `static/js/04-admin.js`):
- `<li onclick="selectAdminView('contacts')">` verwijderd uit grp-admin.
- `case 'contacts':` uit de admin-switch verwijderd.
- `renderAdminContacts` en `removeContact` JS-functies verwijderd (~45 regels).
- Backend `/contacts/remove` endpoint blijft bestaan voor backward-compat met scripts en de generieke cleanup-helper (gebruikt nu in /admin/contacts/cleanup en /admin/repeaters/cleanup).
- DM → Contactpersonen blijft de plek voor de contactenlijst zoals de user 'm dagelijks gebruikt; companion-knowledge-overview was admin-eye-candy maar zelden actionable.

**Generieke stale-contacts-cleanup** (`web.py`):
- Nieuwe helper `_stale_contact_candidates(age_secs, type_set, skip_favorites)` — vervangt de oude `_stale_repeater_candidates` (die was hardcoded op types {2,3}, 28-dagen, skip-favs=true).
- Nieuwe helper `_parse_stale_params(days, types, skip_favs)` voor consistente query/body-parsing met validatie. Types is CSV (`"1"`, `"1,2"`, `"2,3"` etc.); alleen {1,2,3,4} toegestaan; days moet ≥ 0.
- Nieuw `GET /admin/contacts/stale?days=N&types=2,3&skip_favorites=1` — preview-endpoint. Returnt `{count, age_days_threshold, types, skip_favorites, items[]}`.
- Nieuw `POST /admin/contacts/cleanup` met body `{days, types, skip_favorites}` — voert delete uit.
- **Legacy aliases** `/admin/repeaters/stale` en `/admin/repeaters/cleanup` blijven werken; ze roepen `_stale_contact_candidates(28d, {2,3}, True)` aan — zelfde resultaat als v1.1.038. Geen breaking change voor eventuele scripts of cron-jobs.

**Housekeeping UI uitbreiden** (`static/js/04-admin.js`):
- "Stale repeaters opruimen" hernoemd naar "Stale companion-contacten opruimen".
- **`<input type="date">`** ("Sinds datum…") met default 28 dagen geleden en `max=vandaag`. Live "(X dagen geleden)"-hint naast de input zodat user feedback krijgt over wat 'ie geselecteerd heeft.
- **Type-checkboxes**: clients (uit by default — minder risico op per ongeluk legitieme contacten kwijtraken), repeaters (aan), rooms (aan). Sensors (type 4) niet in UI omdat ze in de praktijk nog niet voorkomen; backend ondersteunt 't wel.
- **Checkbox "Favorieten overslaan"** (default aan). Off zou ook favoriete repeaters/rooms verwijderen — disclaimer bij confirm.
- Knoppen: "Toon kandidaten" (preview) en "Verwijder N contacten" (na preview). Confirm-dialog toont count.
- Render-tabel ongewijzigd (Naam / Type / Pubkey-prefix / Laatste advert / Leeftijd).

**Bewuste keuzes:**
- Date-picker ipv "ouder dan N dagen"-number-input: gebruiksvriendelijker voor "alles voor vorige maand". Backend werkt intern nog steeds met `age_secs`; UI rekent days van datum-naar-nu.
- Clients (type=1) standaard uit: voorkomt dat een DM-partner die toevallig al een tijd offline is per ongeluk weggesneeuwd wordt. User kan 't aanvinken als 'ie weet wat 'ie doet.
- Sensor-type (4) ondersteund in backend, niet in UI — voeg checkbox toe zodra firmware sensors gangbaar exporteert.

**Lint**: Python + alle 8 JS schoon. Render-smoke groen (Admin → Contacten verdwenen, alleen Housekeeping nog).

### v1.1.040 — DM-naam lookup + tree-volgorde

User-feedback na test van v1.1.039: in een DM-conversatie verschijnt de afzender als pubkey-prefix ("2e400317326b: Hey") en hetzelfde komt terug in de view-title. Reden: companion levert voor DM's alleen `peer = pubkey_prefix` mee, geen `adv_name`; de UI gebruikte die prefix als sender-naam zonder lookup.

**Naam-lookup helper** (`static/js/01-core.js`):
- Nieuwe `_resolveContactName(prefix)`: zoekt eerst in `STATE.myContacts`, dan in `STATE.contacts` op exacte 12-char prefix-match (of via volledige pubkey die start met prefix). Returnt `null` als niet gevonden — caller kan dan naar prefix fallback'en.
- Generiek bruikbaar; niet beperkt tot DM. Mention-detection en thread-heuristiek kunnen dit later ook gebruiken als het nodig is.

**`extractSender(m)` in `03-chat.js`:**
- Voor `m.kind === 'dm'`: probeer `_resolveContactName(m.peer)` eerst. Bij hit → naam; anders prefix (huidig gedrag). Channel/public-flow ongewijzigd.

**`selectChannel(ch)` in `02-tree.js`:**
- View-title voor DM is nu **alleen de naam** (geen `(DM · prefix)` suffix meer) — de prefix staat al in het detail-paneel en in de tree.
- Als `ch.name` leeg of identiek aan `ch.peer` (= prefix) is: secundaire lookup via `_resolveContactName(ch.peer)`. Dat geeft de juiste naam terug zelfs als de tree-bron geen `name` had.
- Placeholder van het chat-input-veld gebruikt nu dezelfde naam.

**DM-tree volgorde geflipt** (`02-tree.js` + `templates/index.html`):
- Het "Contactpersonen"-Beheren-item stond bovenaan; nu onderaan, na de feitelijke DM-contacten. Inline border-bottom in template verwijderd; JS zet nu `border-top` boven het item zodat de visuele scheiding correct staat.
- Lege staat (geen DM-contacten): italic placeholder verschijnt tussen het top van de tak en het mgr-item.

**Lint**: Python + alle 8 JS schoon. Geen render-smoke wijzigingen (template-edits zijn minimaal).

### v1.1.041 — Callsign-modal + modal-helpers gepromoveerd

HANDOFF §8 item 1 afgewerkt: `changeCallsignPrompt()` gebruikt geen `prompt()` meer (geen emoji-picker op desktop Chrome) maar een HTML-modal met inline emoji-row + live voorbeeld.

**Modal-helpers gepromoveerd naar 01-core.js:**
- `_qrOpenModal` → `openModal(title, html, onClose?)` — generieke functie. Optionele `onClose`-callback voor cleanup (bv. camera-stream stoppen).
- `_qrCloseModal` → `closeModal()`.
- `08-qr.js` gebruikt nu de generieke helpers. Aparte `_qrCloseAndStopScan()`-wrapper roept eerst `_qrStopScan()` aan en daarna `closeModal()` — vervangt alle plekken waar voorheen `_qrCloseModal` werd aangeroepen. Camera-stream wordt nooit per ongeluk in de lucht gelaten bij modal-sluit.
- Sed-replace door alle `_qrOpenModal(` → `openModal(` en `_qrCloseModal(` → `_qrCloseAndStopScan(` in `08-qr.js`.

**Callsign-modal in `07-bootstrap.js`:**
- `changeCallsignPrompt()` is nu sync (geen await meer); bouwt modal met `openModal(...)`. Sluit avatar-menu voor modal opent.
- **Input-veld** `<input id="cs-input">` met `maxlength=32`, font-size 16px (geen iOS-zoom), `min-height:40px`, current value pre-gevuld. Cursor naar eind bij open. Enter = opslaan.
- **Live voorbeeld**: `<code id="cs-preview">` toont `[XX] Hallo!` (of "(geen prefix — uit)" als leeg). Update bij elke `input`-event.
- **Inline emoji-row**: hergebruikt `EMOJIS`-array uit `03-chat.js` (global, plain script). 8-koloms grid in `.cs-emoji-row` div; klik op een emoji-knop voegt 'm in op cursor-positie in `cs-input`.
- **Knoppen**: "Wissen (uit)" (leegt input + update preview) en "Opslaan" (POST naar `/me/callsign`, update menu-label, toast, sluit modal). Save-faal laat modal open.
- Helpers `_csSave()` en `_csClear()` als top-level globals (zodat de onclick-handlers in de gegenereerde HTML ze kunnen vinden).

**CSS toevoeging (`static/app.css`):**
- `.cs-emoji-row` grid-template (8 kolommen, 4px gap, lichtgrijze achtergrond, kleine padding).
- `.cs-emoji-row button.emoji-pick`: 1.2em font, 36px min-hoogte, wit met hover/active states.
- Mobile (≤767px): override op `.modal-body button{min-width:100%}` voor emoji-buttons in cs-emoji-row zodat ze niet één-per-rij worden (zou de hele picker eindeloos lang maken).

**Bewuste keuzes:**
- Emoji-row inline ipv popup zoals chat-`#emoji-picker`: minder UI-state (geen open/close), past in modal-flow, gridje vult max half de modal-hoogte.
- `EMOJIS`-array niet verplaatst naar 01-core.js — 03-chat.js blijft eigenaar, 07-bootstrap.js gebruikt 'm als global. Minimal-impact.
- Knop "Wissen (uit)" ipv "Verwijder" — minder dreigend; user behoudt focus op de input voor nieuwe invoer.

**Backlog opruiming:**
- HANDOFF §8 item 1 (callsign-modal) verwijderd; resterende items hernummerd. Item 5 (smoke-tests) en 6 (threading-performance) gemarkeerd als "alleen bij trigger" — bevestigd in gesprek met user dat ze niet urgent zijn.

**Lint**: Python + alle 8 JS-files schoon. Render-smoke groen.

### v1.1.042 — Mobile fase B

HANDOFF §8 item 2 (mobile fase B) afgewerkt: tabellen op mobile als kaarten, chat-controls compact, admin-forms volledig responsive, landscape-tablet krijgt 3-koloms layout.

**Tabellen → kaarten op ≤767px** (`static/app.css` + alle dynamische tabellen):
- CSS-rule `table:not(.no-card) tr { display:block; border; padding; margin }` met `td[data-label]::before { content: attr(data-label) }` als label-prefix in uppercase. `thead` verborgen. Elke rij is een visueel kaartje met de kolom-labels links voor de waarden.
- `data-label="Kolomnaam"` toegevoegd aan elke `<td>` in 8 dynamische tabellen:
  - `02-tree.js`: Mijn contactpersonen (Status·Naam / Pubkey / Notitie / Toegevoegd), Privé-kanalen (Slot / Naam / Scope).
  - `04-admin.js`: Channels (# / Type / Naam / Alias / Scope), Stale-housekeeping (Naam / Type / Pubkey-prefix / Laatste advert / Leeftijd), Bots (Naam / Kanaal / Keyword / Status / Reply), Gebruikers (Naam / Rol / Wachtwoord / Laatste login).
  - `05-reports.js`: Top-kanalen (Kanaal / Aantal), Repeaters (Naam / Type / Hash / Pubkey-prefix / Laatste advert / Locatie / Path / Ping).
- Action-cellen (knoppen-cellen aan het eind van een rij) krijgen geen `data-label` — dan blijft alleen de knop staan zonder label-prefix.
- Escape-hatch: een `<table class="no-card">` valt terug naar horizontaal-scroll (zoals fase A).

**Chat-controls compacter** (`static/app.css`):
- Filter-input op eigen rij (was al fase A).
- Tijd-knoppen (−12u/−2u/+2u/+12u/↑ouder) krijgen `flex:1 1 0` — verdelen evenredig de beschikbare ruimte ipv lelijk te wrappen.
- "nu" en pauze-knop blijven compact (`flex:0 0 auto; min-width:44px`).
- Knop-hoogte 40px (tap-target). Filter-input 16px font-size (geen iOS-zoom).

**Admin-forms responsive** (`static/app.css`):
- `.row label` krijgt uppercase + bold + kleinere font — zichtbare visuele scheiding bij column-flow op mobile (was eerder normaal-tekst die in 1 visuele blob met de input bleef hangen).
- `.row input/select/textarea` forceert `width:100% !important; flex:1 1 auto !important` op mobile — overrult alle inline `style="width:120px"` (priv-slot, hk-date, lat/lon) die op desktop nuttig waren maar mobile lelijk maakten.
- `.row label input[type=checkbox]` houdt z'n inline-layout (housekeeping types: clients/repeaters/rooms).

**Landscape-tablet 3-koloms** (`static/app.css`):
- Nieuwe media-query `@media (min-width:1024px) and (max-width:1199px) and (orientation:landscape)`: detail-pane wordt inline (zoals desktop) ipv overlay. Geen backdrop. Tree 200px + main + detail 260px past op iPad-landscape (1180×820) en kleine laptops in die range.
- Tablet portrait (768-1023, of landscape <1024) blijft overlay-detail.

**Bewuste keuzes:**
- Per-`td` `data-label` ipv één CSS-rule met `:nth-child` — robuuster bij wijzigende kolom-volgorde. Net iets meer string-in-JS maar future-proof.
- `text-transform:uppercase` + bold op label-prefix: visueel duidelijk dat het een label is, niet content. Alternatief was "Kolomnaam: " in mixed-case, leek minder strak.
- Landscape-tablet pas vanaf 1024px breed — 768-1023 landscape (kleine tablets, telefoon-landscape) heeft niet genoeg ruimte voor 3 kolommen.
- Geen `data-label` op de Channels-admin tabel's `tr.action-only`-cellen (zoals "scope" en "remove" buttons) zodat de kaart-stijl die op een eigen regel toont zonder "ACTIES:"-label.

**Bekende grenzen:**
- Bot-reply-tabel: lange replies (multi-line templates met `{TIME}` etc.) breken op smal scherm. Word-break werkt; horizontaal-scrollen niet nodig. Hopelijk OK.
- Repeaters-tabel heeft 9 kolommen — op kaart-stijl dus 9 regels per repeater. Bij veel repeaters wordt het scrollintensief. Voor nu acceptabel; later kan een "compacte view"-toggle helpen.
- `td[colspan]` (placeholder "geen items") wordt gecentreerd italic getoond — werkt visueel.

**Lint**: Python + alle 8 JS-files schoon. Render-smoke groen.

### v1.1.043 — Avatar-menu kleiner, Mijn profiel-submenu

User-wens na fase B: avatar-menu was te lang (7 items). Verplaats de persoonlijke instellingen naar een aparte "Mijn profiel"-modal, zodat het avatar-menu compact wordt.

**Template (`templates/index.html`):**
- Avatar-menu krijgt nu alleen: username (read-only header), "Mijn profiel…", "Logout", "Quit" (admin-only). Was: username + Callsign + Threading + Mijn QR + Wachtwoord + Logout + Quit. Vier items kleiner.

**Mijn profiel-modal in `07-bootstrap.js`:**
- `showProfileModal()` opent een modal getiteld "Mijn profiel — <username>".
- Body: `<ul class="profile-actions">` met 4 klikbare regels:
  - "Callsign" + value (huidige callsign of `(uit)`) → opent callsign-modal
  - "Threading-indicators" + value (aan/uit) → toggle + modal heropenen voor live label-update
  - "Mijn QR (deel contact)" → opent QR-export-modal
  - "Wachtwoord wijzigen" → opent wachtwoord-modal
- Sluit-knop onderaan. Klikken op een actie sluit de profiel-modal en opent de volgende modal (`openModal` doet altijd eerst `closeModal`).
- `_profileToggleThreading()` flipt threading + `showProfileModal()` opnieuw om de label te updaten (kort visueel flitsje, simpeler dan in-place DOM-update).

**Wachtwoord-modal** (`changePasswordPrompt` herschreven):
- Drie input-velden (huidig / nieuw / nogmaals) met `type=password`, `autocomplete=current-password` / `new-password`. Font-size 16px (geen iOS-zoom), min-height 40px.
- Focus naar huidig-veld bij open; Enter in laatste veld = opslaan.
- Validatie: alle 3 velden ingevuld, nieuw + nogmaals matchen, min 6 tekens. Faal-toast laat modal open.
- `_cpwSave()` POST naar `/change-password`, sluit modal bij succes.
- **Belangrijk**: `forcedChangePasswordPrompt` (eerste-login-flow bij tijdelijk wachtwoord) blijft `prompt()` gebruiken — die mag niet sluitbaar zijn (loop tot succes) en heeft andere semantiek. Aparte flow.

**CSS (`static/app.css`):**
- `ul.profile-actions` als card-list met border, item-min-height 48px voor goede tap-targets, hover-state, value rechts-gelijnd.

**Niet gewijzigd:**
- `updateCallsignMenuLabel()` en `updateThreadingMenuLabel()` blijven bestaan; ze checken al op `null` en zijn nu effectief no-ops (de gerefereerde menu-items zijn weg). Niet verwijderd voor backward-safety en omdat ze ergens later in profiel-flow nuttig kunnen zijn.
- Threading-state in `STATE.threadingEnabled` ongewijzigd; toggle gebruikt nog steeds `setThreadingEnabled` + `refreshAllThreadBadges` + evt. `exitThreadView`.

**Lint**: Python + alle 8 JS-files schoon. Render-smoke groen.

### v1.1.044 — UI-fixes: repeater-panel state + scope-modal

Twee user-gemelde UX-issues.

**Bug 1: repeater-panel reset elke 30s.**
Symptoom: in admin → Repeaters → repeater geselecteerd reset het paneel
periodiek; ingetypt admin-wachtwoord en CLI-commando-input verdwenen, CLI-history
scrollde naar boven. Oorzaak: `refresh()` (30s-loop in `07-bootstrap.js`)
roept altijd `renderDetail()` aan, en die rebuildt voor het repeater-paneel
het hele HTML-blok via `renderRepeaterManage()` — `innerHTML`-vervanging wist
uncontrolled inputs.

Fix: in `refresh()` `renderDetail()` skippen als
`STATE.view==='admin' && STATE.adminSub==='repeaters' && STATE.selectedRepeater`.
Het paneel is event-driven (login/logout/status/cli) — login-state, status en
cli-history worden allemaal door user-acties gezet, niet door periodieke fetch.
De andere views (chat-msg-detail, admin-system, kanaal-info) blijven gewoon
periodiek hertekend zoals voorheen.

**Bug 2: scope-popup onduidelijk.**
Symptoom: `editScope()` gebruikte `prompt('Scope voor slot N (leeg = geen scope):', cur)`.
Geen uitleg waarvoor scope dient, geen voorbeelden, geen maxlength.

Fix: vervangen door HTML-modal (`openModal`). Toont:
- Korte uitleg (flood-scope, `set_flood_scope()`, leeg = standaard flood).
- KV-blok met slot/kanaalnaam/type/huidige waarde.
- Input met `maxlength=64` (matcht DB-kolom), 16px font (geen iOS-zoom),
  placeholder `bv. #europa  (leeg = uit)`, Enter = opslaan.
- Voorbeelden (`#europa`, `#nl-noord`, `#regio-zuid`) + opmerking dat het
  vrije tekst is (geen `#`-verplichting; conventie is hashtag).
- Knoppen "Wissen (uit)" en "Opslaan". Helpers `_scopeClear()` en
  `_scopeSave(slot)` als nieuwe globals.

Past in lijn met v1.1.041 (callsign-modal) en v1.1.043 (wachtwoord-modal):
geleidelijke vervanging van `prompt()` door modals met betere UX.

**Niet aangeraakt:**
- Backend `/admin/channels/scope`-endpoint: scope blijft vrije tekst, geen
  validatie. Frontend-conventie (hashtag) is louter advies.
- Andere `prompt()`-aanroepen (bv. in `forcedChangePasswordPrompt` —
  bewust niet sluitbaar).

**Lint**: Python + alle 8 JS-files schoon. Render-smoke groen.

### v1.1.045 — Companion-wide default flood-scope (Node-settings)

User-wens: er bestaat ook een companion-wide default flood-scope (de SDK kent
`get_default_flood_scope` / `set_default_flood_scope`); de bestaande per-kanaal
scope overrulet die. Toevoegen aan Node-settings.

**SDK-context (`meshcore.commands.messaging`):**
- `set_flood_scope(scope)` — vluchtig: zet de scope voor de **volgende** sends.
  We gebruiken deze al per-channel via `_apply_channel_scope`.
- `set_default_flood_scope(scope)` — persistent op de companion zelf.
- `get_default_flood_scope()` — leest 'm; event-payload is
  `{scope_name, scope_key}`.

**Gateway (`gateway.py`):**
- Nieuw veld `GatewayState.default_scope: Optional[str]` (gecachte string,
  None = geen default).
- Nieuwe coroutine `load_default_scope(mc)` — best-effort `get_default_flood_scope`
  bij connect (timeout 2s, faalt stil als SDK/firmware 'm niet kent). Gevuld
  in dezelfde bootstrap-sequence als `load_self_info` (na connect + status).
- **`_apply_channel_scope` aangepast:** bij `ch.scope` leeg valt 't terug op
  `state.default_scope` ipv direct `set_flood_scope(None)`. Reden:
  deterministisch gedrag — channel-scope overrulet, geen channel-scope = de
  default. We wachten niet op firmware-interpretatie van scope-key `0*16`.
  Caching (`state.last_scope`) blijft werken zoals voorheen — alleen
  daadwerkelijke veranderingen triggeren USB-traffic.

**Backend (`web.py`):**
- `GET /admin/radio/default-scope` (admin-only): roept `get_default_flood_scope`
  aan, update gateway-cache, returnt `{ok, supported, scope_name, scope_key}`.
  Bij read-fail returnt 'ie cache + `error`-veld. Bij ontbrekende SDK-methode:
  `supported:false`.
- `POST /admin/radio/default-scope` body `{scope}` (admin-only): roept
  `set_default_flood_scope(scope)` aan. Update `state.default_scope` én
  invalideert `state.last_scope` zodat de eerstvolgende send met de nieuwe
  default werkt. Leeg/`None`/`""`/`"*"`/`"0"` = uit.

**Frontend (`static/js/04-admin.js`):**
- `renderAdminNode()` krijgt een tweede `<section>` "Default flood-scope" met:
  - Korte uitleg dat channel-scope overrulet en leeg = volle flood.
  - Input `#n-default-scope` (maxlength 31 — matcht SDK-buffer in
    `set_default_flood_scope` die `(31-len(scope))*b'\\0'` doet).
  - Wijzigen-knop (`setDefaultScope`) + clear-knop (`clearDefaultScope`).
  - Status-note `#n-default-scope-status` toont actieve waarde of fout.
- `loadDefaultScope()` haalt op via GET; toont `supported:false` als grijze
  disabled-input met uitleg.

**Caveats:**
- Companion-firmware moet de `GET_DEFAULT_FLOOD_SCOPE`-packet (cmd 64,
  reply-type 28) ondersteunen. Op oudere builds krijg je een timeout — de UI
  toont dan "Cache: (leeg) — read faalde …" maar Save werkt mogelijk nog wel.
- De `set_default_flood_scope`-SDK doet `(31-len(scope))*b'\\0'` als padding
  — als `len(scope) > 31` faalt 't. Daarom UI-maxlength 31.

**Lint**: Python + alle 8 JS-files schoon. Render-smoke groen.

### v1.1.046 — Repeater-sessie countdown + verleng-knop

HANDOFF §8 item 3 (sessie-keepalive of zichtbare countdown). Gekozen voor
**zichtbare countdown + expliciete verleng-knop** ipv automatische
keepalive — auto-keepalive zou USB-traffic genereren én de firmware-zijde
TTL is onbekend dus eventuele winst was onzeker.

**Frontend (`static/js/05-reports.js`):**
- `STATE.repeaterMgmt` krijgt `last_activity_ms` (epoch ms) en `ttl_secs`.
  `refreshRepeaterSession` vult ze uit het server-payload (`last_activity`
  is in seconden — *1000).
- Nieuwe globals `_repeaterCountdownTimer`, `startRepeaterCountdown`,
  `stopRepeaterCountdown`, `_tickRepeaterCountdown`, `_bumpRepeaterActivity`,
  `repeaterKeepalive`.
- `_tickRepeaterCountdown` (1 Hz interval) updatet `<b id="rep-mgmt-countdown">`
  via `textContent` — bewust **niet** via `renderDetail()` om de v1.1.044-bug
  (uncontrolled inputs wissen) niet terug te brengen. Self-cleanup: stopt
  zichzelf als `STATE.selectedRepeater` of `mgmt.logged_in` wegvalt; blijft
  passief draaien als 't paneel-DOM tijdelijk weg is (user op andere view).
- Bij ≤30s krijgt de span de `cd-warn`-class voor knipper-rood.
- Bij 0 → UI naar logout-state via `renderDetail()` + één toast.
- `repeaterLogin` (succes) en `refreshRepeaterSession` (logged_in) starten
  de countdown; `repeaterLogout` en `not_logged_in`-resp stoppen 'm.
- Elke succesvolle CLI (`repeaterAction`, `repeaterRunCli`) doet
  `_bumpRepeaterActivity()` zodat de client-side counter synchroon loopt met
  server-side `last_activity` (die backend al refresht na elke `/cmd`).
- "Verleng"-knop (`repeaterKeepalive`) doet onder de motorkap een echte
  `clock`-CLI via `repeaterAction` zodat ook firmware-zijde TTL (vermoedelijk)
  reset. Resultaat verschijnt in CLI-history zodat user bevestiging ziet.

**UI (`renderRepeaterManage` ingelogd-blok):**
- "Sessie verloopt over **mm Xs**" + verleng-knop + logout-knop op één rij.
- Note onder: "Elke verstuurde CLI-cmd telt ook als verleng. Bij verlopen
  valt de UI terug op het login-form."

**CSS (`static/app.css`):**
- `.rep-cd` (monospace+bold) en `.rep-cd.cd-warn` (rood, 1Hz knipper via
  `@keyframes cd-blink`).

**Niet veranderd:**
- Backend `/admin/repeaters/session` payload was al compleet — geen
  endpoint-wijzigingen.
- TTL blijft 120s (`_REPEATER_SESSION_TTL_SECS` in `web.py`).
- `forcedChangePasswordPrompt` en andere CSS/HTML ongewijzigd.

**Caveats (in HANDOFF al genoemd, hier herhaald):**
- Countdown is client-zijde hint op basis van server-TTL. Firmware-zijde
  TTL is onbekend — als de firmware eerder uitlogt, krijgt user
  `not_logged_in` op de volgende cmd; UI valt netjes terug op login-form.
- Een echte automatische keepalive zou periodiek USB-traffic vereisen
  zonder garantie dat 't iets oplost. Daarom alleen expliciete verleng.

**Lint**: Python + alle 8 JS-files schoon. Render-smoke groen.

### v1.1.047 — Persistente login (cookie 7d sliding)

User-wens: niet telkens opnieuw inloggen na tab-close. Keuze: alleen cookie
persistent maken (geen DB-backed sessions). Sliding 7d expiry. Server-restart
wist nog steeds alles (acceptabel — `_SESSIONS` blijft in-memory).

**`web.py`:**
- Nieuwe constante `SESSION_MAX_AGE = 7 * 86400`.
- `_new_session` schrijft `last_seen` mee in de sessie-dict.
- `_session(token)` checkt `now - last_seen > SESSION_MAX_AGE`; bij overschrijden
  pop't 'ie de sessie + returnt None (route's geven dan 401/redirect → user logt
  opnieuw in).
- Bestaande `set_cookie`-calls (login-POST, setup-POST en de change-password
  cookie-refresh) krijgen `max_age=SESSION_MAX_AGE`. Cookie was eerder een
  session-cookie zonder expiry → verdween bij tab-close.
- Nieuwe middleware `_sliding_session_cookie`: bij elke HTTP-response refresht
  hij `last_seen` + `set_cookie` met `max_age` (sliding). Throttled op 60s
  zodat UI-polling (10-30s) niet bij elke poll een Set-Cookie-header
  produceert. Skipt expliciet als de route zelf al een Set-Cookie voor
  `mc_auth` zette (login/logout) zodat we de delete-cookie van logout niet
  ongedaan maken.

**Niet gewijzigd:**
- `_SESSIONS` blijft een in-memory dict. DB-backed sessions waren een
  alternatieve route maar vraagt SCHEMA_VERSION-bump + Session-tabel — user
  koos voor de kleine fix.
- Auth-logica, password-hashing, role-check ongewijzigd.
- LOGIN_HTML/SETUP_HTML ongewijzigd.

**Caveats:**
- Cookie blijft 7d na last activity. Op een gedeeld apparaat = sec-risico —
  Logout-knop blijft de juiste actie. Bij twijfel: kortere TTL kiezen.
- Server-restart wist alles. Voor "ook na restart ingelogd" zou een
  Session-tabel nodig zijn.

**Lint**: Python schoon (web.py, gateway.py, db.py, bot.py). JS schoon.
Render-smoke groen.

### v1.1.048 — Hotfix v1.1.047: `_time` import-scope

Bug: na inloggen 500 Internal Server Error.
`NameError: name '_time' is not defined` in `_new_session` (regel 130).

Oorzaak: `_time` was alleen geïmporteerd binnen `create_app()` (regel 436),
maar `_new_session` staat op module-niveau. v1.1.047 introduceerde een
`_time.time()`-call in `_new_session` zonder dat te beseffen — lint vangt
dit niet omdat `ast.parse` geen name-resolution doet.

Fix: `import time as _time` verplaatst naar de top-level imports. De
binnen-`create_app`-import is een no-op comment geworden.

Test: `/login` POST werkt weer, cookie krijgt 7d max_age, sliding-refresh
draait via `_sliding_session_cookie` middleware.

**Lint**: PY OK, JS schoon, render-smoke OK.

### v1.1.049 — Containerisatie: GitHub Actions → GHCR → Portainer

User-wens: de app als container draaien op een Portainer-host (x86), image
gebouwd door GitHub Actions, gepubliceerd op GHCR onder `1nvolver`.

**Keuze: pull-based deploy.** CI bouwt en pusht, Portainer pullt. Alternatief
was build-on-host via een Portainer git-stack; afgewezen omdat de host dan
build-context + buildkit nodig heeft en je niet kunt garanderen dat het
draaiende image hetzelfde is als wat getest is.

**`Dockerfile`:**
- **Build-breker gefixt**: `useradd -m -u 1000 -g 1000 app` faalde omdat groep
  1000 niet bestaat in `python:3.12-slim`. Nu eerst `groupadd -g 1000 app`.
  Dit was nooit opgevallen — er is lokaal nooit een `docker build` gedraaid.
- `HEALTHCHECK` toegevoegd op `/healthz` (bestond al als unauthenticated route,
  raakt de DB niet). Via `urllib` want de slim-image heeft geen curl/wget.
  Portainer toont de container hierdoor als healthy/unhealthy.
- OCI-labels (`image.source` koppelt het package aan de repo op GHCR).

**`.github/workflows/ci.yml` (nieuw):**
- Job `checks` op elke push/PR: `ast.parse` van de 4 Python-modules ·
  `node --check` per file in `static/js/` · Jinja2 render-smoke van
  `index.html` · `docker compose config -q` op beide compose-bestanden ·
  guard die faalt als `APP_VERSION` niet in `HANDOFF.md` voorkomt.
- Job `build`: buildx + `docker/metadata-action` → GHCR. `linux/amd64`.
  Tags vanaf main: `latest`, `<APP_VERSION>`, `sha-<short>`; git-tags `v*`
  leveren ook hun eigen tag. PR's bouwen wél maar pushen níét.
- Auth via `GITHUB_TOKEN` + `permissions: packages: write` — geen secrets.
- gha-cache aan (`cache-from/to: type=gha`).

**`portainer-stack.yml` (nieuw):**
- Pullt `ghcr.io/1nvolver/meshcore-webclient:${MESHCORE_TAG:-latest}`, geen
  `build:` (Portainer-stacks kunnen dat niet altijd).
- `devices: /dev/ttyACM0:/dev/ttyACM0` + `group_add: ["20"]` voor serial-toegang
  als niet-root user. Bewust geen `privileged: true`.
- Named volume `meshcore-data` met expliciete `name:` zodat Portainer er geen
  stack-prefix voor plakt en de DB een redeploy overleeft.
- json-file logging met rotatie (10m × 3), `TZ=Europe/Amsterdam`.

**`docker-compose.yml`:** teruggebracht tot puur lokaal bouwen/testen
(`image: meshcore-gateway:dev`, `container_name: ...-dev`) zodat 't niet met
de productie-stack botst. `group_add` ook hier toegevoegd.

**`README.md`:** container-sectie herschreven (twee compose-bestanden,
by-id device-pad, group_add ipv privileged, healthcheck, volume-backup) +
nieuwe sectie "Deploy via GitHub Actions → GHCR → Portainer" met de
package-visibility-stap, stack-deploy, tag-pinning en update/rollback-procedure.

**`.gitignore`:** `data/` toegevoegd (lokale compose-bind-mount).

**Git:** `refactor/split-web` (7 commits vóór op `main`) fast-forward gemerged
naar `main`; `main` is de default branch waar CI op triggert.

**Lint**: Python schoon, JS schoon, beide compose-bestanden valide YAML,
workflow-YAML valide. Image is nog niet lokaal gebouwd (geen Docker in de
dev-omgeving) — de eerste echte build is de CI-run.

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
