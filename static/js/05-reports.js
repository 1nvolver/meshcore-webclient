
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
    return '<tr><td data-label="Kanaal">'+escapeHTML(naam)+'</td><td data-label="Aantal" style="text-align:right">'+c.count+'</td></tr>';
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

  STATE.repeaterRows = data.repeaters || [];
  const q = (STATE.repeaterSearch || '').toLowerCase();
  el.innerHTML = `
    <section><h2>Repeaters &amp; Rooms (${data.count})</h2>
      <div class="note" style="margin-bottom:8px">Bron: contactenlijst van de companion (alle nodes met type repeater of room-server). Klik een kolomkop om te sorteren — favorieten blijven altijd bovenaan.</div>
      <div class="row" style="margin-bottom:8px">
        <input id="rep-search" type="text" placeholder="zoek op naam, pubkey of hash…" style="flex:1"
               value="${escapeHTML(STATE.repeaterSearch || '')}"
               oninput="filterRepeaterTable(this.value)">
      </div>
      <table style="width:100%">
        <thead><tr>
          <th style="width:24px" title="favoriet">★</th>
          ${_repSortTh('name','Naam')}${_repSortTh('type','Type')}${_repSortTh('hash','Hash')}${_repSortTh('pubkey','Pubkey-prefix')}${_repSortTh('last_advert','Laatste advert')}${_repSortTh('loc','Locatie')}${_repSortTh('path','Path')}
          <th>Ping</th>
        </tr></thead>
        <tbody id="rep-tbody"></tbody>
      </table>
    </section>`;
  renderRepeaterRows();
  // Focus terugzetten als er een zoekterm staat, zodat typen niet onderbroken wordt
  const inp = $('rep-search');
  if (inp && q) { inp.focus(); inp.setSelectionRange(q.length, q.length); }
}

function filterRepeaterTable(q){
  STATE.repeaterSearch = (q || '').toLowerCase();
  renderRepeaterRows();
}

/* ============== Sorteren (v1.1.050) ==============
   Klikbare kolomkoppen. Favorieten staan ALTIJD bovenaan, ongeacht de
   sortering — de sortering wordt binnen de twee groepen (fav / niet-fav)
   toegepast. Dat is bewust: de ster is een pin, geen sorteerkolom.
   Default blijft de server-volgorde (fav, type, naam); pas bij de eerste
   klik op een kop gaat STATE.repeaterSort meedoen. */

// Kolom-key → waarde waarop gesorteerd wordt. Null/undefined sorteert altijd
// achteraan, ongeacht de richting (anders vullen lege cellen de bovenkant).
const REP_SORT_ACCESSORS = {
  name:        r => (r.name || '').toLowerCase(),
  type:        r => (r.type_label || '').toLowerCase(),
  hash:        r => (r.hash_1b || '').toLowerCase(),
  pubkey:      r => (r.pubkey_prefix || '').toLowerCase(),
  last_advert: r => (typeof r.last_advert === 'number' ? r.last_advert : null),
  loc:         r => ((typeof r.lat === 'number' && typeof r.lon === 'number' && (r.lat || r.lon)) ? r.lat : null),
  // 'flood' (-1 / 255) is geen hop-count; sorteer 'm als hoogste waarde.
  path:        r => {
    const v = r.out_path_len;
    if (v === -1 || v === 255) return 999;
    return (typeof v === 'number') ? v : null;
  },
};

function _repSortTh(key, label){
  const srt = STATE.repeaterSort;
  const active = srt && srt.key === key;
  const arrow = active ? (srt.dir === 'asc' ? ' ▲' : ' ▼') : '';
  const style = 'cursor:pointer;user-select:none' + (active ? ';color:#1a6fc4' : '');
  return '<th style="' + style + '" title="sorteer op ' + label + '" ' +
         'onclick="sortRepeaterTable(\'' + key + '\')">' + label + arrow + '</th>';
}

function sortRepeaterTable(key){
  if (!REP_SORT_ACCESSORS[key]) return;
  const srt = STATE.repeaterSort;
  if (srt && srt.key === key) {
    // zelfde kolom: asc → desc → uit (terug naar server-volgorde)
    if (srt.dir === 'asc') STATE.repeaterSort = {key: key, dir: 'desc'};
    else                   STATE.repeaterSort = null;
  } else {
    STATE.repeaterSort = {key: key, dir: 'asc'};
  }
  _repUpdateSortHeaders();
  renderRepeaterRows();
}

// Alleen de <th>'s hertekenen, zodat de zoek-input z'n focus/cursor houdt.
function _repUpdateSortHeaders(){
  const tbody = $('rep-tbody');
  if (!tbody) return;
  const thead = tbody.parentElement && tbody.parentElement.querySelector('thead tr');
  if (!thead) return;
  const cols = [['name','Naam'],['type','Type'],['hash','Hash'],['pubkey','Pubkey-prefix'],
                ['last_advert','Laatste advert'],['loc','Locatie'],['path','Path']];
  thead.innerHTML = '<th style="width:24px" title="favoriet">★</th>' +
                    cols.map(c => _repSortTh(c[0], c[1])).join('') +
                    '<th>Ping</th>';
}

function _repSortedRows(list){
  const srt = STATE.repeaterSort;
  if (!srt) return list;
  const acc = REP_SORT_ACCESSORS[srt.key];
  if (!acc) return list;
  const mul = (srt.dir === 'desc') ? -1 : 1;
  // slice(): niet in-place, anders raakt de server-volgorde onherstelbaar kwijt
  return list.slice().sort((a, b) => {
    // Favorieten eerst — dit wint altijd van de gekozen sortering.
    const fa = a.is_favorite ? 0 : 1, fb = b.is_favorite ? 0 : 1;
    if (fa !== fb) return fa - fb;
    const va = acc(a), vb = acc(b);
    const na = (va === null || va === undefined), nb = (vb === null || vb === undefined);
    if (na && nb) return 0;
    if (na) return 1;          // lege waarden altijd onderaan
    if (nb) return -1;
    if (typeof va === 'number' && typeof vb === 'number') return (va - vb) * mul;
    return String(va).localeCompare(String(vb)) * mul;
  });
}

function renderRepeaterRows(){
  const tbody = $('rep-tbody');
  if (!tbody) return;
  const q = (STATE.repeaterSearch || '').toLowerCase();
  const list = _repSortedRows((STATE.repeaterRows || []).filter(r => {
    if (!q) return true;
    const name = (r.name || '').toLowerCase();
    const pk   = (r.pubkey || '').toLowerCase();
    const pref = (r.pubkey_prefix || '').toLowerCase();
    const h1   = (r.hash_1b || '').toLowerCase();
    const h2   = (r.hash_2b || '').toLowerCase();
    return name.includes(q) || pk.includes(q) || pref.includes(q) || h1.includes(q) || h2.includes(q);
  }));
  const rows = list.map(r => {
    const lastAdv = r.last_advert
      ? new Date(r.last_advert * 1000).toLocaleString()
      : '—';
    const loc = (typeof r.lat === 'number' && typeof r.lon === 'number' && (r.lat || r.lon))
      ? r.lat.toFixed(4) + ', ' + r.lon.toFixed(4)
      : '—';
    const hashCell = '<code style="background:#dfeefd;padding:1px 4px;border-radius:3px">'+r.hash_1b+'</code>';
    const opl = (r.out_path_len === -1 || r.out_path_len === 255) ? 'flood' : (r.out_path_len ?? '—');
    const starChar = r.is_favorite ? '★' : '☆';
    const starTitle = r.is_favorite ? 'verwijder uit favorieten' : 'markeer als favoriet';
    const starColor = r.is_favorite ? '#e9a300' : '#bbb';
    const star = '<span class="fav-star" style="cursor:pointer;font-size:16px;color:'+starColor+'" '+
                 'title="'+starTitle+'" onclick="toggleRepeaterFav(\''+r.pubkey+'\','+(r.is_favorite?'true':'false')+')">'+
                 starChar+'</span>';
    const pingCellId = 'ping-cell-' + r.pubkey.slice(0, 12);
    const pingCell = '<button style="padding:2px 8px;font-size:12px" onclick="event.stopPropagation();pingRepeater(\''+r.pubkey+'\')">ping</button> ' +
                     '<span id="'+pingCellId+'" style="font-size:11px;color:#666;margin-left:4px"></span>';
    const isSel = (STATE.selectedRepeater && STATE.selectedRepeater.pubkey === r.pubkey);
    const rowStyle = isSel ? ' style="background:#e8f1ff;cursor:pointer" ' : ' style="cursor:pointer" ';
    const escName = (r.name || '?').replace(/"/g,'&quot;').replace(/\\/g,'\\\\').replace(/'/g,"\\'");
    return '<tr' + rowStyle + 'onclick="selectRepeater(\''+r.pubkey+'\',\''+escName+'\',\''+r.type_label+'\')">' +
      '<td style="text-align:center" onclick="event.stopPropagation()">' + star + '</td>' +
      '<td data-label="Naam">' + escapeHTML(r.name || '?') + '</td>' +
      '<td data-label="Type">' + r.type_label + '</td>' +
      '<td data-label="Hash">' + hashCell + '</td>' +
      '<td data-label="Pubkey-prefix"><code style="font-size:11px">' + r.pubkey_prefix + '</code></td>' +
      '<td data-label="Laatste advert">' + escapeHTML(lastAdv) + '</td>' +
      '<td data-label="Locatie">' + escapeHTML(loc) + '</td>' +
      '<td data-label="Path">' + opl + '</td>' +
      '<td data-label="Ping">' + pingCell + '</td>' +
      '</tr>';
  }).join('');
  const empty = q
    ? '<tr><td colspan="9" style="color:#888">geen repeaters die matchen op &laquo;'+escapeHTML(q)+'&raquo;</td></tr>'
    : '<tr><td colspan="9" style="color:#888">geen bekende repeaters — wacht tot er adverts binnenkomen</td></tr>';
  tbody.innerHTML = rows || empty;
}

async function pingRepeater(pubkey){
  const cellId = 'ping-cell-' + pubkey.slice(0, 12);
  const cell = $(cellId);
  if (cell) cell.innerHTML = '<span style="color:#888">…pinging…</span>';
  let res;
  try {
    res = await api('/admin/repeaters/ping', {method:'POST', body: JSON.stringify({pubkey: pubkey})});
  } catch(e) {
    if (cell) cell.innerHTML = '<span style="color:#c33">✗ fout</span>';
    return;
  }
  if (!cell) return;
  const fmtSnr = v => (typeof v === 'number') ? (v.toFixed(1) + 'dB') : '—';
  if (res.status === 'ok') {
    cell.innerHTML =
      '<span style="color:#28a745">✓</span> ' + res.duration_ms + 'ms ' +
      '<span title="SNR zoals door de remote gerapporteerd (onze packet bij hun)">there:' + fmtSnr(res.snr_there) + '</span> ' +
      '<span title="SNR waarmee onze companion de respons ontving (best-effort)">here:' + fmtSnr(res.snr_here) + '</span>';
  } else if (res.status === 'no_response') {
    cell.innerHTML = '<span style="color:#c33">✗ timeout</span> ' + res.duration_ms + 'ms';
  } else {
    cell.innerHTML = '<span style="color:#c33">✗ ' + escapeHTML(res.error || res.status || 'fout') + '</span>';
  }
}

function selectRepeater(pubkey, name, typeLabel){
  STATE.selectedRepeater = {pubkey: pubkey, name: name, type_label: typeLabel};
  // Reset manage-state per nieuwe selectie
  STATE.repeaterMgmt = {logged_in: false, cli_history: [], last_status: null,
                        last_activity_ms: 0, ttl_secs: 120,
                        has_saved_password: false};
  stopRepeaterCountdown();
  renderRepeaterRows();   // herteken voor de selectie-highlight
  renderDetail();
  openMobileDetail();     // detail-overlay op tablet/mobile
  // Vraag eventuele bestaande sessie-status op (na restart geldt sowieso niets)
  refreshRepeaterSession();
}

async function refreshRepeaterSession(){
  if (!STATE.selectedRepeater) return;
  let s;
  try {
    s = await api('/admin/repeaters/session?pubkey=' + encodeURIComponent(STATE.selectedRepeater.pubkey));
  } catch(e) { return; }
  STATE.repeaterMgmt.logged_in = !!s.logged_in;
  STATE.repeaterMgmt.has_saved_password = !!s.has_saved_password;
  if (typeof s.ttl_secs === 'number') STATE.repeaterMgmt.ttl_secs = s.ttl_secs;
  if (typeof s.last_activity === 'number') {
    STATE.repeaterMgmt.last_activity_ms = s.last_activity * 1000;
  } else if (s.logged_in) {
    // Geen server-tijd ontvangen maar wel ingelogd → gebruik nu als referentie
    STATE.repeaterMgmt.last_activity_ms = Date.now();
  }
  renderDetail();
  if (STATE.repeaterMgmt.logged_in) startRepeaterCountdown();
  else                              stopRepeaterCountdown();
}

/* ============== Sessie-countdown (v1.1.046) ==============
   Client-side TTL-display. Bron-van-waarheid is server-side _REPEATER_SESSIONS
   (zie web.py): elke succesvolle cmd refresht last_activity. Wij kennen via
   GET /session de laatste last_activity + ttl_secs. UI updaten via textContent
   ipv renderDetail() om uncontrolled inputs (wachtwoord, CLI) niet te wissen
   — zelfde principe als de v1.1.044-fix.

   Caveat: countdown is een client-zijde HINT. De firmware-zijde TTL is
   onbekend en kan korter zijn (de UI vangt 'not_logged_in' op en valt
   terug op login). De expliciete "Verleng sessie"-knop stuurt een echte
   CLI (clock) zodat firmware-side TTL óók wordt geraakt. */
let _repeaterCountdownTimer = null;

function startRepeaterCountdown(){
  stopRepeaterCountdown();
  _tickRepeaterCountdown();  // direct render zonder 1s te wachten
  _repeaterCountdownTimer = setInterval(_tickRepeaterCountdown, 1000);
}

function stopRepeaterCountdown(){
  if (_repeaterCountdownTimer) {
    clearInterval(_repeaterCountdownTimer);
    _repeaterCountdownTimer = null;
  }
}

function _tickRepeaterCountdown(){
  const mgmt = STATE.repeaterMgmt || {};
  // Self-cleanup: als de selectie of login weg is, stop het interval.
  if (!STATE.selectedRepeater || !mgmt.logged_in) {
    stopRepeaterCountdown();
    return;
  }
  const el = $('rep-mgmt-countdown');
  if (!el) return;  // paneel niet zichtbaar (bv. user op andere view) — timer
                    // blijft draaien zodat zodra paneel weer in DOM komt, 't
                    // ticket weer werkt. Stop bij echte deselectie hierboven.
  const ttl = (mgmt.ttl_secs || 120) * 1000;
  const since = Date.now() - (mgmt.last_activity_ms || 0);
  const remaining_ms = ttl - since;
  if (remaining_ms <= 0) {
    // TTL op — UI naar logout-state. Eén renderDetail om login-form terug
    // te zetten. Geen toast spam — user ziet 't aan de UI.
    mgmt.logged_in = false;
    stopRepeaterCountdown();
    renderDetail();
    toast('repeater-sessie verlopen', 'err');
    return;
  }
  const s = Math.max(0, Math.ceil(remaining_ms / 1000));
  const mm = Math.floor(s / 60);
  const ss = s % 60;
  el.textContent = (mm > 0 ? mm + 'm ' : '') + ss + 's';
  // Knipper-class onder de 30s
  if (s <= 30) el.classList.add('cd-warn');
  else         el.classList.remove('cd-warn');
}

/* Refresh client-side activity-stempel + DOM. Aangeroepen na elke
   succesvolle CLI-call zodat de countdown reset zonder /session te
   hoeven herfetchen (server doet 't ook server-side). */
function _bumpRepeaterActivity(){
  if (!STATE.repeaterMgmt) return;
  STATE.repeaterMgmt.last_activity_ms = Date.now();
  _tickRepeaterCountdown();
}

async function repeaterKeepalive(){
  // Verleng-knop: stuur een goedkope CLI (clock) zodat zowel onze sessie als
  // (vermoedelijk) de firmware-zijde TTL gereset wordt. Reuse repeaterAction
  // zodat de respons in CLI-history landt — user ziet bevestiging dat het
  // door is.
  if (!STATE.selectedRepeater) return;
  repeaterAction('clock', 'verlengen', null);
}

/* Login. useSaved=true stuurt géén wachtwoord mee; de server pakt dan het
   opgeslagen exemplaar uit de DB. Het wachtwoord komt nooit naar de browser,
   dus de UI weet alleen óf er één ligt (has_saved_password). */
async function repeaterLogin(useSaved){
  if (!STATE.selectedRepeater) return;
  const pwEl = $('rep-mgmt-pw');
  const rememberEl = $('rep-mgmt-remember');
  const pwd = useSaved ? '' : ((pwEl && pwEl.value) || '');
  if (!useSaved && !pwd){ toast('wachtwoord vereist', 'err'); return; }
  const statusEl = $('rep-mgmt-login-status');
  if (statusEl) statusEl.textContent = useSaved ? '…inloggen met opgeslagen wachtwoord…' : '…inloggen…';
  let res;
  try {
    res = await api('/admin/repeaters/login', {method:'POST',
      body: JSON.stringify({
        pubkey: STATE.selectedRepeater.pubkey,
        password: pwd,
        remember: !useSaved && !!(rememberEl && rememberEl.checked),
      })});
  } catch(e) { return; }
  if (res.ok){
    STATE.repeaterMgmt.logged_in = true;
    STATE.repeaterMgmt.last_activity_ms = Date.now();
    if (typeof res.has_saved_password === 'boolean') {
      STATE.repeaterMgmt.has_saved_password = res.has_saved_password;
    }
    if (pwEl) pwEl.value = '';
    toast(res.message || 'ingelogd');
    renderDetail();
    startRepeaterCountdown();
    // v1.1.050: status direct ophalen — je wilt na het verbinden meteen
    // batterij/uptime/klok zien zonder een extra klik.
    repeaterRequestStatus();
  } else {
    STATE.repeaterMgmt.logged_in = false;
    stopRepeaterCountdown();
    renderDetail();
    const el2 = $('rep-mgmt-login-status');
    if (el2) el2.textContent = res.message || res.status || 'mislukt';
  }
}

async function forgetRepeaterPassword(){
  if (!STATE.selectedRepeater) return;
  if (!confirm('Opgeslagen wachtwoord voor deze repeater verwijderen?')) return;
  try {
    await api('/admin/repeaters/forget-password', {method:'POST',
      body: JSON.stringify({pubkey: STATE.selectedRepeater.pubkey})});
  } catch(e) { return; }
  STATE.repeaterMgmt.has_saved_password = false;
  toast('opgeslagen wachtwoord verwijderd');
  renderDetail();
}

async function repeaterLogout(){
  if (!STATE.selectedRepeater) return;
  try {
    await api('/admin/repeaters/logout', {method:'POST',
      body: JSON.stringify({pubkey: STATE.selectedRepeater.pubkey})});
  } catch(e) { /* hard close lokaal */ }
  STATE.repeaterMgmt.logged_in = false;
  STATE.repeaterMgmt.last_status = null;
  stopRepeaterCountdown();
  renderDetail();
}

function _fmtUptime(secs){
  if (typeof secs !== 'number' || secs < 0) return '—';
  const d = Math.floor(secs / 86400);
  const h = Math.floor((secs % 86400) / 3600);
  const m = Math.floor((secs % 3600) / 60);
  if (d > 0) return d + 'd ' + h + 'h ' + m + 'm';
  if (h > 0) return h + 'h ' + m + 'm';
  return m + 'm';
}

async function repeaterRequestStatus(){
  if (!STATE.selectedRepeater) return;
  $('rep-mgmt-status-box').innerHTML = '<div class="kv">…opvragen…</div>';
  let res;
  try {
    res = await api('/admin/repeaters/manage_status', {method:'POST',
      body: JSON.stringify({pubkey: STATE.selectedRepeater.pubkey})});
  } catch(e) { return; }
  STATE.repeaterMgmt.last_status = res;
  renderDetail();
}

/* ============== CLI met auto-retry (v1.1.050) ==============
   Repeater-commando's gaan over de mesh: een uitblijvend antwoord is vaker
   een gemiste pakket dan een echte fout. Daarom 3 pogingen (origineel +
   retry 1 + retry 2) met een korte pauze ertussen.

   NIET geretried:
     - not_logged_in  → sessie verlopen; opnieuw sturen helpt niet en kost
                        alleen USB-traffic. UI valt terug op het login-form.
     - HTTP-fouten uit api() (4xx/5xx) → dat is een serverfout, geen RF-probleem.
   Wél geretried: timeout en andere 'ok:false'-statussen van de mesh-kant.

   De history-regel wordt live bijgewerkt zodat de user ziet wat er gebeurt:
   '…wachten…' → 'retry 1/2…' → 'retry 2/2…' → uiteindelijke respons of
   'failed na 3 pogingen'. */
const REP_CMD_MAX_ATTEMPTS = 3;      // origineel + 2 retries
const REP_CMD_RETRY_DELAY_MS = 1500; // ademruimte voor de mesh

function _repSleep(ms){ return new Promise(res => setTimeout(res, ms)); }

// Commando's die je niet per ongeluk twee keer wilt uitvoeren. Een timeout
// betekent "geen antwoord", niet "niet aangekomen" — de repeater kan 'm wel
// degelijk hebben uitgevoerd. Voor 'clock'/'get …'/'advert' is dubbel
// onschadelijk, voor een reboot niet.
const REP_CMD_NO_RETRY = /^\s*reboot\b/i;

async function _repeaterCmdWithRetry(cmd, hist){
  // hist = het history-object dat al in cli_history zit; wordt in-place
  // bijgewerkt zodat renderDetail() de voortgang toont.
  const maxAttempts = REP_CMD_NO_RETRY.test(cmd) ? 1 : REP_CMD_MAX_ATTEMPTS;
  let last = null;
  for (let attempt = 1; attempt <= maxAttempts; attempt++){
    if (attempt > 1) {
      hist.response = 'retry ' + (attempt - 1) + '/' + (maxAttempts - 1) + '…';
      hist.ok = null;
      renderDetail();
      await _repSleep(REP_CMD_RETRY_DELAY_MS);
    }
    let res;
    try {
      res = await api('/admin/repeaters/cmd', {method:'POST',
        body: JSON.stringify({pubkey: STATE.selectedRepeater.pubkey, cmd: cmd})});
    } catch(e) {
      // HTTP-fout: niet retryen, dit is geen RF-probleem.
      return {ok: false, status: 'http_error', error: '(fout)', attempts: attempt, fatal: true};
    }
    if (res.ok) return Object.assign({}, res, {attempts: attempt});
    if (res.status === 'not_logged_in') {
      return Object.assign({}, res, {attempts: attempt, fatal: true});
    }
    last = res;
  }
  return Object.assign({}, last || {ok:false, status:'error'},
                       {attempts: maxAttempts, exhausted: maxAttempts > 1});
}

// Gedeelde afhandeling van het resultaat voor zowel de actie-knoppen als de
// vrije CLI-tab — één plek die de history-regel en de sessie-state bijwerkt.
function _repeaterCmdFinish(hist, res){
  if (res.ok) {
    hist.response = res.response || '(leeg / accepted)';
    if (res.attempts > 1) hist.response += '\n(gelukt na ' + res.attempts + ' pogingen)';
    hist.ok = true;
    _bumpRepeaterActivity();
  } else {
    const reason = res.error || res.message || res.status || 'fout';
    hist.response = res.exhausted
      ? ('failed na ' + res.attempts + ' pogingen — ' + reason)
      : reason;
    hist.ok = false;
    if (res.status === 'not_logged_in') {
      STATE.repeaterMgmt.logged_in = false;
      stopRepeaterCountdown();
      toast('sessie verlopen — opnieuw inloggen', 'err');
    } else if (res.exhausted) {
      toast('commando mislukt na ' + res.attempts + ' pogingen', 'err');
    }
  }
  renderDetail();
}

async function repeaterAction(cmd, label, confirmMsg){
  if (!STATE.selectedRepeater) return;
  if (confirmMsg && !confirm(confirmMsg)) return;
  // Voer 'm uit alsof het een CLI-commando is, zodat de respons in dezelfde
  // history-lijst terechtkomt en je achteraf kunt zien wat er teruggekomen is.
  const h = {cmd: '['+label+'] ' + cmd, response: '…wachten…', ok: null};
  STATE.repeaterMgmt.cli_history.push(h);
  renderDetail();
  _repeaterCmdFinish(h, await _repeaterCmdWithRetry(cmd, h));
}

function repeaterSyncTime(){
  // Repeater-firmware accepteert 'time <epoch_seconds>' direct.
  // (meshcore-cli's 'clock sync' is een alias die intern dit commando bouwt.)
  const epoch = Math.floor(Date.now() / 1000);
  repeaterAction('time ' + epoch, 'sync tijd', null);
}

function repeaterSendAdvert(){
  repeaterAction('advert', 'flood advert', null);
}

function repeaterReboot(){
  repeaterAction('reboot', 'reboot', 'Repeater echt herstarten? De radio is daarna kort niet bereikbaar en je sessie wordt verbroken.');
}

async function repeaterRunCli(){
  if (!STATE.selectedRepeater) return;
  const inp = $('rep-mgmt-cli-input');
  const cmd = (inp.value || '').trim();
  if (!cmd) return;
  inp.value = '';
  const h = {cmd: cmd, response: '…wachten…', ok: null};
  STATE.repeaterMgmt.cli_history.push(h);
  renderDetail();
  _repeaterCmdFinish(h, await _repeaterCmdWithRetry(cmd, h));
}

function renderRepeaterManage(){
  const r = STATE.selectedRepeater;
  if (!r) return '';
  const mgmt = STATE.repeaterMgmt || {};
  const st = mgmt.last_status;

  // 1) Repeater-header (compact, geen knoppen)
  const headerBlock =
    '<div class="detail-section"><h3>Repeater</h3>' +
      '<div class="kv">' +
        '<div><span class="k">naam:</span>' + escapeHTML(r.name || '?') + '</div>' +
        '<div><span class="k">type:</span>' + escapeHTML(r.type_label || '?') + '</div>' +
        '<div><span class="k">pubkey:</span><code style="font-size:11px">' + escapeHTML(r.pubkey.slice(0,16)) + '…</code></div>' +
      '</div>' +
    '</div>';

  // 2) Login/Logout-blok bovenaan
  let loginBlock;
  if (!mgmt.logged_in) {
    const saved = !!mgmt.has_saved_password;
    // Met een opgeslagen wachtwoord is de primaire actie één klik; het
    // handmatige veld blijft eronder staan om een gewijzigd wachtwoord
    // te kunnen invoeren (en meteen te overschrijven).
    const savedRow = saved
      ? '<div class="row" style="align-items:center;gap:8px;flex-wrap:wrap;margin-bottom:8px">' +
          '<button onclick="repeaterLogin(true)">verbind (opgeslagen wachtwoord)</button>' +
          '<button class="sec small" onclick="forgetRepeaterPassword()" title="verwijder het opgeslagen wachtwoord uit de database">vergeet</button>' +
        '</div>'
      : '';
    const pwPlaceholder = saved ? 'ander/nieuw wachtwoord' : 'admin-wachtwoord';
    loginBlock =
      '<div class="detail-section"><h3>Login</h3>' +
        savedRow +
        '<div class="row"><input id="rep-mgmt-pw" type="password" placeholder="' + pwPlaceholder + '" style="flex:1" autocomplete="off" ' +
          'onkeydown="if(event.key===\'Enter\'){event.preventDefault();repeaterLogin(false);}">' +
        '<button onclick="repeaterLogin(false)">manage</button></div>' +
        '<label class="note" style="display:flex;align-items:center;gap:6px;margin-top:6px;cursor:pointer">' +
          '<input type="checkbox" id="rep-mgmt-remember"' + (saved ? ' checked' : '') + '> ' +
          'onthoud dit wachtwoord' +
        '</label>' +
        '<div class="note" style="margin-top:4px;color:#888">' +
          'Opgeslagen wachtwoorden staan <b>onversleuteld</b> in de database en gelden voor alle admins. ' +
          'Behandel de DB en z\'n backups navenant.' +
        '</div>' +
        '<div id="rep-mgmt-login-status" class="note" style="margin-top:6px;color:#c33"></div>' +
      '</div>';
  } else {
    // Countdown + verleng-knop. Cijfer komt van _tickRepeaterCountdown
    // (1s interval; aangezet door startRepeaterCountdown na login/select).
    loginBlock =
      '<div class="detail-section"><h3>Ingelogd</h3>' +
        '<div class="row" style="align-items:center;gap:8px;flex-wrap:wrap">' +
          '<span class="note">Sessie verloopt over <b id="rep-mgmt-countdown" class="rep-cd">…</b></span>' +
          '<button onclick="repeaterKeepalive()" class="small" title="stuur een keepalive (clock) om de sessie te verlengen">verleng</button>' +
          '<button onclick="repeaterLogout()" class="sec">logout</button>' +
        '</div>' +
        '<div class="note" style="margin-top:6px;color:#888">' +
          'Elke verstuurde CLI-cmd telt ook als verleng. Bij verlopen valt de UI terug op het login-form.' +
        '</div>' +
      '</div>';
  }

  // 3) Status-blok (knop + resultaat). Werkt alleen zinvol na login.
  let statusInner;
  if (st && st.ok) {
    const bootDate = st.boot_time ? new Date(st.boot_time * 1000).toLocaleString() : '—';
    statusInner = '<div class="kv">' +
      '<div><span class="k">naam:</span>' + escapeHTML(st.name || r.name || '?') + '</div>' +
      '<div><span class="k">batterij:</span>' + (st.bat != null ? (st.bat + ' mV') : '—') + '</div>' +
      '<div><span class="k">uptime:</span>' + _fmtUptime(st.uptime) + '</div>' +
      '<div><span class="k">boot-tijd:</span>' + escapeHTML(bootDate) + '</div>' +
      '<div><span class="k">lokale tijd:</span>' + escapeHTML(st.remote_clock_text || (mgmt.logged_in ? '(geen \'clock\'-respons)' : '— (login vereist)')) + '</div>' +
      '</div>';
  } else if (st && !st.ok) {
    statusInner = '<div class="kv" style="color:#c33">fout: ' + escapeHTML(st.error || 'onbekend') + '</div>';
  } else {
    statusInner = '<div class="kv" style="color:#888">— nog niet opgevraagd —</div>';
  }
  const statusBtnDisabled = mgmt.logged_in ? '' : ' disabled title="login eerst" style="opacity:0.5;cursor:not-allowed"';
  const statusBlock =
    '<div id="rep-mgmt-status-box" class="detail-section"><h3>Status</h3>' +
      '<div class="row" style="margin-bottom:8px"><button onclick="repeaterRequestStatus()"' + statusBtnDisabled + '>request status</button></div>' +
      statusInner +
    '</div>';

  // 4) Acties-blok (alleen als ingelogd) — snelknoppen voor veelgebruikte commando's
  let actionsBlock = '';
  if (mgmt.logged_in) {
    actionsBlock =
      '<div class="detail-section"><h3>Acties</h3>' +
        '<div class="row" style="flex-wrap:wrap;gap:6px">' +
          '<button onclick="repeaterSyncTime()" title="stuur huidige browser-tijd naar de repeater">sync tijd</button>' +
          '<button onclick="repeaterSendAdvert()" title="laat de repeater een flood-advert versturen">advert</button>' +
          '<button onclick="repeaterReboot()" class="danger" title="repeater herstarten">reboot</button>' +
        '</div>' +
        '<div class="note" style="margin-top:6px">De daadwerkelijke commando\'s en hun respons verschijnen in de CLI-history hieronder.</div>' +
      '</div>';
  }

  // 5) CLI-blok (alleen tonen als ingelogd)
  let cliBlock = '';
  if (mgmt.logged_in) {
    const cliRows = (mgmt.cli_history || []).map(h => {
      const color = h.ok === false ? '#c33' : (h.ok === true ? '#28a745' : '#888');
      return '<div style="margin-bottom:8px;padding:6px;background:#f7f7f7;border-radius:4px">' +
             '<div style="font-family:monospace;font-size:12px">&gt; ' + escapeHTML(h.cmd) + '</div>' +
             '<pre style="margin:4px 0 0 0;font-size:11px;color:'+color+';white-space:pre-wrap;word-break:break-word">' +
                escapeHTML(h.response) + '</pre></div>';
    }).join('');
    cliBlock =
      '<div class="detail-section"><h3>CLI</h3>' +
        '<div style="max-height:240px;overflow-y:auto;margin-bottom:6px">' + (cliRows || '<div class="kv" style="color:#888">geen commando\'s verzonden</div>') + '</div>' +
        '<div class="row"><input id="rep-mgmt-cli-input" type="text" placeholder="commando, bv. clock" style="flex:1;font-family:monospace" onkeydown="if(event.key===\'Enter\'){event.preventDefault();repeaterRunCli();}">' +
        '<button onclick="repeaterRunCli()">run</button></div>' +
        '<div class="note" style="margin-top:6px">Antwoorden komen via een DM-respons; timeout 12s. Veelgebruikt: <code>clock</code>, <code>get name</code>, <code>advert</code>, <code>reboot</code>.</div>' +
      '</div>';
  }

  return headerBlock + loginBlock + statusBlock + actionsBlock + cliBlock;
}

async function toggleRepeaterFav(pubkey, isFav){
  try {
    if (isFav) {
      await api('/reports/repeaters/favorites/' + encodeURIComponent(pubkey), {method:'DELETE'});
    } else {
      await api('/reports/repeaters/favorites', {method:'POST', body: JSON.stringify({pubkey: pubkey})});
    }
  } catch(e) { return; }
  // Update local state + re-sort (favorieten eerst, dan type, dan naam)
  const row = (STATE.repeaterRows || []).find(r => r.pubkey === pubkey);
  if (row) row.is_favorite = !isFav;
  STATE.repeaterRows.sort((a,b) => {
    const fa = a.is_favorite ? 0 : 1, fb = b.is_favorite ? 0 : 1;
    if (fa !== fb) return fa - fb;
    const ta = a.type || 99, tb = b.type || 99;
    if (ta !== tb) return ta - tb;
    return (a.name || '').toLowerCase().localeCompare((b.name || '').toLowerCase());
  });
  renderRepeaterRows();
}

