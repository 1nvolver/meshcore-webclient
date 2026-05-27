# Handoff — MeshCore WebClient sessie 2026-05-25 / 26

**Project:** `/Users/dhammel/Library/CloudStorage/OneDrive-Flight815B.V/Development/Python/Meshcore/WebClient`
**Branch:** `refactor/split-web` (eerder gemerged naar `main`; user heeft feitelijk maar 1 branch)
**Eindstand bij dit handoff-moment:** **v1.1.028** (avatar-menu zichtbaarheid hersteld)

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
