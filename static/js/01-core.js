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
  // Threading-state (zie HANDOFF v1.1.033 + 034)
  replyTo: null,             // {id, sender} van msg waarop volgende send reply is, of null
  threadFilter: null,        // root-msg-id als thread-view actief is; null = vlakke chat
  msgs: [],                  // alle msgs in huidige view (root-array voor reply-tree)
  replyCounts: {},           // {rootId: totale-descendant-count} — opnieuw berekend bij elke addMsg
  threadingEnabled: (function(){
    try {
      const v = localStorage.getItem('threading_on');
      return v === null ? true : v === '1';
    } catch(e) { return true; }
  })(),
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

/* ============== mobile drawer + detail-overlay ==============
   Op mobile (≤767px) is .pane.tree een off-screen drawer (transform).
   Op tablet+mobile is .pane.detail een off-screen overlay.
   We togglen body-classes 'drawer-open' en 'detail-open'; CSS doet de
   rest. Helpers zijn no-ops op desktop omdat de classes geen visueel
   effect hebben buiten de media queries. */
function toggleMobileDrawer(){
  const open = document.body.classList.toggle('drawer-open');
  $('mobile-backdrop').classList.toggle('show', open);
}
function closeMobileDrawer(){
  document.body.classList.remove('drawer-open');
  // backdrop alleen verbergen als ook detail dicht is — anders blijft hij
  // staan voor de detail-overlay.
  if (!document.body.classList.contains('detail-open')) {
    $('mobile-backdrop').classList.remove('show');
  }
}
/* Eén handler voor de backdrop: sluit alles wat open is. */
function closeMobileOverlays(){
  closeMobileDrawer();
  closeMobileDetail();
}
/* Nav-acties op mobile sluiten de drawer automatisch (anders zit hij in
   de weg na keuze). Op desktop is dit een no-op want body-class heeft
   geen visueel effect. */
function _isMobileViewport(){ return window.matchMedia('(max-width:767px)').matches; }
function _maybeCloseDrawerOnNav(){ if (_isMobileViewport()) closeMobileDrawer(); }

/* Detail-overlay (tablet en mobile). Op desktop blijft .pane.detail in
   de flex-flow staan; deze helpers togglen alleen de body-class, dus
   geen effect daar. */
function _isOverlayDetailViewport(){ return window.matchMedia('(max-width:1199px)').matches; }
function openMobileDetail(){
  if (!_isOverlayDetailViewport()) return;
  // Niets te tonen? Niet openen — voorkomt lege overlay op kanaal-switch.
  if (!STATE.selectedMsg && !STATE.selectedRepeater) return;
  document.body.classList.add('detail-open');
  $('mobile-backdrop').classList.add('show');
}
function closeMobileDetail(){
  document.body.classList.remove('detail-open');
  if (!document.body.classList.contains('drawer-open')) {
    $('mobile-backdrop').classList.remove('show');
  }
}

async function quitApp(){
  if (!confirm('De gateway helemaal afsluiten? CLI en Web stoppen beide.')) return;
  try {
    await api('/admin/quit', {method:'POST', body:'{}'});
    toast('Gateway sluit af…', 'ok');
    setTimeout(()=>document.body.innerHTML='<p style="padding:40px;font-family:system-ui">Gateway is afgesloten.</p>', 1500);
  } catch(e){}
}

/* ============== threading helpers ==============
   Hybride model:
   - Expliciet: msg.parent_id gezet door Reply-knop (persisted in DB).
   - Heuristisch: msg-tekst begint met '@[NAAM]' → koppel aan meest recente
     msg van NAAM binnen 30 min ervoor in dezelfde view.
   buildReplyTree() loopt door alle msgs in volgorde, vult inferredParent in
   en bouwt replyCounts (totale descendants per root).
*/
function extractMentionTarget(text){
  if (!text) return null;
  const m = String(text).match(/^@\[([^\]]+)\]\s*/);
  return m ? m[1] : null;
}

function _msgTs(m){
  // Robust timestamp -> milliseconds. Geeft 0 terug als unparsable (vist altijd uit window).
  const t = m && m.ts;
  if (!t) return 0;
  const d = new Date(t);
  return isNaN(d.getTime()) ? 0 : d.getTime();
}

function _msgSender(m){
  // Voor mention-match willen we de getoonde sender-naam. Pas op: stripNamePrefix
  // wordt elders gezet als ._sender; gebruik die als beschikbaar.
  if (m && m._sender) return m._sender;
  // Fallback: extractSender-equivalent. Voor channel-in: parse '<naam>: ...'.
  if (m && m.kind === 'channel' && m.direction === 'in') {
    const raw = m.text || '';
    const colon = raw.indexOf(':');
    if (colon > 0) return raw.slice(0, colon).trim();
  }
  return (m && m.peer) || null;
}

const THREAD_WINDOW_MS = 30 * 60 * 1000;  // 30 minuten

function buildReplyTree(msgs){
  /* Loopt chronologisch (msgs verwacht oud→nieuw of nieuw→oud — sort even):
     Voor elke msg met null parent_id, check of tekst begint met @[X];
     zo ja, zoek meest recente msg met sender X binnen 30 min ervoor
     (in dezelfde channel/dm-view). Sla heuristic-parent op als _inferredParent
     (niet als parent_id, want dat is alleen voor DB-persisted waardes).
     Bouw daarna replyCounts: root → totale descendant-count.
  */
  if (!Array.isArray(msgs) || msgs.length === 0) {
    STATE.replyCounts = {};
    return;
  }
  // Sorteer kopie chronologisch
  const arr = msgs.slice().sort((a,b) => _msgTs(a) - _msgTs(b));
  const byId = {};
  for (const m of arr) {
    if (m && m.id != null) byId[m.id] = m;
    m._inferredParent = null;
  }

  // Heuristische parent-inferrer
  for (let i = 0; i < arr.length; i++) {
    const m = arr[i];
    if (!m) continue;
    if (m.parent_id) continue;  // expliciet gezet — geen heuristiek nodig
    const stripped = stripNamePrefix(m);  // verwijdert eventueel 'sender: ' prefix
    const target = extractMentionTarget(stripped);
    if (!target) continue;
    const cutoff = _msgTs(m) - THREAD_WINDOW_MS;
    // Loop terug tot kandidaat gevonden
    for (let j = i - 1; j >= 0; j--) {
      const c = arr[j];
      if (_msgTs(c) < cutoff) break;
      if (_msgSender(c) === target) {
        m._inferredParent = c.id;
        break;
      }
    }
  }

  // Reply-counts: descendants per root. Loop bottom-up: voor elke msg met parent
  // (explicit of inferred), tel +1 bij parent én bij root.
  const counts = {};
  function rootOf(m){
    // Wandel parent-chain naar boven; bescherming tegen cycli (max 50 hops).
    let cur = m, hops = 0;
    while (cur && hops < 50) {
      const pid = cur.parent_id || cur._inferredParent;
      if (!pid) return cur.id;
      const p = byId[pid];
      if (!p) return cur.id;  // dangling parent — behandel als root
      cur = p; hops++;
    }
    return cur.id;
  }
  for (const m of arr) {
    const pid = m.parent_id || m._inferredParent;
    if (!pid) continue;  // root, geen reply
    const root = rootOf(m);
    if (root != null && root !== m.id) {
      counts[root] = (counts[root] || 0) + 1;
    }
  }
  STATE.replyCounts = counts;
}

function isInThread(m, rootId){
  // Zit msg m in de thread van rootId? (Inclusief root zelf.)
  if (!rootId || !m) return false;
  if (m.id === rootId) return true;
  let cur = m, hops = 0;
  while (cur && hops < 50) {
    const pid = cur.parent_id || cur._inferredParent;
    if (!pid) return false;
    if (pid === rootId) return true;
    // Zoek parent-msg in STATE.msgs (lineaire scan; fine voor typische sizes)
    cur = (STATE.msgs || []).find(x => x.id === pid);
    hops++;
  }
  return false;
}

function setThreadingEnabled(on){
  STATE.threadingEnabled = !!on;
  try { localStorage.setItem('threading_on', on ? '1' : '0'); } catch(e){}
}

