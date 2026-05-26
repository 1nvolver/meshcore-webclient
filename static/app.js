/* ============== state ============== */
let STATE = {
  view: 'chat',           // 'chat' | 'admin' | 'reports'
  adminSub: 'radio',
  reportSub: 'overview',
  channel: {kind:'public', idx:0, name:'Public'},  // ook DM: {kind:'dm', peer, name}
  channels: [],
  contacts: [],           // van /contacts (companion-contactenlijst)
  myContacts: [],         // van /my/contacts (per-user opgeslagen)
  status: null,
  selectedMsg: null,
  me: null,
  msgIndex: {},
  // chat-controls
  paused: false,
  pendingMsgs: [],        // berichten die binnenkomen tijdens pauze
  filterText: '',         // huidige tekst-filter (lowercase)
  timeAnchorHours: 0,     // 0 = realtime; >0 = N uur in het verleden
  reportPeriodHours: 24,  // default grafiekperiode
  // Repeater-rapport: laatst-gefetchte rows + zoektekst (lowercase)
  repeaterRows: [],
  repeaterSearch: '',
  selectedRepeater: null,    // {pubkey, name, type_label} of null
  repeaterMgmt: {            // UI-state voor het manage-paneel
    logged_in: false,
    cli_history: [],         // [{cmd, response, ok}]
    last_status: null,       // payload van /admin/repeaters/manage_status
  },
};

/* ============== helpers ============== */
function $(id){return document.getElementById(id);}
function escapeHTML(s){return String(s||'').replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;","\"":"&quot;","'":"&#39;"})[c]);}

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
  // Reports-sub-items markeren
  document.querySelectorAll('#grp-reports li[data-report]').forEach(li => {
    li.classList.toggle('active', STATE.view === 'reports' && li.dataset.report === STATE.reportSub);
  });
  // DM-tree
  renderDmTree();
}

function renderDmTree(){
  const ul = $('tree-dms');
  if (!ul) return;
  // Pak het Contactpersonen-beheer-item, knip het er even uit en plak weer bovenaan
  const mgr = $('li-contacts-mgr');
  ul.innerHTML = '';
  if (mgr) {
    mgr.classList.toggle('active', STATE.view === 'contacts');
    ul.appendChild(mgr);
  }

  const my = STATE.myContacts || [];
  if (my.length === 0) {
    const li = document.createElement('li');
    li.style.color = '#bbb';
    li.style.fontStyle = 'italic';
    li.style.fontSize = '0.85em';
    li.textContent = '(geen opgeslagen contacten)';
    ul.appendChild(li);
    return;
  }
  my.forEach(c => {
    const li = document.createElement('li');
    const lab = document.createElement('span');
    lab.className = 'label';
    lab.textContent = c.name || c.pubkey_prefix;
    li.appendChild(lab);
    li.onclick = () => selectChannel({kind:'dm', peer: c.pubkey_prefix, name: c.name || c.pubkey_prefix});
    if (STATE.view === 'chat' && STATE.channel.kind === 'dm' && STATE.channel.peer === c.pubkey_prefix) {
      li.classList.add('active');
    }
    ul.appendChild(li);
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
  _hideAllViews();
  $('chat-view').style.display = 'flex';
  const dmSuffix = (ch.kind === 'dm' && ch.peer) ? ' (DM · ' + ch.peer + ')' : '';
  $('view-title').textContent = ch.name + dmSuffix;
  $('txt').placeholder = 'Bericht naar ' + ch.name + '…';
  loadChatHistory();
  renderTree();
  renderDetail();
}
function _hideAllViews(){
  $('chat-view').style.display = 'none';
  $('admin-view').style.display = 'none';
  $('reports-view').style.display = 'none';
  const cv = $('contacts-view');
  if (cv) cv.style.display = 'none';
}

function selectAdminView(sub){
  if (!STATE.me || STATE.me.role !== 'admin') return;
  STATE.view = 'admin';
  STATE.adminSub = sub;
  _hideAllViews();
  $('admin-view').style.display = 'block';
  const titles = {radio:'Radio', node:'Node', prefs:'Voorkeuren',
                  channels:'Channels', contacts:'Contacten', bots:'Bots',
                  housekeeping:'Housekeeping', users:'Gebruikers'};
  $('view-title').textContent = 'Admin — ' + (titles[sub] || sub);
  renderAdmin();
  renderTree();
  renderDetail();
}

function selectContactsManager(){
  STATE.view = 'contacts';
  _hideAllViews();
  $('contacts-view').style.display = 'block';
  $('view-title').textContent = 'DM — Contactpersonen';
  renderContactsManager();
  renderTree();
  renderDetail();
}

async function renderContactsManager(){
  const el = $('contacts-view');
  el.innerHTML = '<section><h2>Mijn contactpersonen</h2><div class="kv">…laden…</div></section>';
  let mine;
  try { mine = await api('/my/contacts'); } catch(e) { return; }

  const rows = mine.map(c => {
    const created = c.created_at ? new Date(c.created_at).toLocaleDateString() : '—';
    const safeName = escapeHTML(c.name || '').replace(/\\/g,'\\\\').replace(/\x27/g,"\\\x27");
    const status = c.known_to_companion
      ? '<span style="color:#161" title="bekend bij companion — DM werkt">✓</span>'
      : '<span style="color:#c80" title="niet bekend bij companion — DM werkt nog niet">⚠</span>';
    return '<tr>' +
      '<td>' + status + ' ' + escapeHTML(c.name || '?') + '</td>' +
      '<td><code style="font-size:11px;word-break:break-all" title="'+escapeHTML(c.pubkey)+'">' + escapeHTML(c.pubkey_prefix) + '…</code></td>' +
      '<td>' + escapeHTML(c.notes || '') + '</td>' +
      '<td>' + escapeHTML(created) + '</td>' +
      '<td><button class="small danger" onclick="removeMyContact(\''+c.pubkey+'\',\''+safeName+'\')">verwijder</button></td>' +
    '</tr>';
  }).join('');

  el.innerHTML = `
    <section><h2>Mijn contactpersonen (${mine.length})</h2>
      <table style="width:100%">
        <thead><tr>
          <th>Status · Naam</th><th>Pubkey (prefix · hover voor vol)</th><th>Notitie</th><th>Toegevoegd</th><th></th>
        </tr></thead>
        <tbody>${rows || '<tr><td colspan="5" style="color:#888">geen opgeslagen contacten</td></tr>'}</tbody>
      </table>
      <div class="note">
        <b>✓</b> = bekend bij companion (DM werkt). <b>⚠</b> = lokaal opgeslagen, maar de companion kent de pubkey nog niet → DM faalt met "not found".
        Wacht op een advert van die node (of zet "Auto-add adverts" aan in <i>Admin → Voorkeuren</i>) zodat de companion 'm leert kennen.
      </div>
    </section>

    <section><h2>Contactpersoon toevoegen</h2>
      <div class="row"><label>Naam</label><input id="mc-name" type="text" placeholder="bv 'Henk'"></div>
      <div class="row"><label>Pubkey</label><input id="mc-pk" type="text" placeholder="64 hex chars (32 bytes), bv 2e400317326bc8d4..." style="font-family:monospace"></div>
      <div class="row"><label>Notitie</label><input id="mc-notes" type="text" placeholder="optioneel"></div>
      <div class="row"><button onclick="addMyContact()">Toevoegen</button></div>
      <div class="note">Volledige 32-byte publieke sleutel (te vinden in Admin → Contacten of in een share/QR).</div>
      <div class="note">Per-user opgeslagen — andere web-gebruikers zien jouw contacten niet.</div>
    </section>`;
}

async function addMyContact(){
  const name = $('mc-name').value.trim();
  const pk   = $('mc-pk').value.trim().toLowerCase();
  const notes= $('mc-notes').value.trim();
  if (!name || !pk) { toast('naam + pubkey vereist','err'); return; }
  if (pk.length !== 64) { toast('pubkey moet 64 hex chars zijn (gaf '+pk.length+')','err'); return; }
  try {
    const r = await api('/my/contacts/add', {method:'POST', body:JSON.stringify({name, pubkey: pk, notes: notes || null})});
    toast(r.message || 'ok', 'ok');
    $('mc-name').value = ''; $('mc-pk').value = ''; $('mc-notes').value = '';
    await refreshMyContacts();
    renderContactsManager();
  } catch(e){}
}

async function removeMyContact(pubkey, name){
  if (!confirm('Contactpersoon "'+name+'" verwijderen?')) return;
  try {
    const r = await api('/my/contacts/remove', {method:'POST', body:JSON.stringify({pubkey: pubkey})});
    toast(r.message || 'ok', 'ok');
    await refreshMyContacts();
    renderContactsManager();
  } catch(e){}
}

async function refreshMyContacts(){
  try {
    STATE.myContacts = await api('/my/contacts');
    renderTree();
  } catch(e){}
}

function selectReport(sub){
  STATE.view = 'reports';
  STATE.reportSub = sub;
  // Bij wisselen van rapport: repeater-selectie wissen zodat detail-paneel terug naar default gaat
  STATE.selectedRepeater = null;
  _hideAllViews();
  $('reports-view').style.display = 'block';
  const titles = {overview:'Overzicht', repeaters:'Repeaters'};
  $('view-title').textContent = 'Rapportages — ' + (titles[sub] || sub);
  renderReports();
  renderTree();
  renderDetail();
}

/* ============== chat ============== */
async function loadChatHistory(){
  $('log').innerHTML = '';
  STATE.selectedMsg = null;
  STATE.msgIndex = {};
  STATE.pendingMsgs = [];
  try {
    let basePath;
    if (STATE.channel.kind === 'dm') {
      basePath = '/dm/' + encodeURIComponent(STATE.channel.peer) + '/history';
    } else {
      basePath = '/channels/' + STATE.channel.idx + '/history';
    }
    let path = basePath + '?limit=30';
    if (STATE.timeAnchorHours > 0) {
      path = basePath + '?limit=200';
    }
    const rows = await api(path);
    let toRender = rows;
    if (STATE.timeAnchorHours > 0) {
      const anchorMs = Date.now() - STATE.timeAnchorHours * 3600 * 1000;
      toRender = rows.filter(m => new Date(m.ts).getTime() <= anchorMs).slice(-30);
    }
    toRender.forEach(m => addMsg(m, /*skipFilter=*/false));
    applyFilter();
  } catch(e){}
}

async function loadOlder(){
  if (STATE.msgIndex && Object.keys(STATE.msgIndex).length === 0) {
    return loadChatHistory();
  }
  const ids = Object.keys(STATE.msgIndex).map(Number);
  if (ids.length === 0) return;
  const oldest = Math.min(...ids);
  let path;
  if (STATE.channel.kind === 'dm') {
    path = '/dm/' + encodeURIComponent(STATE.channel.peer) + '/history?limit=30&before_id=' + oldest;
  } else {
    path = '/channels/' + STATE.channel.idx + '/history?limit=30&before_id=' + oldest;
  }
  try {
    const rows = await api(path);
    if (rows.length === 0) {
      toast('geen oudere berichten', 'ok');
      return;
    }
    // Prepend in DOM (in volgorde, oudste bovenaan)
    const log = $('log');
    const wasAtBottom = log.scrollTop + log.clientHeight >= log.scrollHeight - 4;
    const sentinel = log.firstChild;
    rows.forEach(m => {
      const div = _buildMsgEl(m);
      log.insertBefore(div, sentinel);
    });
    applyFilter();
    // Scroll niet auto naar onder als we boven aan het kijken zijn
    if (wasAtBottom) log.scrollTop = log.scrollHeight;
  } catch(e){}
}

function shiftTime(deltaH){
  // deltaH negatief = ouder, positief = recenter
  STATE.timeAnchorHours = Math.max(0, STATE.timeAnchorHours - deltaH);
  updateNowButton();
  loadChatHistory();
}

function jumpToNow(){
  STATE.timeAnchorHours = 0;
  STATE.paused = false;
  updatePauseButton();
  updateNowButton();
  loadChatHistory();
}

function updateNowButton(){
  const btn = document.querySelector('.cc-now');
  if (!btn) return;
  if (STATE.timeAnchorHours > 0) {
    btn.textContent = 'nu (-' + STATE.timeAnchorHours + 'u)';
    btn.style.background = '#fc6';
  } else {
    btn.textContent = 'nu';
    btn.style.background = '';
  }
}

function togglePause(){
  STATE.paused = !STATE.paused;
  updatePauseButton();
  if (!STATE.paused) {
    // Replay queued msgs
    const q = STATE.pendingMsgs;
    STATE.pendingMsgs = [];
    q.forEach(m => addMsg(m));
  }
}

function updatePauseButton(){
  const btn = $('cc-pause-btn');
  if (!btn) return;
  if (STATE.paused) {
    btn.textContent = '▶ ' + (STATE.pendingMsgs.length || '');
    btn.classList.add('paused');
    btn.title = 'hervat live updates (' + STATE.pendingMsgs.length + ' wachtend)';
  } else {
    btn.textContent = '⏸';
    btn.classList.remove('paused');
    btn.title = 'pauzeer live updates';
  }
}

function applyFilter(){
  // Lokaal verbergen tijdens typen / live-msgs
  const f = STATE.filterText;
  document.querySelectorAll('#log .msg').forEach(el => {
    if (!f) { el.style.display = ''; return; }
    el.style.display = el.textContent.toLowerCase().includes(f) ? '' : 'none';
  });
}

let _searchTimer = null;
async function runServerSearch(q){
  // Server-side search door alle berichten in dit kanaal/dm
  if (!q) {
    // Filter leeg → reset naar gewone history
    return loadChatHistory();
  }
  $('log').innerHTML = '';
  STATE.msgIndex = {};
  try {
    const params = new URLSearchParams({q, limit: '200'});
    if (STATE.channel.kind === 'dm') {
      params.set('kind', 'dm');
      params.set('peer', STATE.channel.peer);
    } else {
      params.set('kind', 'channel');
      params.set('channel_idx', String(STATE.channel.idx));
    }
    const rows = await api('/messages/search?' + params.toString());
    rows.forEach(m => addMsg(m));
    if (rows.length === 0) {
      const div = document.createElement('div');
      div.className = 'msg sys';
      div.style.color = '#888';
      div.style.fontStyle = 'italic';
      div.textContent = '— geen resultaten voor "'+q+'" —';
      $('log').appendChild(div);
    }
  } catch(e){}
}

/* Extract afzendernaam uit channel-msg tekst.
   Companion firmware geeft pubkey_prefix vaak NIET mee op channels;
   afzenders prefixen hun naam zelf met "NAAM: tekst".
   Heuristiek: naam = alles tot eerste ':', max 64 chars, mag spaties bevatten,
   geen newlines, geen URL-achtige patronen ('://'). */
function extractSender(m){
  if (m.peer && m.peer !== '?' && m.peer !== 'self') return m.peer;
  const t = m.text || '';
  if (!t) return null;
  // Vermijd URLs: 'http://...' zou anders 'http' als naam pakken
  const urlIdx = t.indexOf('://');
  const colon = t.indexOf(':');
  if (colon <= 0 || colon > 64) return null;
  if (urlIdx >= 0 && urlIdx <= colon) return null;
  const head = t.substring(0, colon);
  if (head.indexOf(String.fromCharCode(10)) >= 0) return null;
  if (head.indexOf(String.fromCharCode(13)) >= 0) return null;
  const candidate = head.trim();
  if (!candidate) return null;
  // Geen control chars in naam
  for (let i = 0; i < candidate.length; i++) {
    if (candidate.charCodeAt(i) < 32) return null;
  }
  return candidate;
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

function ackIcon(msg){
  if (!msg || msg.direction !== 'out') return '';
  const status = msg.ack_status;
  if (msg.kind === 'dm') {
    if (status === 'acked')   return '<span class="ack ack-acked" title="bevestigd">✓✓</span>';
    if (status === 'failed')  return '<span class="ack ack-failed" title="mislukt">!!</span>';
    if (status === 'sent')    return '<span class="ack ack-sent" title="verzonden, wachten op ack">✓</span>';
    return '';
  }
  // Channel-out: geen ack op protocol-niveau, maar wel implicit-repeat detectie
  if (msg.kind === 'channel') {
    if (status === 'repeated') return '<span class="ack ack-acked" title="opgepikt door mesh-repeater">↻</span>';
    if (status === 'sent')     return '<span class="ack ack-sent" title="verzonden">✓</span>';
  }
  return '';
}

function signalDot(meta){
  // Eén gekleurd bolletje op basis van SNR (of hops als geen SNR).
  let cls = null, label = '';
  if (typeof meta.snr === 'number') {
    if (meta.snr >= 7)       { cls = 'sig-good'; label = 'SNR ' + meta.snr.toFixed(1) + ' dB (uitstekend)'; }
    else if (meta.snr >= 0)  { cls = 'sig-ok';   label = 'SNR ' + meta.snr.toFixed(1) + ' dB (goed)'; }
    else if (meta.snr >= -7) { cls = 'sig-mid';  label = 'SNR ' + meta.snr.toFixed(1) + ' dB (matig)'; }
    else                     { cls = 'sig-bad';  label = 'SNR ' + meta.snr.toFixed(1) + ' dB (slecht)'; }
  } else if (typeof meta.hops === 'number') {
    if (meta.hops <= 0)        { cls = 'sig-good'; label = 'direct (0 hops)'; }
    else if (meta.hops === 1)  { cls = 'sig-ok';   label = '1 hop'; }
    else if (meta.hops === 2)  { cls = 'sig-mid';  label = '2 hops'; }
    else                       { cls = 'sig-bad';  label = meta.hops + ' hops'; }
  }
  if (!cls) return '';
  return '<span class="sig-dot '+cls+'" title="'+label+'">●</span>';
}

function _buildMsgEl(m){
  const div = document.createElement('div');
  div.className = 'msg ' + (m.direction === 'out' ? 'out' : 'in');
  const displayPeer = extractSender(m) || (m.direction === 'out' ? 'self' : '?');
  const displayText = stripNamePrefix(m);
  if (m.direction === 'in' && isMention(m)) div.classList.add('mention');

  let prefix = '';
  if (m.direction === 'out') prefix = ackIcon(m);
  else                       prefix = signalDot(extractMeta(m));

  div.innerHTML = prefix +
    '<span class="ts">'+escapeHTML(fmtTs(m.ts))+'</span>' +
    '<span class="peer">'+escapeHTML(displayPeer)+':</span>' +
    escapeHTML(displayText);
  div.onclick = () => selectMsg(m, div);
  m._sender = displayPeer;
  m._body = displayText;
  if (m.id) STATE.msgIndex[m.id] = {el: div, msg: m};
  return div;
}

function addMsg(m){
  const div = _buildMsgEl(m);
  $('log').appendChild(div);
  // Filter direct toepassen
  const f = STATE.filterText;
  if (f && !div.textContent.toLowerCase().includes(f)) {
    div.style.display = 'none';
  } else {
    $('log').scrollTop = $('log').scrollHeight;
  }
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
sock.on('connect',    () => { $('conn-title').textContent='MeshCore Gateway Web Client · verbonden'; $('txt').disabled=false; $('btn').disabled=false; });
sock.on('disconnect', () => { $('conn-title').textContent='MeshCore Gateway Web Client · verbroken'; $('txt').disabled=true; $('btn').disabled=true; });
sock.on('connect_error', () => { setTimeout(()=>location.href='/login', 1500); });
sock.on('msg', (m) => {
  // Mention-detectie: alleen op kanaal-msgs (DMs zijn al gericht aan mij)
  if (m.kind === 'channel' && m.direction === 'in' && isMention(m)) {
    onMention(m);
  }
  if (STATE.view !== 'chat') return;
  // Filter op huidige view: channel of DM
  if (STATE.channel.kind === 'dm') {
    if (m.kind !== 'dm') return;
    if (m.peer !== STATE.channel.peer) return;
  } else {
    if (m.kind !== 'channel') return;
    if (m.channel_idx !== STATE.channel.idx) return;
  }
  if (STATE.timeAnchorHours > 0) return;
  if (STATE.filterText) return;  // tijdens search-modus geen live-updates
  if (STATE.paused) {
    STATE.pendingMsgs.push(m);
    updatePauseButton();
    return;
  }
  addMsg(m);
});

sock.on('msg-update', (u) => {
  // Update inline ack-icoon en bewaarde state voor latency in detail
  if (!u || !u.msg_id) return;
  const entry = STATE.msgIndex[u.msg_id];
  if (entry) {
    entry.msg.ack_status = u.ack_status;
    entry.msg.acked_at = u.acked_at;
    entry.msg.latency_s = u.latency_s;
    // Re-render alleen het ack-icoon (eerste span vervangen)
    const oldAck = entry.el.querySelector('.ack');
    const newAck = document.createElement('span');
    newAck.innerHTML = ackIcon(entry.msg);
    if (oldAck && newAck.firstChild) entry.el.replaceChild(newAck.firstChild, oldAck);
    else if (newAck.firstChild) entry.el.insertBefore(newAck.firstChild, entry.el.firstChild);
  }
  // Detail-pane bijwerken als deze msg geselecteerd is
  if (STATE.selectedMsg && STATE.selectedMsg.id === u.msg_id) {
    STATE.selectedMsg.ack_status = u.ack_status;
    STATE.selectedMsg.acked_at = u.acked_at;
    STATE.selectedMsg.latency_s = u.latency_s;
    renderDetail();
  }
});

$('chat-form').addEventListener('submit', (e) => {
  e.preventDefault();
  const t = $('txt').value.trim();
  if (!t) return;
  $('btn').disabled = true;
  let payload;
  if (STATE.channel.kind === 'dm') {
    payload = {kind:'dm', peer: STATE.channel.peer, text: t};
  } else {
    payload = {channel_idx: STATE.channel.idx, text: t};
  }
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
  const msg = "Hashtag-kanaal " + displayName + " (slot " + slot + ") verwijderen uit DB?\n\nLet op: de slot blijft op de companion bestaan tot je hem via Admin overschrijft.";
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
    return '<tr><td>'+escapeHTML(naam)+'</td><td style="text-align:right">'+c.count+'</td></tr>';
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
      <div class="note" style="margin-bottom:8px">Bron: contactenlijst van de companion (alle nodes met type repeater of room-server). Favorieten staan bovenaan.</div>
      <div class="row" style="margin-bottom:8px">
        <input id="rep-search" type="text" placeholder="zoek op naam, pubkey of hash…" style="flex:1"
               value="${escapeHTML(STATE.repeaterSearch || '')}"
               oninput="filterRepeaterTable(this.value)">
      </div>
      <table style="width:100%">
        <thead><tr>
          <th style="width:24px"></th>
          <th>Naam</th><th>Type</th><th>Hash</th><th>Pubkey-prefix</th>
          <th>Laatste advert</th><th>Locatie</th><th>Path</th>
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

function renderRepeaterRows(){
  const tbody = $('rep-tbody');
  if (!tbody) return;
  const q = (STATE.repeaterSearch || '').toLowerCase();
  const list = (STATE.repeaterRows || []).filter(r => {
    if (!q) return true;
    const name = (r.name || '').toLowerCase();
    const pk   = (r.pubkey || '').toLowerCase();
    const pref = (r.pubkey_prefix || '').toLowerCase();
    const h1   = (r.hash_1b || '').toLowerCase();
    const h2   = (r.hash_2b || '').toLowerCase();
    return name.includes(q) || pk.includes(q) || pref.includes(q) || h1.includes(q) || h2.includes(q);
  });
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
      '<td>' + escapeHTML(r.name || '?') + '</td>' +
      '<td>' + r.type_label + '</td>' +
      '<td>' + hashCell + '</td>' +
      '<td><code style="font-size:11px">' + r.pubkey_prefix + '</code></td>' +
      '<td>' + escapeHTML(lastAdv) + '</td>' +
      '<td>' + escapeHTML(loc) + '</td>' +
      '<td>' + opl + '</td>' +
      '<td>' + pingCell + '</td>' +
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
  STATE.repeaterMgmt = {logged_in: false, cli_history: [], last_status: null};
  renderRepeaterRows();   // herteken voor de selectie-highlight
  renderDetail();
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
  renderDetail();
}

async function repeaterLogin(){
  if (!STATE.selectedRepeater) return;
  const pwd = $('rep-mgmt-pw').value || '';
  if (!pwd){ toast('wachtwoord vereist', 'err'); return; }
  $('rep-mgmt-login-status').textContent = '…inloggen…';
  let res;
  try {
    res = await api('/admin/repeaters/login', {method:'POST',
      body: JSON.stringify({pubkey: STATE.selectedRepeater.pubkey, password: pwd})});
  } catch(e) { return; }
  if (res.ok){
    STATE.repeaterMgmt.logged_in = true;
    $('rep-mgmt-pw').value = '';
    toast(res.message || 'ingelogd');
  } else {
    STATE.repeaterMgmt.logged_in = false;
    $('rep-mgmt-login-status').textContent = res.message || res.status || 'mislukt';
  }
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

async function repeaterAction(cmd, label, confirmMsg){
  if (!STATE.selectedRepeater) return;
  if (confirmMsg && !confirm(confirmMsg)) return;
  // Voer 'm uit alsof het een CLI-commando is, zodat de respons in dezelfde
  // history-lijst terechtkomt en je achteraf kunt zien wat er teruggekomen is.
  STATE.repeaterMgmt.cli_history.push({cmd: '['+label+'] ' + cmd, response: '…wachten…', ok: null});
  renderDetail();
  let res;
  try {
    res = await api('/admin/repeaters/cmd', {method:'POST',
      body: JSON.stringify({pubkey: STATE.selectedRepeater.pubkey, cmd: cmd})});
  } catch(e) {
    const h = STATE.repeaterMgmt.cli_history[STATE.repeaterMgmt.cli_history.length-1];
    h.response = '(fout)'; h.ok = false;
    renderDetail(); return;
  }
  const h = STATE.repeaterMgmt.cli_history[STATE.repeaterMgmt.cli_history.length-1];
  if (res.ok) {
    h.response = res.response || '(leeg / accepted)';
    h.ok = true;
  } else {
    h.response = res.error || res.message || res.status || 'fout';
    h.ok = false;
    if (res.status === 'not_logged_in') STATE.repeaterMgmt.logged_in = false;
  }
  renderDetail();
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
  STATE.repeaterMgmt.cli_history.push({cmd: cmd, response: '…wachten…', ok: null});
  renderDetail();
  let res;
  try {
    res = await api('/admin/repeaters/cmd', {method:'POST',
      body: JSON.stringify({pubkey: STATE.selectedRepeater.pubkey, cmd: cmd})});
  } catch(e) {
    const h = STATE.repeaterMgmt.cli_history[STATE.repeaterMgmt.cli_history.length-1];
    h.response = '(fout)'; h.ok = false;
    renderDetail(); return;
  }
  const h = STATE.repeaterMgmt.cli_history[STATE.repeaterMgmt.cli_history.length-1];
  if (res.ok) {
    h.response = res.response || '(leeg)';
    h.ok = true;
  } else {
    h.response = res.error || res.message || res.status || 'fout';
    h.ok = false;
    if (res.status === 'not_logged_in') STATE.repeaterMgmt.logged_in = false;
  }
  renderDetail();
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
    loginBlock =
      '<div class="detail-section"><h3>Login</h3>' +
        '<div class="row"><input id="rep-mgmt-pw" type="password" placeholder="admin-wachtwoord" style="flex:1" autocomplete="off">' +
        '<button onclick="repeaterLogin()">manage</button></div>' +
        '<div id="rep-mgmt-login-status" class="note" style="margin-top:6px;color:#c33"></div>' +
      '</div>';
  } else {
    loginBlock =
      '<div class="detail-section"><h3>Ingelogd</h3>' +
        '<div class="row"><button onclick="repeaterLogout()" class="sec">logout</button></div>' +
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
      // Outgoing: ack-status + latency
      const st = m.ack_status || 'sent';
      const stTxt = st === 'acked' ? '✓✓ bevestigd'
                  : st === 'failed' ? '!! mislukt'
                  : st === 'sent' ? '✓ verzonden (geen ack)'
                  : st;
      signalRow = '<div><span class="k">status:</span>'+escapeHTML(stTxt)+'</div>';
      if (typeof m.latency_s === 'number') {
        signalRow += '<div><span class="k">ack na:</span>'+m.latency_s.toFixed(2)+' s</div>';
      }
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
      ${renderPathSection(m)}
      <div class="detail-section">
        <h3>raw payload</h3>
        <div class="msg-quote" style="font-size:11px">${escapeHTML(typeof m.raw === 'string' ? m.raw : JSON.stringify(m.raw, null, 2))}</div>
      </div>`;
    return;
  }

  // 2a) Reports + repeater geselecteerd → manage-paneel
  if (STATE.view === 'reports' && STATE.reportSub === 'repeaters' && STATE.selectedRepeater) {
    el.innerHTML = renderRepeaterManage();
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
  if (/^@\[[^\]]+\]\s*/.test(cur)) {
    $('txt').value = cur.replace(/^@\[[^\]]+\]\s*/, prefix);
  } else {
    $('txt').value = prefix + cur;
  }
  $('txt').focus();
  // Cursor aan eind
  const v = $('txt').value;
  $('txt').setSelectionRange(v.length, v.length);
}

function _renderHop(seg){
  // seg = {hash, name?} of een raw hex-string (legacy)
  if (typeof seg === 'string') {
    return '<span class="hop hop-unknown" title="onbekende repeater">'+escapeHTML(seg)+'</span>';
  }
  const hash = seg.hash || '';
  if (seg.name) {
    return '<span class="hop hop-known" title="'+escapeHTML(hash)+'">'+escapeHTML(seg.name)+'</span>';
  }
  return '<span class="hop hop-unknown" title="onbekende repeater">'+escapeHTML(hash)+'</span>';
}

function _renderOnePath(p, opts){
  opts = opts || {};
  const pl = p.path_len;
  const rssi = (typeof p.rssi === 'number') ? (p.rssi+' dBm') : '?';
  const snr  = (typeof p.snr  === 'number') ? (p.snr.toFixed(2)+' dB') : '?';
  const summary = (pl===255?0:pl) + ' hop' + (pl===1?'':'s') + ' · ' + rssi + ' · SNR ' + snr;

  // Bouw hop-chain: gebruik path_names als beschikbaar, anders ruwe path-hex
  let chainHtml;
  if (pl === 0 || pl === 255) {
    chainHtml = '<i style="color:#888">direct (0 hops)</i> <span class="hop-arrow">&rarr;</span> <span class="hop hop-self">jij</span>';
  } else if (Array.isArray(p.path_names) && p.path_names.length > 0) {
    chainHtml = p.path_names.map(_renderHop).join('<span class="hop-arrow">&rarr;</span>') +
                '<span class="hop-arrow">&rarr;</span><span class="hop hop-self">jij</span>';
  } else if (typeof p.path === 'string' && p.path.length > 0) {
    const hashSize = p.path_hash_size || 1;
    const segs = [];
    for (let i = 0; i + hashSize*2 <= p.path.length; i += hashSize*2) {
      segs.push(p.path.substring(i, i+hashSize*2));
    }
    chainHtml = segs.map(_renderHop).join('<span class="hop-arrow">&rarr;</span>') +
                '<span class="hop-arrow">&rarr;</span><span class="hop hop-self">jij</span>';
  } else {
    chainHtml = '<i style="color:#888">'+pl+' hop(s), pad-bytes niet meegestuurd</i>';
  }

  const openAttr = opts.open ? ' open' : '';
  return '<details class="path-row"'+openAttr+'>' +
         '<summary>'+summary+'</summary>' +
         '<div class="hop-chain">'+chainHtml+'</div>' +
         '</details>';
}

function renderPathSection(m){
  if (!m || m.kind !== 'channel' || m.direction === 'out') return '';

  let raw = m.raw;
  if (typeof raw === 'string') {
    try { raw = JSON.parse(raw); } catch(e) { raw = null; }
  }
  if (!raw || typeof raw !== 'object') {
    return '<div class="detail-section"><h3>Pad</h3>' +
      '<div class="kv"><div><i>geen path-info in payload</i></div></div></div>';
  }

  // Multi-path: paths-array met alle ontvangsten van dezelfde msg via
  // verschillende routes (zoals "Heard X Times" in de Android-app).
  if (Array.isArray(raw.paths) && raw.paths.length > 0) {
    // Sorteer op kortste pad eerst (best signal als tie-breaker)
    const sorted = raw.paths.slice().sort((a,b) => {
      const al = (typeof a.path_len === 'number') ? a.path_len : 999;
      const bl = (typeof b.path_len === 'number') ? b.path_len : 999;
      if (al !== bl) return al - bl;
      const sa = (typeof a.snr === 'number') ? a.snr : -999;
      const sb = (typeof b.snr === 'number') ? b.snr : -999;
      return sb - sa;
    });
    const header = sorted.length > 1
      ? ('Paden — gehoord ' + sorted.length + 'x')
      : 'Pad';
    // Eerste (= kortste) standaard open, rest dicht
    const rendered = sorted.map((p, i) => _renderOnePath(p, {open: i === 0})).join('');
    return '<div class="detail-section"><h3>'+header+'</h3>' +
           rendered + '</div>';
  }

  // path_len: 255 = "direct" (geen mesh-hops). Anders = aantal repeaters.
  let pathLen = raw.path_len;
  let directFlag = (pathLen === 255);
  if (directFlag) pathLen = 0;

  if (typeof pathLen !== 'number') {
    return '<div class="detail-section"><h3>Pad</h3>' +
      '<div class="kv"><div><i>path_len niet aanwezig in payload</i></div></div></div>';
  }

  if (pathLen === 0) {
    return '<div class="detail-section"><h3>Pad</h3>' +
      '<div class="kv"><div><i>' + (directFlag ? 'direct (geen tussen-hops)' : '0 hops') +
      '</i></div></div></div>';
  }

  // Hash-size afleiden — eerst expliciet veld, anders uit path/path_len.
  let hashSize = null;
  if (typeof raw.path_hash_size === 'number' && raw.path_hash_size > 0) {
    hashSize = raw.path_hash_size;
  } else if (typeof raw.path_hash_mode === 'number' && raw.path_hash_mode >= 0) {
    hashSize = raw.path_hash_mode + 1;
  } else if (typeof raw.path === 'string' && pathLen > 0) {
    const totalNibbles = raw.path.length;
    if (totalNibbles % (pathLen*2) === 0) {
      hashSize = totalNibbles / (pathLen*2);
    }
  }

  const path = raw.path;
  let segments;
  let extra = '';
  if (typeof path === 'string' && hashSize && path.length >= hashSize*2) {
    const segs = [];
    for (let i = 0; i + hashSize*2 <= path.length; i += hashSize*2) {
      segs.push('<code style="background:#f0f0f0;padding:2px 4px;border-radius:3px">'+escapeHTML(path.substring(i, i+hashSize*2))+'</code>');
    }
    segments = segs.join(' &rarr; ') + ' &rarr; <b>jij</b>';
    extra = '<div class="note" style="margin-top:4px">hash-size: '+hashSize+' byte(s)</div>';
  } else if (typeof path === 'string' && path.length > 0) {
    segments = '<code style="background:#f0f0f0;padding:2px 4px;border-radius:3px">'+escapeHTML(path)+'</code>';
    extra = '<div class="note" style="margin-top:4px">pad-bytes ongesplitst (geen hash-size bekend)</div>';
  } else {
    segments = '<i>'+pathLen+' hop(s), pad-hashes niet meegestuurd</i>' +
               '<div class="note" style="margin-top:4px">' +
               'Tip: zorg dat decrypt-channel-logs aan staat (gateway-log toont "[*] decrypt-channel-logs aan").</div>';
  }

  let attemptInfo = '';
  if (typeof raw.attempt === 'number') {
    attemptInfo = '<div class="note" style="margin-top:4px">attempt: '+raw.attempt+'</div>';
  }

  return '<div class="detail-section"><h3>Pad ('+pathLen+' hop'+(pathLen===1?'':'s')+')</h3>' +
         '<div style="font-size:11px;line-height:1.7">'+segments+'</div>' +
         extra + attemptInfo + '</div>';
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
    const [s, c, mc] = await Promise.all([
      api('/admin/state'),
      api('/contacts').catch(() => []),
      api('/my/contacts').catch(() => []),
    ]);
    STATE.status = s;
    STATE.channels = s.channels;
    STATE.contacts = c;
    STATE.myContacts = mc;
    renderTree();
    renderHeaderStatus();
    // Bewust geen renderAdmin/Reports/Contacts hier — die rerenderen
    // zou form-inputs wissen tijdens typen. Sub-views verversen alleen
    // bij user-actie of bij explicit klik in tree.
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
  // Filter: server-side search door alle berichten (gedebounced)
  const fi = $('chat-filter');
  if (fi) fi.addEventListener('input', () => {
    STATE.filterText = fi.value.trim().toLowerCase();
    if (_searchTimer) clearTimeout(_searchTimer);
    _searchTimer = setTimeout(() => runServerSearch(STATE.filterText), 300);
  });
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

// Wordt aangeroepen door admin-actions (save/edit/delete) waar de zichtbare
// tabel wel direct moet updaten. Verschilt van refresh(): die wordt ook
// periodiek aangeroepen en mag NIET rerenderen want dat wist form-inputs.
async function refreshAndRerender(){
  await refresh();
  if (STATE.view === 'admin')     renderAdmin();
  if (STATE.view === 'reports')   renderReports();
  if (STATE.view === 'contacts')  renderContactsManager();
}
