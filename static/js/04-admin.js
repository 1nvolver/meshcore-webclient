/* ============== admin views (5 sub-pages) ============== */
function renderAdmin(){
  const sub = STATE.adminSub || 'radio';
  switch (sub) {
    case 'radio':        return renderAdminRadio();
    case 'node':         return renderAdminNode();
    case 'prefs':        return renderAdminPrefs();
    case 'channels':     return renderAdminChannels();
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
    <section><h2>Klok</h2>
      <div class="note" style="margin-bottom:8px">
        Advert- en berichttijden worden door de companion gestempeld. Loopt zijn klok
        uit de pas met de gateway, dan kloppen alle leeftijden niet — zie housekeeping.
      </div>
      <div class="kv" id="adm-clock">…uitlezen…</div>
      <div class="row" style="margin-top:8px">
        <button onclick="syncCompanionClock()">Nu gelijkzetten</button>
        <button class="sec" onclick="loadCompanionClock()">Opnieuw uitlezen</button>
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
  loadCompanionClock();   // kost één companion-roundtrip, dus async ná de render
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
  if (!confirm('Path-hash mode → '+mode+' (= '+(mode+1)+'-byte hashes)?\nAlle nodes in de mesh moeten dezelfde mode hebben.')) return;
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
    </section>
    <section><h2>Default flood-scope</h2>
      <div class="note" style="margin-bottom:8px">
        Companion-wide default. Wordt actief op kanalen die <b>zelf geen scope</b>
        hebben (zie Channels → scope). Een kanaal-eigen scope <b>overrulet</b> deze.
        Leeg = geen default (volle flood).
      </div>
      <div class="row">
        <label>Scope</label>
        <input id="n-default-scope" type="text" maxlength="31" placeholder="bv. #europa  (leeg = uit)" autocomplete="off">
        <button onclick="setDefaultScope()">Wijzigen</button>
        <button onclick="clearDefaultScope()" class="small">clear</button>
      </div>
      <div id="n-default-scope-status" class="note" style="margin-top:6px;color:#888">…ophalen…</div>
    </section>`;
  $('n-name').value=s.node?.name||''; $('n-lat').value=s.radio?.lat||''; $('n-lon').value=s.radio?.lon||'';
  loadDefaultScope();
}

async function loadDefaultScope(){
  const inp = $('n-default-scope');
  const note = $('n-default-scope-status');
  if (!inp || !note) return;
  let r;
  try {
    r = await api('/admin/radio/default-scope');
  } catch(e) {
    note.textContent = 'Lezen mislukt — companion niet bereikbaar?';
    note.style.color = '#c33';
    return;
  }
  if (r.supported === false) {
    inp.value = '';
    inp.disabled = true;
    note.textContent = r.message || 'Firmware/SDK ondersteunt dit niet.';
    note.style.color = '#c33';
    return;
  }
  inp.value = r.scope_name || '';
  if (r.error) {
    note.textContent = 'Cache: ' + (r.scope_name || '(geen)') + ' — ' + r.error;
    note.style.color = '#c33';
  } else if (r.scope_name) {
    note.textContent = 'Actief op de companion: ' + r.scope_name;
    note.style.color = '#666';
  } else {
    note.textContent = 'Geen default ingesteld.';
    note.style.color = '#888';
  }
}

async function setDefaultScope(){
  const inp = $('n-default-scope');
  if (!inp) return;
  const v = (inp.value || '').trim();
  try {
    const r = await api('/admin/radio/default-scope',
      {method:'POST', body:JSON.stringify({scope: v})});
    toast(r.message || 'ok', 'ok');
    loadDefaultScope();
  } catch(e){}
}

async function clearDefaultScope(){
  const inp = $('n-default-scope');
  if (inp) inp.value = '';
  setDefaultScope();
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
    tr.innerHTML='<td data-label="#">'+c.idx+'</td><td data-label="Type">'+(c.kind||'?')+'</td><td data-label="Naam">'+escapeHTML(c.name||'')+'</td><td data-label="Alias">'+escapeHTML(c.alias||'')+'</td><td data-label="Scope">'+escapeHTML(c.scope||'—')+'</td><td>'+scopeBtn+removeBtn+'</td>';
    tb.appendChild(tr);
  });
}

/* Channel scope-modal (v1.1.044) — vervangt eerdere prompt(). prompt() gaf
   geen uitleg, geen voorbeelden, geen maxlength. Modal toont waarvoor scope
   dient, voorbeelden, leeg-laat-gedrag en huidige waarde. */
function editScope(slot){
  const ch = STATE.channels.find(x => x.idx === slot) || {};
  const cur = ch.scope || '';
  const chName = ch.name || ('slot ' + slot);
  const chKind = ch.kind || '?';
  openModal('Flood-scope voor "' + chName + '"',
    '<p>Optionele <b>flood-scope</b> die vóór elke send naar dit kanaal wordt ' +
      'gezet via <code>set_flood_scope()</code>. Beperkt het bereik tot ontvangers ' +
      'die op deze scope luisteren. Leeg = geen scope (standaard flood, overal).</p>' +
    '<div class="kv" style="margin:8px 0">' +
      '<div><span class="k">slot:</span>' + slot + '</div>' +
      '<div><span class="k">kanaal:</span>' + escapeHTML(chName) + '</div>' +
      '<div><span class="k">type:</span>' + escapeHTML(chKind) + '</div>' +
      '<div><span class="k">huidig:</span>' + (cur ? '<code>' + escapeHTML(cur) + '</code>' : '<i>(geen)</i>') + '</div>' +
    '</div>' +
    '<div class="row" style="align-items:center;gap:8px;margin-top:10px">' +
      '<label style="flex:0;min-width:90px">Scope</label>' +
      '<input id="scope-input" type="text" maxlength="64" value="' + escapeHTML(cur) +
        '" placeholder="bv. #europa  (leeg = uit)" autocomplete="off" ' +
        'style="flex:1;font:inherit;padding:8px 10px;border:1px solid #ccc;border-radius:4px;min-height:40px;font-size:16px">' +
    '</div>' +
    '<div class="note" style="margin-top:8px">' +
      '<b>Voorbeelden:</b> <code>#europa</code>, <code>#nl-noord</code>, <code>#regio-zuid</code>.<br>' +
      'Conventie is een hashtag, maar elke tekenreeks van max 64 tekens werkt. Vrije tekst — geen validatie.' +
    '</div>' +
    '<div class="modal-actions">' +
      '<button type="button" onclick="_scopeClear()">Wissen (uit)</button>' +
      '<button type="button" class="primary" onclick="_scopeSave(' + slot + ')">Opslaan</button>' +
    '</div>'
  );
  const input = document.getElementById('scope-input');
  if (input) {
    input.focus();
    try { input.setSelectionRange(input.value.length, input.value.length); } catch(e) {}
    input.addEventListener('keydown', (e) => {
      if (e.key === 'Enter') { e.preventDefault(); _scopeSave(slot); }
    });
  }
}

function _scopeClear(){
  const input = document.getElementById('scope-input');
  if (input) { input.value = ''; input.focus(); }
}

async function _scopeSave(slot){
  const input = document.getElementById('scope-input');
  if (!input) return;
  const v = (input.value || '').trim();
  try {
    const r = await api('/admin/channels/scope', {method:'POST', body:JSON.stringify({slot, scope: v})});
    toast(r.message || 'ok', 'ok');
    closeModal();
    refreshAndRerender();
  } catch(e){
    // api() heeft al getoast — modal blijft open zodat user kan corrigeren.
  }
}

function renderAdminHousekeeping(){
  const s = STATE.status || {db:{count:0}};
  // Default: 28 dagen geleden als 'voor datum'.
  const dflt = new Date(Date.now() - 28 * 86400 * 1000);
  const dfltStr = dflt.toISOString().slice(0, 10);  // YYYY-MM-DD voor input[type=date]
  $('admin-view').innerHTML = `
    <section><h2>Housekeeping</h2>
      <div class="row"><label>DB-records</label><span id="db-count" class="kv">…</span></div>
      <div class="row" style="margin-top:10px"><label>Verwijder ouder dan</label><input id="hk-age" type="number" placeholder="aantal" style="width:90px"><select id="hk-unit"><option value="86400">dagen</option><option value="3600">uren</option><option value="60">minuten</option></select><button onclick="cleanOlder()" class="danger">Verwijder</button></div>
      <div class="row"><button onclick="cleanAll()" class="danger">Alles verwijderen</button><button onclick="vacuum()">VACUUM</button></div>
    </section>
    <section><h2>Stale companion-contacten opruimen</h2>
      <div class="note" style="margin-bottom:8px">
        Verwijdert contacten van de companion die sinds een gekozen datum geen advert
        meer hebben gestuurd. Selecteer welke types meegenomen worden. Favorieten
        (repeaters/rooms met ster) worden standaard overgeslagen.
      </div>
      <div class="row" style="align-items:center;flex-wrap:wrap;gap:12px">
        <label>Sinds datum</label>
        <input id="hk-stale-date" type="date" value="${dfltStr}" max="${(new Date()).toISOString().slice(0,10)}" style="flex:0;width:160px">
        <span id="hk-stale-days-hint" class="note" style="margin-left:4px"></span>
      </div>
      <div class="row" style="align-items:center;flex-wrap:wrap;gap:14px">
        <label>Types</label>
        <label style="font-weight:normal"><input type="checkbox" id="hk-stale-t1"> clients</label>
        <label style="font-weight:normal"><input type="checkbox" id="hk-stale-t2" checked> repeaters</label>
        <label style="font-weight:normal"><input type="checkbox" id="hk-stale-t3" checked> rooms</label>
      </div>
      <div class="row" style="align-items:center;flex-wrap:wrap;gap:14px">
        <label></label>
        <label style="font-weight:normal"><input type="checkbox" id="hk-stale-skipfavs" checked> Favorieten overslaan</label>
      </div>
      <div class="row"><button onclick="loadStaleContacts()">Toon kandidaten</button></div>
      <div id="stale-result" style="margin-top:10px"></div>
    </section>`;
  $('db-count').textContent = (s.db?.count ?? '?') + ' berichten';
  // Live hint: "(X dagen geleden)"
  const dateEl = $('hk-stale-date');
  const hintEl = $('hk-stale-days-hint');
  function updateHint(){
    // v1.1.055: Math.round() maakte van een drempel van 0,74 dagen
    // "(1 dagen geleden)" — dat las als "gisteren" terwijl de grens in
    // werkelijkheid vanochtend 00:00 lag. Nu exact, en in uren als het
    // minder dan twee dagen is.
    if (!dateEl.value) { hintEl.textContent = ''; return; }
    const picked = new Date(dateEl.value + 'T00:00:00');
    const ms = Math.max(0, Date.now() - picked.getTime());
    const days = ms / 86400000;
    const txt = days < 2
      ? '(grens = ' + picked.toLocaleDateString() + ' 00:00, ' + (ms / 3600000).toFixed(1) + ' uur geleden)'
      : '(grens = ' + picked.toLocaleDateString() + ' 00:00, ' + days.toFixed(1) + ' dagen geleden)';
    hintEl.textContent = txt;
  }
  dateEl.addEventListener('input', updateHint);
  updateHint();
}

function _hkStaleParams(){
  const dateStr = $('hk-stale-date').value;
  if (!dateStr) { toast('Datum is verplicht', 'err'); return null; }
  const picked = new Date(dateStr + 'T00:00:00');
  const days = Math.max(0, (Date.now() - picked.getTime()) / 86400000);
  const types = [];
  if ($('hk-stale-t1').checked) types.push(1);
  if ($('hk-stale-t2').checked) types.push(2);
  if ($('hk-stale-t3').checked) types.push(3);
  if (!types.length) { toast('Selecteer minstens één type', 'err'); return null; }
  return {
    days: days,
    types: types.join(','),
    skip_favorites: $('hk-stale-skipfavs').checked ? '1' : '0',
  };
}

async function loadCompanionClock(){
  const el = $('adm-clock');
  if (!el) return;
  el.innerHTML = '<div>…uitlezen…</div>';
  let c;
  try { c = await api('/admin/companion/time'); } catch(e) { return; }
  const a = c.auto_sync || {};
  const last = a.last || {};
  const rows = [];
  if (c.ok) {
    const skew = c.skew_secs || 0;
    const kleur = Math.abs(skew) > (a.threshold_secs || 30) ? '#c33' : '#28a745';
    rows.push('<div><span class="k">afwijking:</span><b style="color:' + kleur + '">' +
              (skew > 0 ? '+' : '') + skew + 's</b> ' +
              (Math.abs(skew) < 2 ? '(gelijk)' : '(' + _fmtSkew(skew) + (skew > 0 ? ' vóór' : ' achter') + ')') + '</div>');
    rows.push('<div><span class="k">companion:</span>' +
              escapeHTML(new Date(c.companion_epoch * 1000).toLocaleString()) + '</div>');
    rows.push('<div><span class="k">gateway:</span>' +
              escapeHTML(new Date(c.host_epoch * 1000).toLocaleString()) + '</div>');
  } else {
    rows.push('<div style="color:#c33">niet uitleesbaar: ' + escapeHTML(c.error || 'onbekend') + '</div>');
  }
  if (a.enabled) {
    rows.push('<div><span class="k">auto-sync:</span>elke ' +
              Math.round((a.interval_secs || 0) / 3600) + 'u, bijstellen vanaf ' +
              (a.threshold_secs || 0) + 's afwijking</div>');
  } else {
    rows.push('<div><span class="k">auto-sync:</span><span style="color:#c33">uit</span> (MESHCORE_TIME_SYNC=0)</div>');
  }
  if (last.checked_at) {
    rows.push('<div><span class="k">laatste check:</span>' +
              escapeHTML(new Date(last.checked_at * 1000).toLocaleString()) +
              ' — ' + escapeHTML(last.last_result || '?') + '</div>');
  }
  if (last.synced_at) {
    rows.push('<div><span class="k">laatst bijgesteld:</span>' +
              escapeHTML(new Date(last.synced_at * 1000).toLocaleString()) + '</div>');
  }
  el.innerHTML = rows.join('');
}

// Klok-skew. last_advert wordt door de companion gestempeld met ZIJN klok,
// terwijl wij de leeftijd berekenen tegen onze eigen tijd. Loopt de companion
// voor, dan komen adverts 'uit de toekomst' en is geen enkele drempel zinnig.
function _fmtSkew(secs){
  const a = Math.abs(secs);
  if (a < 90)    return Math.round(a) + ' seconden';
  if (a < 5400)  return Math.round(a/60) + ' minuten';
  if (a < 172800) return (a/3600).toFixed(1) + ' uur';
  return (a/86400).toFixed(2) + ' dagen';
}

function _renderClockWarning(clk, d){
  const future = d && d.skipped_future_advert;
  if (!clk) {
    return future ? '<div style="margin-top:8px;color:#c33"><b>' + future +
      ' contacten hebben een advert-tijd in de toekomst.</b> Dat kan alleen als de klok ' +
      'van de companion niet gelijkloopt met die van de gateway.</div>' : '';
  }
  if (!clk.ok) {
    return '<div style="margin-top:8px;color:#888">Companion-klok kon niet uitgelezen worden: ' +
           escapeHTML(clk.error || 'onbekend') + '</div>';
  }
  const skew = clk.skew_secs || 0;
  if (Math.abs(skew) < 60) {
    let out = '<div style="margin-top:8px;color:#888">Companion-klok loopt gelijk (' +
              _fmtSkew(skew) + ' verschil).</div>';
    if (future) {
      // Klok is inmiddels goed, maar de opgeslagen stempels nog niet: die zijn
      // gezet toen de klok scheef stond en schuiven pas recht bij een nieuwe advert.
      out += '<div style="margin-top:8px;padding:8px;background:#fff3cd;border-radius:4px;color:#7a5b00">' +
             '<b>' + future + ' contacten dragen nog een advert-tijd uit de periode dat de klok scheef stond.</b><br>' +
             'De klok is nu goed, maar bestaande tijdstempels worden daar niet met terugwerkende kracht ' +
             'door gecorrigeerd — elk contact krijgt pas een kloppende tijd bij zijn volgende advert. ' +
             'Opschonen op datum is pas betrouwbaar als dat rondje geweest is (meestal enkele uren).' +
             '</div>';
    }
    return out;
  }
  const richting = skew > 0 ? 'vóór' : 'achter';
  return '<div style="margin-top:10px;padding:8px;background:#fff3cd;border-radius:4px;color:#7a5b00">' +
         '<b>De klok van de companion loopt ' + _fmtSkew(skew) + ' ' + richting + '</b> op die van de gateway.<br>' +
         'Advert-tijden worden door de companion gestempeld, dus alle leeftijden in dit scherm ' +
         'zijn met datzelfde bedrag verschoven. Zolang dit zo staat, is opschonen op datum zinloos.' +
         '<div style="margin-top:8px"><button onclick="syncCompanionClock()">Zet de companion-klok gelijk</button></div>' +
         '</div>';
}

async function syncCompanionClock(){
  if (!confirm('De klok van de companion gelijkzetten aan die van de gateway?\n\n' +
               'Dit gebeurt normaal automatisch (zie Admin → Radio → Klok); deze knop ' +
               'forceert het nu, ook als de afwijking binnen de drempel valt.\n\n' +
               'Let op: bestaande advert-tijdstempels worden hier NIET door gecorrigeerd — ' +
               'die blijven verschoven tot elke node opnieuw geadverteerd heeft.')) return;
  let res;
  try { res = await api('/admin/companion/time/sync', {method:'POST', body:'{}'}); }
  catch(e) { return; }
  if (res.ok) {
    const na = res.after && res.after.skew_secs;
    toast('klok gelijkgezet' + (na != null ? ' (rest-verschil ' + _fmtSkew(na) + ')' : ''), 'ok');
  } else {
    toast(res.error || 'klok zetten mislukt', 'err');
  }
  // Beide panelen kunnen open staan; ververs wat er is.
  if ($('adm-clock')) loadCompanionClock();
  if ($('stale-result')) loadStaleContacts();
}

// Waarom vielen er contacten af? Zonder dit is "0 kandidaten" niet te
// onderscheiden van een kapotte filter.
function _renderStaleDiagnostics(data){
  const d = data && data.diagnostics;
  if (!d) return '';
  const rows = [
    ['contacten op de companion', d.contacts_total],
    ['afgevallen op type', d.skipped_type],
    ['overgeslagen als favoriet', d.skipped_favorite],
    ['zonder bekende advert-tijd', d.skipped_no_advert],
    ['jonger dan je drempel (en dus overgeslagen)', d.skipped_too_recent],
    ['\u2514 daarvan met een advert-tijd in de toekomst', d.skipped_future_advert],
    ['onleesbare entries', d.skipped_bad_entry],
  ].filter(r => r[1]);
  let extra = '';
  const thr = Math.round((data.age_days_threshold || 0) * 100) / 100;
  if (d.oldest_age_days != null) {
    extra = '<div style="margin-top:6px">Binnen de gekozen types is de oudste advert <b>' +
            d.oldest_age_days + ' dagen</b> oud, de nieuwste <b>' + d.newest_age_days +
            ' dagen</b>. Je drempel staat op <b>' + thr + ' dagen</b>.</div>';
    // De meest voorkomende verwarring: "er zijn er 83, waarvan 30 raar — dan
    // moeten er toch 53 weg?" Nee: die 83 zijn ALLEMAAL jonger dan de drempel,
    // en de 30 zijn daar een deelverzameling van. Zeg dat expliciet.
    if (!data.count && d.oldest_age_days <= thr) {
      extra += '<div style="margin-top:6px;padding:6px;background:#eef4ff;border-radius:4px">' +
               'Zelfs de <b>oudste</b> advert (' + d.oldest_age_days + ' dagen) is jonger dan je drempel (' +
               thr + ' dagen). Er is dus niets om op te ruimen — geen enkele datum die je hier kunt ' +
               'kiezen levert nu kandidaten op. De tellingen hierboven zijn geen aparte groepen: ' +
               'alle ' + (d.skipped_too_recent || 0) + ' vallen onder dezelfde regel.' +
               '</div>';
    }
    if (d.skipped_too_recent && d.oldest_age_days > thr) {
      extra += '<div style="margin-top:4px;color:#c33">Let op: er is wél iets ouder dan de drempel, ' +
               'maar het viel af op een andere regel — kijk naar de telling hierboven.</div>';
    }
  } else {
    extra = '<div style="margin-top:6px;color:#c33">Geen enkel contact binnen de gekozen types had een bruikbare advert-tijd. ' +
            'Dat wijst op een probleem met de contactenlijst, niet op je drempel.</div>';
  }
  return '<div class="note" style="margin-top:8px;padding:8px;background:#f7f7f7;border-radius:4px">' +
         '<b>Waarom niets?</b><ul style="margin:6px 0 0 18px;padding:0">' +
         rows.map(r => '<li>' + r[0] + ': <b>' + r[1] + '</b></li>').join('') +
         '</ul>' + extra + _renderClockWarning(d.clock, d) + '</div>';
}

async function loadStaleContacts(){
  const el = $('stale-result');
  if (!el) return;
  const p = _hkStaleParams();
  if (!p) return;
  el.innerHTML = '<div class="kv">…ophalen…</div>';
  const qs = new URLSearchParams(p).toString();
  let data;
  try { data = await api('/admin/contacts/stale?' + qs); } catch(e) { return; }
  if (!data.count){
    // v1.1.052: geen kale nul meer. De server vertelt nu waaróm elk contact
    // afviel, zodat je kunt zien of het aan de drempel ligt of aan iets anders.
    el.innerHTML = '<div class="kv" style="color:#888">Geen kandidaten.</div>' +
                   _renderStaleDiagnostics(data);
    return;
  }
  const rows = data.items.map(it => {
    const lastAdv = it.last_advert ? new Date(it.last_advert * 1000).toLocaleString() : '—';
    return '<tr>' +
      '<td data-label="Naam">' + escapeHTML(it.name) + '</td>' +
      '<td data-label="Type">' + it.type_label + '</td>' +
      '<td data-label="Pubkey-prefix"><code style="font-size:11px">' + it.pubkey_prefix + '</code></td>' +
      '<td data-label="Laatste advert">' + escapeHTML(lastAdv) + '</td>' +
      '<td data-label="Leeftijd">' + it.age_days + ' d</td>' +
      '</tr>';
  }).join('');
  const typesLbl = data.types.map(t => ({1:'clients',2:'repeaters',3:'rooms',4:'sensors'}[t] || t)).join(', ');
  el.innerHTML = `
    <div class="kv" style="margin-bottom:6px">${data.count} kandidaten (${typesLbl}; ouder dan ${data.age_days_threshold.toFixed(1)} dagen).</div>
    <table style="width:100%">
      <thead><tr><th>Naam</th><th>Type</th><th>Pubkey-prefix</th><th>Laatste advert</th><th>Leeftijd</th></tr></thead>
      <tbody>${rows}</tbody>
    </table>
    <div class="row" style="margin-top:10px">
      <button onclick="cleanupStaleContacts(${data.count})" class="danger">Verwijder ${data.count} contacten</button>
      ${_renderStaleDiagnostics(data)}
    </div>`;
}

async function cleanupStaleContacts(expected){
  const p = _hkStaleParams();
  if (!p) return;
  if (!confirm('Verwijder ' + expected + ' contacten van de companion-contactlijst?\n\nDit is niet ongedaan te maken; deze contacten kunnen later weer binnenkomen via een nieuwe advert.')) return;
  let res;
  try {
    res = await api('/admin/contacts/cleanup', {method:'POST', body:JSON.stringify({
      days: parseFloat(p.days),
      types: p.types,
      skip_favorites: p.skip_favorites === '1',
    })});
  } catch(e) { return; }
  // v1.1.051: mislukte verwijderingen niet wegmoffelen. De server telt nu
  // alleen een echt 'command_ok' als verwijderd; de rest komt hier terug met
  // een reden (companion weigerde / timeout).
  const failed = res.failed_count || 0;
  if (failed > 0) {
    toast(res.message || 'klaar', 'err');
    const reasons = (res.failed || []).slice(0, 5)
      .map(f => (f.name || f.pubkey_prefix || '?') + ': ' + (f.error || 'onbekend'));
    const extra = (res.failed || []).length > 5 ? '\n…en nog ' + ((res.failed || []).length - 5) : '';
    alert('Niet alle contacten zijn verwijderd (' + failed + ' mislukt):\n\n' +
          reasons.join('\n') + extra);
  } else {
    toast(res.message || 'klaar', 'ok');
  }
  if (res.contacts_refreshed === false) {
    toast('contactenlijst kon niet ververst worden — het overzicht loopt mogelijk achter', 'err');
  }
  loadStaleContacts();
  refresh();
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
      '<td data-label="Naam">' + escapeHTML(b.name) + '</td>' +
      '<td data-label="Kanaal">' + escapeHTML(chanLbl) + '</td>' +
      '<td data-label="Keyword"><code style="font-size:13px">?'+escapeHTML(b.keyword)+'</code></td>' +
      '<td data-label="Status">' + en + '</td>' +
      '<td data-label="Reply" style="font-family:ui-monospace,monospace;font-size:12px;word-break:break-word">' +
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
      if (u.has_password) btns.push('<button class="small" onclick="resetUserPw(\''+u.username+'\')">reset pw</button>');
      btns.push('<button class="small danger" onclick="deleteUser(\''+u.username+'\')">verwijder</button>');
      tr.innerHTML = '<td data-label="Naam">'+escapeHTML(u.username)+'</td><td data-label="Rol">'+u.role+'</td><td data-label="Wachtwoord">'+pwState+'</td><td data-label="Laatste login">'+escapeHTML(last)+'</td><td>'+btns.join(' ')+'</td>';
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
  if (!confirm('Wachtwoord van '+username+' resetten naar tijdelijk ww?\n'+username+' moet bij volgende login zelf nieuw ww instellen.')) return;
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
  if (!confirm('Radio → freq='+body.freq+' bw='+body.bw+' sf='+body.sf+' cr='+body.cr+'?\nReboot vereist.')) return;
  const r=await api('/admin/radio',{method:'POST',body:JSON.stringify(body)}); toast(r.message||'ok','ok'); refreshAndRerender();
}
async function setTxPower(){
  const dbm=parseInt($('r-tx').value);
  if (!confirm('tx-power → '+dbm+' dBm? Reboot vereist.')) return;
  const r=await api('/admin/txpower',{method:'POST',body:JSON.stringify({dbm})}); toast(r.message||'ok','ok'); refreshAndRerender();
}
async function setName(){
  const name=$('n-name').value.trim(); if(!name) return;
  if (!confirm('Naam → '+name+'?')) return;
  const r=await api('/admin/name',{method:'POST',body:JSON.stringify({name})}); toast(r.message||'ok','ok'); refreshAndRerender();
}
async function setCoords(){
  const lat=parseFloat($('n-lat').value),lon=parseFloat($('n-lon').value);
  const r=await api('/admin/coords',{method:'POST',body:JSON.stringify({lat,lon})}); toast(r.message||'ok','ok'); refreshAndRerender();
}
async function clearCoords(){
  if (!confirm('Coords wissen?')) return;
  const r=await api('/admin/coords',{method:'POST',body:JSON.stringify({clear:true})}); toast(r.message||'ok','ok'); refreshAndRerender();
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
    toast(r.message + (r.generated_key ? '\nKEY: '+r.generated_key : ''), 'ok');
    refreshAndRerender();
  } catch(e){}
}
async function removeChannel(slot){
  if (!confirm('Slot '+slot+' uit DB-metadata verwijderen?')) return;
  await api('/admin/channels/remove',{method:'POST',body:JSON.stringify({slot})}); refreshAndRerender();
}
async function cleanOlder(){
  const n=parseInt($('hk-age').value),unit=parseInt($('hk-unit').value);
  if (!n){toast('aantal vereist','err');return;}
  const seconds=n*unit;
  const c=await api('/admin/clean/preview?seconds='+seconds);
  if (!confirm(c.count+' berichten ouder dan dat — verwijderen?')) return;
  const r=await api('/admin/clean',{method:'POST',body:JSON.stringify({mode:'older',seconds})}); toast(r.message,'ok'); refreshAndRerender();
}
async function cleanAll(){
  if (!confirm('ALLE berichten verwijderen?')) return;
  const r=await api('/admin/clean',{method:'POST',body:JSON.stringify({mode:'all'})}); toast(r.message,'ok'); refreshAndRerender();
}
async function vacuum(){
  if (!confirm('VACUUM uitvoeren?')) return;
  const r=await api('/admin/vacuum',{method:'POST',body:'{}'}); toast(r.message,'ok');
}
