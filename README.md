# MeshCore Gateway Web Client

Een desktop/Pi-gateway voor [MeshCore](https://meshcore.co.nz/) LoRa-mesh — verbindt een via USB aangesloten companion-radio met een eenvoudige web-interface (en CLI), zodat je vanaf elk apparaat in je netwerk kunt meelezen en mee-praten op het mesh.

De gateway bewaart al het verkeer in een lokale SQLite-database, ondersteunt meerdere web-gebruikers met rollen, biedt admin-instellingen voor de companion-firmware (radio, kanalen, contacten), en heeft een eenvoudig bot-framework voor automatische antwoorden.

---

## Wat doet de app

In één regel: **een MeshCore-companion zichtbaar en bestuurbaar maken vanuit je browser**, met persistente historie en multi-user toegang.

Concrete functionaliteit:

- **Chat**: real-time channel- en DM-berichten via WebSocket. Public channel, hashtag-kanalen (gedeelde naam-PSK) en private kanalen (eigen 128-bit AES-key).
- **Threading**: berichten die replies hebben krijgen een `💬N`-badge; klik om alleen die conversatie te tonen. Hybride: expliciet via Reply-knop én heuristisch via `@[X]`-mentions (binnen 30 min). Per-user aan/uit toggle.
- **Historie**: alle in- en uitgaande berichten in SQLite, met paginatie ("laad oudere") en server-side tekst-zoek.
- **Detail-paneel** per bericht: signaal-kleur (SNR-gebaseerd), hops, alle paden waarover een bericht is binnengekomen ("Heard X Times"), met repeater-namen waar bekend.
- **Multi-user web-UI**: één admin (eerste setup), daarna kunnen extra gebruikers worden aangemaakt met rol `admin` of `user`. Gebruikers zien Chat, DM en Rapportages → Overzicht; Admin-tak (incl. Repeaters-paneel) blijft verborgen.
- **Callsign**: optionele 1-3-tekens (of emoji) identifier per user; wordt automatisch als `[XXX] ` voor uitgaande berichten gezet zodat ontvangers zien welke web-user het verstuurd heeft.
- **Per-user opgeslagen contactpersonen** in DM-tree, met label en notities.
- **Admin-paneel**: radio-instellingen (freq/bw/sf/cr/tx-power/path-hash-mode), node (naam/locatie/reboot/advert), kanalen (met optionele flood-scope), contacten, repeaters (favorieten, stale-cleanup, OTA-management), housekeeping (DB clean/vacuum), gebruikers, bots, voorkeuren (telemetry, multi-acks, auto-add adverts).
- **Rapportages**: berichten-per-uur grafiek met instelbare periode, top-kanalen, ack-rate (DM).
- **Bot-framework**: simpele admin-defined bots die op `?keyword` reageren in een specifiek kanaal, met variabelen `{TIME}`, `{UPRADIO}`, `{UPNODE}`, `{HELP}`.
- **Notificaties**: gele highlight + geluid + browser-notification bij berichten waarin jouw node-naam ge-`@`-ed wordt.
- **Implicit ack-tracking** voor channel-msgs (wanneer een repeater jouw bericht herhaalt) en gewone DM-acks, met inline `✓`/`↻`/`✓✓`-indicatoren.
- **Mobile-responsive UI**: 3 breakpoints (mobile ≤767px / tablet 768-1199 / desktop ≥1200). Op mobile: hamburger-drawer voor de tree, bottom-sheet detail, 16px input-font (voorkomt iOS-zoom), 100dvh body-hoogte (corrigeert voor Chrome/Safari URL-bar).

---

## Vereisten

- **Hardware**: Raspberry Pi (5 of vergelijkbaar) of Mac/Linux-PC met een USB-aangesloten MeshCore companion-radio. Geteste hardware: Seeed XIAO nRF52840.
- **Firmware**: de **companion**-firmware (bv. de XIAO-companion-build). Dit is een ander firmware-image dan repeater of room-server. Zonder companion-firmware verschijnt bij start de melding `handshake mislukt: node antwoordde niet binnen 3.0s — is dit wel companion firmware?` en stopt de app.
- **Python**: 3.12 of nieuwer (3.14 getest). De projectfolder bevat een `.python-version` en `pyproject.toml` voor [`uv`](https://docs.astral.sh/uv/).

---

## Setup

### 1. Companion aansluiten

Sluit de nRF52840 met USB aan. Op macOS verschijnt 'm meestal als `/dev/cu.usbmodem*`, op Linux als `/dev/ttyACM0`. De gateway detecteert het pad automatisch via VID `0x2886` (Seeed) of `0x239A` (Adafruit-bootloader); je hoeft niets in te stellen.

Bij een ander device-pad: forceer met `MESHCORE_PORT=/dev/...`.

### 2. Dependencies installeren

```bash
cd <projectmap>
uv sync
```

Of zonder `uv`:

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

### 3. Eerste start

```bash
uv run gateway.py
```

Verwachte output:

```
[*] db: meshcore.db
    path=…/meshcore.db  new=True  schema=fresh  integrity=ok
    0 berichten in archief.
[*] verbind met /dev/cu.usbmodem1101 @ 115200 baud …
[*] verbonden.
[*] self: name='MijnNode' pubkey_prefix=2e400317326b
[*] auto-message-fetcher actief.
[*] decrypt-channel-logs aan (path/RSSI/SNR per bericht)
[*] repeater-cache: N prefixes geladen
[*] web UI op http://127.0.0.1:8080/  (0 user(s) in DB)
    Eerste keer? Open de URL en maak een admin via /setup.
[*] bot-framework actief — definieer bots in Admin → Bots
```

Open `http://127.0.0.1:8080/` in je browser. De allereerste bezoeker komt automatisch op `/setup` om de admin-account aan te maken.

### 4. Op je LAN bereikbaar maken

Standaard luistert de webserver op `127.0.0.1` (alleen lokale toegang). Voor toegang vanaf andere apparaten in je netwerk:

```bash
MESHCORE_WEB_HOST=0.0.0.0 uv run gateway.py
```

Open dan `http://<pi-ip>:8080/` op je telefoon/tablet/laptop.

### 5. Stoppen

`Ctrl-C` in de terminal (of het commando /quit), of in de browser via avatar-menu → "Quit (gateway stoppen)" (admin-only).

---

## Container (Docker / docker-compose)

Het project is gecontaineriseerd voor makkelijke deployment op een Pi. **USB-passthrough werkt alleen op Linux-hosts** (Pi, Linux-PC). macOS/Windows-Docker-desktop hosten geen USB door — daar moet je de gateway native draaien.

### Snel starten met docker-compose

```bash
# Eenmalig: bouw + start
docker compose up -d --build

# Logs volgen
docker compose logs -f

# Stoppen
docker compose down
```

De gateway is dan bereikbaar op `http://<host-ip>:8080/`. De DB staat in `./data/meshcore.db` (host-volume).

### Manual `docker run`

```bash
docker build -t meshcore-gateway .

docker run -d \
  --name meshcore-gateway \
  --restart unless-stopped \
  -p 8080:8080 \
  --device /dev/ttyACM0:/dev/ttyACM0 \
  -v "$(pwd)/data:/data" \
  -e MESHCORE_PORT=/dev/ttyACM0 \
  meshcore-gateway
```

### Container-aandachtspunten

- **USB-device**: pas `/dev/ttyACM0` aan in `docker-compose.yml` (`devices:`-blok) als je companion een ander pad heeft. Check op de host met `ls /dev/ttyACM*` of `lsusb`.
- **Permissions**: de container draait als non-root user `app` (UID 1000) in groep `dialout`. Als je host een andere group-id voor dialout gebruikt, kan een permission-error optreden — fix met `--group-add` op de host-uid van dialout.
- **Persistente data**: alleen het `./data`-volume; bij `docker compose down -v` ben je je DB kwijt. Backup `./data/meshcore.db` regelmatig.
- **Updates**: `docker compose up -d --build` na een code-pull. Schema-migraties draaien automatisch.

### CLI-toegang vanuit een container

In een container is de interactieve CLI van `gateway.py` beperkt bruikbaar — alle admin-functies zitten al in de Web UI. Voor de paar dingen die alleen via CLI gaan:

#### `--reset-admin` (admin-wachtwoord vergeten)

`--reset-admin` doet alleen DB-werk en exit zonder de USB te claimen. Je kan 'm dus naast een draaiende gateway uitvoeren als one-shot:

```bash
docker compose run --rm gateway python gateway.py --reset-admin
```

Hierna kan je in de browser via `/setup` opnieuw een admin aanmaken.

> Let op: dit start een tweede container die hetzelfde DB-volume mount. SQLite met WAL is daar prima tegen bestand voor lees/één-write, maar voor de zekerheid kun je ook eerst de gateway stoppen: `docker compose stop gateway && docker compose run --rm gateway python gateway.py --reset-admin && docker compose start gateway`.

#### Interactieve CLI (zelden nodig)

Als je echt de gateway-CLI live wilt zien (`/info`, `/users`, etc.) en niet alleen de Web UI wilt gebruiken:

1. Stop de service: `docker compose stop gateway`
2. Start handmatig met TTY: `docker compose run --rm --service-ports gateway`
3. Detach (zonder te stoppen) is hier niet nodig; gewoon `Ctrl-C` of `/quit` om te stoppen.

#### `docker logs` voor diagnose

Voor read-only inspectie van wat de gateway doet (incl. `MESHCORE_DEBUG=1` events):

```bash
docker compose logs -f gateway
```

---

## Auto-start als service

### Optie A: via Docker (aanbevolen op een Pi)

`docker-compose.yml` heeft al `restart: unless-stopped`. Bij een host-reboot start de container vanzelf op, mits de Docker daemon zelf ook auto-start:

```bash
sudo systemctl enable docker
sudo systemctl start docker
```

Op Raspberry Pi OS staat dat normaal al aan na `apt install docker.io` of de officiële Docker-install. Verifieer met `systemctl is-enabled docker`.

Eenmaal `docker compose up -d` gestart blijft het draaien tot je expliciet `docker compose down` doet — herstart van Pi inclusief.

### Optie B: native via systemd

Voor wie liever zonder Docker draait. Het project bevat een kant-en-klaar template-bestand `meshcore-gateway.service.example` dat je naar `/etc/systemd/system/` kopieert en aanpast.

#### Stap-voor-stap

1. **User in dialout-groep**: nodig voor toegang tot `/dev/ttyACM*`:
   ```bash
   sudo usermod -aG dialout $USER
   ```
   Log opnieuw in (of `newgrp dialout`) zodat de groepswijziging actief wordt.

2. **Kopieer en pas het template aan**:
   ```bash
   sudo cp meshcore-gateway.service.example /etc/systemd/system/meshcore-gateway.service
   sudo nano /etc/systemd/system/meshcore-gateway.service
   ```
   Pas in elk geval aan:
   - `User=` — de Linux-user waarmee de gateway moet draaien
   - `WorkingDirectory=` — pad naar je project-folder
   - `ExecStart=` — controleer het pad waar `uv` staat (`which uv`); op een fresh Pi is dat vaak `/home/<user>/.local/bin/uv`
   - `Environment="MESHCORE_DB=..."` — pad naar `data/meshcore.db`
   - `Environment="MESHCORE_WEB_HOST=..."` — `127.0.0.1` voor alleen-localhost, `0.0.0.0` of een vast LAN-IP voor andere apparaten

3. **Maak de data-folder aan** (eenmalig, zodat de service erin kan schrijven):
   ```bash
   mkdir -p data
   ```

4. **Activeer en start**:
   ```bash
   sudo systemctl daemon-reload
   sudo systemctl enable --now meshcore-gateway
   ```

5. **Verifieer**:
   ```bash
   sudo systemctl status meshcore-gateway
   sudo journalctl -u meshcore-gateway -f
   ```
   In de log moet je `[*] geen TTY beschikbaar — CLI uitgeschakeld (headless modus)` zien (gevolgd door normale opstartmeldingen) — dat is correct gedrag onder systemd.

#### Beheer-commando's

```bash
sudo systemctl stop meshcore-gateway       # stoppen
sudo systemctl start meshcore-gateway      # starten
sudo systemctl restart meshcore-gateway    # herstart na config-wijziging
sudo systemctl disable meshcore-gateway    # uit auto-start halen
sudo journalctl -u meshcore-gateway -f     # live logs
sudo journalctl -u meshcore-gateway -n 200 # laatste 200 regels
```

#### Wijzigingen in de unit-file

Na een edit van `/etc/systemd/system/meshcore-gateway.service` is een `daemon-reload` + `restart` nodig:

```bash
sudo systemctl daemon-reload
sudo systemctl restart meshcore-gateway
```

#### Admin-wachtwoord vergeten (systemd-versie)

Stop de service, run `--reset-admin` als de juiste user, herstart:

```bash
sudo systemctl stop meshcore-gateway
cd /home/<user>/meshcore-gateway   # jouw projectpad
uv run gateway.py --reset-admin
sudo systemctl start meshcore-gateway
```

Ga vervolgens naar de Web UI → `/setup` voor een nieuwe admin.

### Welke kies ik?

- **Docker** — als je al containers gebruikt op de Pi, of meerdere apps wilt isoleren. Eenvoudigste updates: `docker compose pull && docker compose up -d`.
- **systemd** — als je een minimaal-overhead Pi wilt en alleen deze ene app draait. Iets sneller bij start, geen Docker-laag.

Beide overleven host-reboots zonder ingrijpen.

---

## Command-line parameters & environment-variabelen

### CLI-flags

| Flag             | Effect                                                                  |
|------------------|-------------------------------------------------------------------------|
| `--reset-admin`  | Wist alle admin-accounts vóór start. Bij eerste login wordt `/setup` opnieuw doorlopen. Handig als je je admin-wachtwoord vergeten bent. |

```bash
uv run gateway.py --reset-admin
```

### Environment-variabelen

| Variabele                | Default       | Wat het doet                                                                  |
|--------------------------|---------------|-------------------------------------------------------------------------------|
| `MESHCORE_PORT`          | auto-detect   | Forceer serieel-pad, bv. `/dev/ttyACM0`.                                      |
| `MESHCORE_BAUD`          | `115200`      | Baud-rate voor de USB-serial-link.                                            |
| `MESHCORE_DB`            | `meshcore.db` | Pad naar het SQLite-bestand.                                                  |
| `MESHCORE_WEB`           | `1`           | Webserver aan/uit. Zet op `0`/`false` om alleen CLI te draaien.               |
| `MESHCORE_WEB_HOST`      | `127.0.0.1`   | Bind-host. `0.0.0.0` voor LAN-toegang.                                        |
| `MESHCORE_WEB_PORT`      | `8080`        | TCP-poort van de webserver.                                                   |
| `MESHCORE_DEBUG`         | leeg          | Op `1` zetten om alle binnenkomende meshcore-events naar de console te loggen.|
|                          |               | Handig voor diagnose, vooral van `RX_LOG_DATA`.                               |

Voorbeeld voor productie-gebruik op een Pi:

```bash
MESHCORE_WEB_HOST=0.0.0.0 \
MESHCORE_DB=/var/lib/meshcore/gateway.db \
uv run gateway.py
```

### CLI-commando's tijdens runtime

De CLI-prompt is bewust minimaal — alle admin/channels/housekeeping zit in de Web UI:

```
<tekst>                  stuur naar Public channel
/dm <prefix> <tekst>     directe boodschap (pubkey-prefix, hex)
/history [n]             laatste n berichten van Public
/info                    node-info
/bat                     batterijstatus
/poll                    handmatig msgs ophalen
/users                   toon web-gebruikers
/reset-admin             wis admin-accounts (eerste /setup opnieuw)
/help                    deze help
/quit                    stoppen
```

---

## First login

### Admin (eerste setup)

Bij de allereerste opstart is de DB leeg. Iedere bezoeker van `/` of `/login` wordt geredirect naar `/setup`:

1. Vul een **admin-gebruikersnaam** in (bv. `david`).
2. Vul een **wachtwoord** in (min. 6 tekens), tweemaal ter bevestiging.
3. Klik **Aanmaken** — je bent direct ingelogd als admin.

Als je je admin-wachtwoord vergeten bent: stop de gateway, herstart met `--reset-admin`, en doorloop `/setup` opnieuw. Bestaande chat-historie en kanalen blijven intact.

### Gewone user

Een gewone gebruiker wordt door de admin aangemaakt:

1. Admin opent **Admin → Gebruikers**.
2. Vult een gebruikersnaam in, kiest rol `user` of `admin`, en geeft een **tijdelijk wachtwoord** op.
3. De gebruiker logt voor het eerst in op `/login` met die gebruikersnaam + tijdelijk wachtwoord.
4. Direct na inlog vraagt de UI (verplicht) om een nieuw eigen wachtwoord (minimaal 6 tekens).
5. Daarna is het account normaal actief.

Een user kan **niets in het admin-menu** zien (de hele Admin-tak in de tree is verborgen, inclusief Repeaters). De Quit-knop in het avatar-menu is ook admin-only. Wel toegankelijk: Chat, DM, Rapportages → Overzicht.

### Wachtwoord wijzigen

Iedere gebruiker kan het eigen wachtwoord wijzigen via het avatar-icoon rechtsboven → **Wachtwoord wijzigen**. Drie prompts: huidig, nieuw, nieuw nogmaals.

### Reset wachtwoord (admin)

Admin kan in **Admin → Gebruikers** op `reset pw` klikken bij een user, een nieuw tijdelijk wachtwoord opgeven, waarna die user bij volgende login weer een nieuw eigen wachtwoord moet kiezen.

### Callsign per user

Bij meerdere mensen die vanaf dezelfde gateway/companion uitzenden ziet de ontvangende kant alleen de node-naam — niet wie van de web-users het bericht heeft gestuurd. Met een **callsign** voeg je een kort prefix toe aan elk uitgaand bericht.

- Klik op het avatar-icoon → **Callsign instellen…**
- Vul 1-3 tekens of emoji in (bv. `DMH`, `PA3`, `🚀✨`). Leeg betekent uit.
- Bij verzenden wordt de tekst geprefixt: `[DMH] hallo allemaal`.

Per-user, self-serve — admins hoeven niets te beheren. De callsign wordt opgeslagen in de DB; bestaande sessies worden direct geüpdate zonder her-login.

---

## Channels

Drie types op de companion (in de **Channels**-admin-pagina te beheren):

- **Public** (slot 0, vast): de standaard publieke channel. Niet te wijzigen of verwijderen.
- **Hashtag** (slots 1-7): een gedeelde "thema"-channel. De key is `sha256("#naam")[:16]`. Iedereen die hetzelfde slot configureert met hetzelfde `#naam` krijgt automatisch dezelfde key — geen sleutel-uitwisseling nodig. In de UI altijd met `#`-prefix.
- **Private** (slots 1-7): unieke 128-bit AES-key per channel. Bij toevoegen wordt de key automatisch gegenereerd; de hex-versie wordt eenmalig getoond zodat je 'm aan andere leden kan delen.

In de tree onder **Chat** staan alle slots als klikbare items. Naast de admin-route kun je hashtag-channels ook snel toevoegen via de `+ hashtag` snelkoppeling in de tree zelf.

Per kanaal kan optioneel een **flood-scope** worden ingesteld (bv. `#europa`) — vóór elke send naar dat kanaal wordt `set_flood_scope()` op de companion aangeroepen.

---

## Threading

Wanneer een gesprek bestaat uit meerdere berichten over hetzelfde onderwerp, helpt threading om die in één blok te zien.

**Hoe werkt 't:**
- Bij een bericht waarop replies bestaan verschijnt tussen tijd en afzender een `💬N`-badge (N = aantal replies).
- Klik op de badge → de chat filtert naar root + alle descendants. Boven het log verschijnt een gele banner met snippet en "← terug naar alle".
- Buiten thread-modus blijft de chat gewoon chronologisch.

**Hoe worden replies gedetecteerd (hybride):**
- **Expliciet:** klik op een bericht → in het detail-paneel "Reply" — vult `@[Afzender]` in de input én markeert intern dat de volgende send een reply is op die msg (`parent_id` in DB).
- **Heuristisch:** als een ander bericht begint met `@[X]` en X heeft binnen 30 min een eigen bericht in hetzelfde kanaal verstuurd, wordt 't automatisch als reply op dat msg behandeld. Geen DB-persistentie nodig — werkt ook bij berichten van non-web-clients.

**Toggle uitzetten:** avatar-menu → "Threading-indicators: aan/uit". Per browser opgeslagen in localStorage. Uit = geen badges, geen klikbare filter.

**Wat onthouden blijft over restarts:** alleen expliciete Reply-relaties (via DB-veld `parent_id`). Mention-heuristiek wordt elke render opnieuw berekend.

---

## Bots

In **Admin → Bots** definieer je per bot:

- **Naam** + optionele beschrijving
- **Kanaal** waar de bot luistert
- **Keyword** (zonder `?`-prefix)
- **Reply-template** met optionele variabelen

Beschikbare variabelen in de reply:

| Variabele   | Waarde                                                    |
|-------------|-----------------------------------------------------------|
| `{TIME}`    | Lokale tijd op de gateway (`HH:MM:SS`)                    |
| `{UPNODE}`  | Uptime van de gateway-applicatie                          |
| `{UPRADIO}` | Uptime van de aangesloten companion-radio                 |
| `{HELP}`    | Komma-lijst van alle variabele-namen                      |

**Belangrijke regel**: een bot reageert alléén als de afzender de bot expliciet `@`-mentioned (`@[<naam-van-deze-node>]` of `@[<self-pubkey-prefix>]`) **én** het `?keyword` gebruikt. Voorbeeld bericht dat een bot triggert op het keyword ?tijd:

> `Hé @[MijnNode] wat is de ?tijd?`

Een bot reageert dus niet op willekeurig `?`-verkeer.

---

## Notificaties (mentions)

Berichten waarin jouw node-naam of pubkey-prefix ge-`@`-ed wordt:

- Krijgen een **gele highlight** in de chat-list
- Spelen een korte **dubbele beep** af via Web Audio
- Tonen een **gele toast** rechtsonder met afzender + bericht
- Als de browser-tab op de **achtergrond** staat: een **systeem-notificatie** (vereist eenmalige permissie-prompt)

---

## Belangrijke aandachtspunten / caveats

### Contactpersonen voor DM's

DM's vereisen dat de **companion-firmware** de bestemming kent (om een routing-pad te hebben). Onze per-user **Contactpersonen**-lijst is alleen een lokaal label — de companion krijgt geen extra info als je daar handmatig een pubkey toevoegt.

In de Contactpersonen-tabel zie je per item:

- **`✓`** = bekend bij companion → DM werkt
- **`⚠`** = alleen lokaal opgeslagen → DM faalt met "not found"

Een onbekende contact wordt vanzelf bekend zodra de companion een **advert** van die node ontvangt. Vanaf dat moment werkt DM. Zet eventueel `Auto-add adverts` aan in **Admin → Voorkeuren** zodat álle adverts automatisch worden opgeslagen.

### Hashtag-channels

Het `#` is functioneel, niet decoratief: de key wordt namelijk afgeleid van de naam **inclusief** `#`. Dus `#weer` en `weer` geven verschillende keys. De UI prepend automatisch `#` als 't ontbreekt.

### Path / RSSI / SNR per bericht

De companion-firmware levert deze info via een aparte `RX_LOG_DATA`-stream die na decryptie aan het bericht wordt gekoppeld. Daarvoor:

1. Moet de companion de channel-key kennen (vanzelfsprekend).
2. Moet `set_decrypt_channel_logs(True)` aan staan (gebeurt automatisch bij start).

Als één van die voorwaarden mist (bv. een onbekend hashtag-channel waar je nooit `set_channel`-aanroep voor hebt gedaan): dan zijn de RSSI/SNR/path-velden leeg en toont de Pad-sectie `geen path-info in payload`.

**RSSI**: in sommige firmware-versies komt RSSI niet door op channel-msg-events, alleen SNR. De UI toont alleen de waarden die echt aanwezig zijn.

### Multi-path "Heard X Times"

De gateway koppelt zelf alle `RX_LOG_DATA`-instances op `pkt_hash` om alle ontvangen paden van hetzelfde bericht te clusteren. De **kortste route** wordt standaard uitgeklapt; andere routes zijn klikbaar. Tijd-correlatie is binnen 15s; bij heel druk verkeer kan af en toe een verkeerd pad mee komen.

### Ack-tracking

- **DM**: gebruikt het echte protocol-ack-mechanisme. `✓` = verzonden, `✓✓` = bevestigd, vinkjes worden live geüpdatet via WebSocket.
- **Channel-msg**: heeft géén protocol-ack (flood-broadcast). De gateway detecteert wel **implicit-ack** via `RX_LOG_DATA`: als een repeater jouw bericht herhaalt, krijgt 'ie status `↻` (gerelayed door mesh).

### Ack-rate in Rapportages

Telt alleen DM's mee — channels hebben geen ack-mechanisme. De percentage gaat over outgoing DM's binnen de gekozen periode.

### Repeaters

Sinds v1.1.030 staat het Repeaters-paneel onder **Admin → Repeaters** (was eerder Rapportages → Repeaters). Niet-admins zien dit paneel niet meer; de bijbehorende endpoints zijn ook admin-only. Repeater-favorieten zijn nog steeds per-user opgeslagen — maar omdat alleen admins toegang hebben heeft elke admin zijn eigen favorieten-set.

### Repeater-namen in pad-visualisatie

De `[hex]`-pillen in de Pad-sectie krijgen een naam-pil (`[NL-020-Involver/RPT2]`) zodra de companion een advert van die repeater heeft gehoord en hem als contact heeft opgeslagen. Bij `Auto-add adverts` aan vult dat zich vanzelf na een uur of wat draaien.

De mapping is `pubkey[:hash_size_bytes]`. Bij `path_hash_size = 1` (default) is een 1-byte prefix soms ambigu: twee repeaters kunnen toevallig dezelfde 1e byte hebben. In dat geval wint de laatst-geladen.

### Sessies & restart

Web-sessies worden in-memory opgeslagen. Bij gateway-herstart moet iedereen opnieuw inloggen. Acceptabel voor home-gebruik.

### Database & housekeeping

- SQLite met WAL-mode (betere crash-bestendigheid).
- Schema-migraties draaien automatisch bij start (zie de `schema=migrated:X->Y`-regel in de log).
- Geen automatische cleanup — gebruik **Admin → Housekeeping** om oudere berichten te verwijderen of de DB te compacteren (`VACUUM`).

### Fysieke radio-instellingen

`Admin → Radio` past parameters live aan, maar de companion vereist een **reboot** voor sommige settings (`set_radio`, `set_tx_power`). De UI toont dat in de toast en je vindt de Reboot-knop onder **Admin → Node**.

### Watchdog

Achter de schermen pingt de gateway elke 60 seconden de companion. Drie missers achter elkaar → waarschuwing in de CLI (`[watchdog] companion reageert 3x niet`). Geen automatische herstart — als de USB-link weg is, herstart je de gateway zelf.

---

## Troubleshooting

| Symptoom                                         | Mogelijke oorzaak / fix                                                              |
|--------------------------------------------------|--------------------------------------------------------------------------------------|
| `kon geen MeshCore-device vinden via USB`        | USB-kabel/recovery-modus, of forceer `MESHCORE_PORT=/dev/...`                        |
| `Permission denied op /dev/ttyACM0`              | `sudo usermod -aG dialout $USER`, daarna opnieuw inloggen                            |
| `handshake mislukt: ... is dit wel companion firmware?` | Verkeerde firmware-build geflashed (repeater/room ipv companion)                |
| Berichten komen niet binnen op een channel       | Channel-key matcht niet met andere nodes; check via Admin → Channels                  |
| DM faalt met `not found`-toast                   | Companion kent contact niet — wacht op advert of zet Auto-add aan                    |
| Geen RSSI in detail-pane                         | Firmware geeft RSSI niet altijd op channel-msg-events; SNR + hops blijven werken     |
| Bot reageert niet                                | Bot reageert alleen op `@[<naam>] ?keyword`; niet op kale `?keyword`                  |
| Web UI werkt na update niet meer                 | Browser-cache; hard refresh (Cmd-Shift-R / Ctrl-Shift-R). Cache-buster `?v=...` voorkomt dit meestal sinds v1.1.032 |
| Knop in avatar-menu doet niets na update         | Static JS niet meegekopieerd. Controleer of `static/js/*.js` op de Pi compleet is, hard-refresh browser |
| Chat-input valt onder Chrome URL-bar op mobiel   | Update naar ≥v1.1.029 (gebruikt `100dvh` ipv `100vh`)                                  |
| Migratie-fout bij start na update                | Backup DB (kopieer `meshcore.db` weg) en check logs — schema-migraties zijn additief, bij ALTER-fout meestal corruptie in oude data |

Voor diepere diagnose: start met `MESHCORE_DEBUG=1` om alle inkomende meshcore-events te zien.

---

## Updates / nieuwe versie deployen

De gateway wordt versiebeheerd via `APP_VERSION` in `web.py` (`x.y.z`-formaat, zichtbaar onderin de tree). Static assets (`/static/app.css`, `/static/js/*.js`) hebben sinds v1.1.032 een `?v={{VERSION}}`-query — een versie-bump invalideert daarmee automatisch browser-cache.

### Standaard update-procedure

**1. Backup de database** (altijd, ook bij ogenschijnlijk onschuldige updates):

```bash
# Docker
cp ./data/meshcore.db ./data/meshcore.db.bak-$(date +%Y%m%d)

# Native (pas pad aan):
cp /var/lib/meshcore/gateway.db /var/lib/meshcore/gateway.db.bak-$(date +%Y%m%d)
```

Schema-migraties zijn **additief** (alleen `ALTER TABLE ADD COLUMN`/`CREATE INDEX`) en bewaren bestaande data, maar een backup kost niets en geeft een terugval-punt bij een onverwacht probleem.

**2. Pull / kopieer de nieuwe code.**
- Docker: `git pull` + `docker compose up -d --build`
- Native: `git pull` + `sudo systemctl restart meshcore-gateway`
- Handmatige bestand-voor-bestand kopie naar Pi (bv. `scp`): zorg dat álle gewijzigde bestanden mee gaan (Python én static), anders ontstaan vreemde gedragingen.

**3. Schema-migratie loopt automatisch bij start.** In de log zie je:

```
[*] db: ...
    path=... new=False schema=migrated:X->Y integrity=ok
```

Bij `schema=ok` was er niks te migreren. Bij `migrated:X->Y` is een nieuwe versie gedraaid; bestaande tabellen kregen nieuwe kolommen met default-waarden.

**4. Hard refresh in de browser**: meestal niet meer nodig dankzij de `?v=`-cache-buster, maar bij twijfel doe `Ctrl-Shift-R` (of `Cmd-Shift-R`).

### Terugrollen bij problemen

```bash
# 1. Stop de gateway
sudo systemctl stop meshcore-gateway        # of: docker compose down

# 2. Code terug naar vorige versie
git checkout <vorige-tag-of-hash>

# 3. DB-backup terugzetten (alleen nodig als migratie schade heeft gedaan)
cp ./data/meshcore.db.bak-YYYYMMDD ./data/meshcore.db

# 4. Start opnieuw
sudo systemctl start meshcore-gateway       # of: docker compose up -d --build
```

> De DB-backup terugzetten is alleen nodig als de migratie iets onverwachts heeft gedaan. Normaal kun je code terugdraaien zonder de DB aan te raken — eerdere versies negeren simpelweg de nieuwe kolommen.

### Wat raken specifieke versies?

Globale handleidingen bij grotere wijzigingen, zie `HANDOFF.md` voor de volledige changelog:

| Versie  | Schema | Belangrijkste wijziging                                         |
|---------|--------|-----------------------------------------------------------------|
| 1.1.031 | 12→13  | Nieuwe kolom `users.callsign`                                   |
| 1.1.033 | 13→14  | Nieuwe kolom `messages.parent_id` + index                       |

---

## Bestandsstructuur

```
WebClient/
  gateway.py                          — entry-point: connectie + CLI + dispatch + webserver-startup
  web.py                              — FastAPI + Socket.IO + alle web-endpoints (renders templates/index.html)
  bot.py                              — bot-framework (DB-driven, variable-templates)
  db.py                               — SQLAlchemy async + alle modellen + auto-migraties
  templates/
    index.html                        — Jinja2-template voor de chat-UI (header, tree, main, detail)
  static/
    app.css                           — alle styling + 3 mobile-breakpoints
    js/
      01-core.js                      — STATE + helpers + threading-tree-bouwer
      02-tree.js                      — tree-rendering + view-switching (chat/admin/reports/contacts)
      03-chat.js                      — chat-render + socketio + emoji-picker + thread-badges
      04-admin.js                     — alle admin-views (radio/node/prefs/channels/contacten/bots/...)
      05-reports.js                   — rapportages + repeater-management
      06-detail.js                    — detail-paneel + path-visualisatie + Reply-flow
      07-bootstrap.js                 — refresh-loops + auth/account + native notifs + threading-toggle
  meshcore.db                         — SQLite-database (auto-aangemaakt; in container: /data/meshcore.db)
  pyproject.toml                      — dependencies (Python 3.12+)
  requirements.txt                    — pinned deps voor pip / Docker-build
  Dockerfile                          — container-image (python:3.12-slim base)
  docker-compose.yml                  — orchestratie met USB-device + volume + port
  .dockerignore                       — uitsluitingen voor docker-build context
  meshcore-gateway.service.example    — voorbeeld-systemd-unit (kopieer + aanpassen)
  HANDOFF.md                          — referentie-doc: architectuur, DB-schema, voltooide features, caveats, backlog
  CHANGELOG.md                        — per-versie wijzigingen, chronologisch (v1.1.018 → huidig)
  README.md                           — dit bestand
```

> **`LOGIN_HTML` / `SETUP_HTML`** zijn (bewust) inline strings in `web.py` — die pages zijn klein en hoeven geen template engine. De main chat-UI gebruikt wél Jinja2 + losse static-files. Wijzigingen aan de chat-UI gaan dus in `templates/index.html`, `static/app.css` en/of `static/js/*.js`.

---

&copy; Flight 815 B.V.
