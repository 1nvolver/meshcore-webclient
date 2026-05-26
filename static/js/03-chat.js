
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

