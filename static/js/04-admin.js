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
    refreshAndRerender();
  } catch(e){}
}

function renderAdminHousekeeping(){
  const s = STATE.status || {db:{count:0}};
  $('admin-view').innerHTML = `
    <section><h2>Housekeeping</h2>
      <div class="row"><label>DB-records</label><span id="db-count" class="kv">…</span></div>
      <div class="row" style="margin-top:10px"><label>Verwijder ouder dan</label><input id="hk-age" type="number" placeholder="aantal" style="width:90px"><select id="hk-unit"><option value="86400">dagen</option><option value="3600">uren</option><option value="60">minuten</option></select><button onclick="cleanOlder()" class="danger">Verwijder</button></div>
      <div class="row"><button onclick="cleanAll()" class="danger">Alles verwijderen</button><button onclick="vacuum()">VACUUM</button></div>
    </section>
    <section><h2>Stale repeaters opruimen</h2>
      <div class="note" style="margin-bottom:8px">Verwijdert repeaters en rooms van de companion-contactlijst die meer dan 4 weken geen advert hebben gestuurd. Favorieten worden nooit opgeruimd, ongeacht leeftijd.</div>
      <div class="row"><button onclick="loadStaleRepeaters()">Toon kandidaten</button></div>
      <div id="stale-rep-result" style="margin-top:10px"></div>
    </section>`;
  $('db-count').textContent = (s.db?.count ?? '?') + ' berichten';
}

async function loadStaleRepeaters(){
  const el = $('stale-rep-result');
  el.innerHTML = '<div class="kv">…ophalen…</div>';
  let data;
  try { data = await api('/admin/repeaters/stale'); } catch(e) { return; }
  if (!data.count){
    el.innerHTML = '<div class="kv" style="color:#888">Geen kandidaten — alles is recent gezien of staat als favoriet.</div>';
    return;
  }
  const rows = data.items.map(it => {
    const lastAdv = it.last_advert ? new Date(it.last_advert * 1000).toLocaleString() : '—';
    return '<tr>' +
      '<td>' + escapeHTML(it.name) + '</td>' +
      '<td>' + it.type_label + '</td>' +
      '<td><code style="font-size:11px">' + it.pubkey_prefix + '</code></td>' +
      '<td>' + escapeHTML(lastAdv) + '</td>' +
      '<td>' + it.age_days + ' d</td>' +
      '</tr>';
  }).join('');
  el.innerHTML = `
    <div class="kv" style="margin-bottom:6px">${data.count} kandidaten (drempel: ${data.age_days_threshold} dagen).</div>
    <table style="width:100%">
      <thead><tr><th>Naam</th><th>Type</th><th>Pubkey-prefix</th><th>Laatste advert</th><th>Leeftijd</th></tr></thead>
      <tbody>${rows}</tbody>
    </table>
    <div class="row" style="margin-top:10px">
      <button onclick="cleanupStaleRepeaters(${data.count})" class="danger">Verwijder ${data.count} contacten</button>
    </div>`;
}

async function cleanupStaleRepeaters(expected){
  if (!confirm('Verwijder ' + expected + ' stale repeaters/rooms van de companion-contactlijst? Dit is niet ongedaan te maken.')) return;
  let res;
  try { res = await api('/admin/repeaters/cleanup', {method:'POST', body:'{}'}); } catch(e) { return; }
  toast(res.message || 'klaar');
  loadStaleRepeaters();
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
    const acts = '<button class="small danger" onclick="removeContact(\''+c.pubkey+'\',\''+escapeHTML(c.name||'').replace(/\\/g,"\\\\").replace(/'/g,"\\'")+'\')">remove</button>';
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
