
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

  // 2a) Admin → Repeaters + repeater geselecteerd → manage-paneel
  // (Repeaters is in v1.1.030 verplaatst van Rapportages naar Admin.)
  if (STATE.view === 'admin' && STATE.adminSub === 'repeaters' && STATE.selectedRepeater) {
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
  // Markeer als reply zodat send-handler parent_id meestuurt voor expliciete
  // (DB-persisted) thread-koppeling. De @[..] prefix is voor backward-compat
  // met clients die alleen mentions kennen — die kunnen 'm dan ook detecteren.
  STATE.replyTo = {id: m.id, sender: sender};
  renderReplyBanner();
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

function cancelReply(){
  STATE.replyTo = null;
  renderReplyBanner();
  // @[..] prefix uit input strippen (alleen als 'ie matched)
  const cur = $('txt').value;
  $('txt').value = cur.replace(/^@\[[^\]]+\]\s*/, '');
  $('txt').focus();
}

function renderReplyBanner(){
  let bar = document.getElementById('reply-banner');
  if (!STATE.replyTo) {
    if (bar) bar.remove();
    return;
  }
  const form = $('chat-form');
  if (!form) return;
  if (!bar) {
    bar = document.createElement('div');
    bar.id = 'reply-banner';
    form.parentNode.insertBefore(bar, form);
  }
  bar.innerHTML = '<span class="rb-label">↳ reply op</span> <b>' +
    escapeHTML(STATE.replyTo.sender) + '</b>' +
    ' <button type="button" class="rb-cancel" onclick="cancelReply()" title="annuleer reply">×</button>';
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

