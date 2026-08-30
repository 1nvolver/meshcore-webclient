/* ============== QR import/export (v1.1.035) ==============
   QR-genereren via qrcode-generator (vendor); decoden via jsQR (vendor).
   Sinds v1.1.041 gebruikt deze module de generieke `openModal`/`closeModal`
   helpers uit 01-core.js (eerder lokale `_qrOpenModal`/`_qrCloseModal`).

   Export:  toont QR van eigen card (`GET /contacts/export`) in modal.
            Self-serve via avatar-menu (geen admin-rechten nodig).
   Import:  modal met camera-scan + foto-upload; verstuurt hex naar
            `POST /contacts/import` (admin-only endpoint). Hex-validatie
            client-side voor snelle feedback bij verkeerd type QR. */

let _qrScanState = null;  // {stream, video, canvas, ctx, raf, active} of null

/* Wrapper rond closeModal die ook actieve camera-scan stopt. */
function _qrCloseAndStopScan(){
  if (_qrScanState) _qrStopScan();
  closeModal();
}

/* ============== QR rendering naar canvas ==============
   typeNumber=0 → auto-grootte op basis van data-lengte.
   Error-correction 'M' (~15% restore) — voldoende voor scherm-display +
   ruimte voor optionele toekomstige logo-overlay. */
function _qrRenderToCanvas(canvas, text){
  const qr = qrcode(0, 'M');
  qr.addData(text);
  qr.make();
  const moduleCount = qr.getModuleCount();
  const cellSize = 4;
  const margin = 4 * cellSize;
  const size = moduleCount * cellSize + margin * 2;
  canvas.width = size;
  canvas.height = size;
  const ctx = canvas.getContext('2d');
  ctx.fillStyle = '#fff';
  ctx.fillRect(0, 0, size, size);
  ctx.fillStyle = '#000';
  for (let r = 0; r < moduleCount; r++) {
    for (let c = 0; c < moduleCount; c++) {
      if (qr.isDark(r, c)) {
        ctx.fillRect(margin + c * cellSize, margin + r * cellSize, cellSize, cellSize);
      }
    }
  }
}

/* ============== Mijn QR (export eigen contact) ==============
   Sinds v1.1.037: backend geeft officieel formaat
   'meshcore://contact/add?name=...&public_key=...&type=...' (zie
   docs.meshcore.io/qr_codes/). MeshCore Android-app scant dit ook. */
async function showMyQR(){
  // Sluit avatar-menu zodat 't niet over de modal valt.
  const menu = document.getElementById('user-menu');
  if (menu) menu.classList.remove('show');

  let resp;
  try {
    resp = await api('/contacts/export');  // geen key = eigen card
  } catch(e) { return; }  // api() heeft al getoast
  const uri = (resp && resp.uri) || '';
  if (!uri) {
    toast('Geen card-data van companion ontvangen', 'err');
    return;
  }
  const myName = (resp && resp.name) || (STATE.status && STATE.status.node && STATE.status.node.name) || '';
  const copyEsc = uri.replace(/'/g, "\\'");
  openModal('Mijn contact-card',
    '<p>Laat iemand anders deze QR scannen — werkt met de MeshCore Android-app en deze web-client.</p>' +
    '<div class="qr-display"><canvas id="qr-export-canvas"></canvas></div>' +
    '<p class="note">Node: <b>' + escapeHTML(myName) + '</b><br>' +
    'URL (' + uri.length + ' chars): <code style="font-size:11px;word-break:break-all">' +
    escapeHTML(uri) + '</code></p>' +
    '<div class="modal-actions">' +
      '<button type="button" onclick="_qrCopyCard(\'' + copyEsc + '\')">Kopieer URL</button>' +
      '<button type="button" class="primary" onclick="_qrCloseAndStopScan()">Sluit</button>' +
    '</div>'
  );
  try {
    _qrRenderToCanvas(document.getElementById('qr-export-canvas'), uri);
  } catch(e) {
    toast('QR genereren faalde: ' + (e.message || e), 'err');
  }
}

async function _qrCopyCard(card){
  try {
    await navigator.clipboard.writeText(card);
    toast('Hex gekopieerd naar klembord', 'ok');
  } catch(e) {
    toast('Kopiëren niet ondersteund (browser of niet-HTTPS)', 'err');
  }
}

/* ============== Import contact via QR ==============
   Twee paden:
   1) Live camera-scan via getUserMedia + jsQR — werkt alleen op HTTPS/localhost.
   2) Foto-upload — werkt overal; geen permissies nodig. */
function showImportQR(){
  const supportsCamera = !!(navigator.mediaDevices && navigator.mediaDevices.getUserMedia);
  const camBtn = supportsCamera
    ? '<button type="button" class="primary" onclick="_qrStartScan()">Camera scannen</button>'
    : '<span class="note">Camera niet beschikbaar (vereist HTTPS of localhost).</span>';
  openModal('Contact importeren via QR',
    '<p>Scan met de camera, of upload een afbeelding met de QR-code.</p>' +
    '<div id="qr-scan-area" style="display:none">' +
      '<div class="qr-scan-wrap"><video id="qr-scan-video" playsinline muted></video></div>' +
      '<div class="qr-scan-status" id="qr-scan-status">Camera starten…</div>' +
    '</div>' +
    '<div id="qr-import-actions" class="modal-actions">' +
      camBtn +
      '<button type="button" onclick="document.getElementById(\'qr-file-input\').click()">Foto uploaden</button>' +
      '<input id="qr-file-input" type="file" accept="image/*" style="display:none" onchange="_qrHandleFile(event)">' +
      '<button type="button" onclick="_qrCloseAndStopScan()">Annuleer</button>' +
    '</div>'
  );
}

async function _qrStartScan(isChannel){
  // Idempotent: als een scan al draait, niets doen (voorkomt dat een 2e klik
  // op 'Camera scannen' een nieuwe getUserMedia-call doet — die call geeft op
  // sommige Android-browsers een korte autofocus-flits, wat door user als
  // "lijkt op een foto maken" werd ervaren).
  if (_qrScanState && _qrScanState.active) return;

  const area = document.getElementById('qr-scan-area');
  const status = document.getElementById('qr-scan-status');
  const video = document.getElementById('qr-scan-video');
  if (!area || !status || !video) return;
  area.style.display = '';

  // Disable de scan-knop zolang scan loopt; user kan altijd Annuleer of modal-X.
  const actions = document.getElementById('qr-import-actions');
  if (actions) {
    actions.querySelectorAll('button.primary').forEach(b => { b.disabled = true; });
  }

  status.textContent = 'Camera-permissie vragen…';
  status.className = 'qr-scan-status';

  let stream;
  try {
    // Hogere ideal-resolutie helpt jsQR bij kleine QR's (typisch een
    // telefoonscherm dat een eindje van de camera staat). Browser kan
    // downscalen als hardware 't niet aankan — 'ideal' is een hint, geen eis.
    stream = await navigator.mediaDevices.getUserMedia({
      video: {
        facingMode: { ideal: 'environment' },
        width:  { ideal: 1280 },
        height: { ideal: 720 },
      },
      audio: false
    });
  } catch(e) {
    status.textContent = 'Camera-toegang geweigerd: ' + (e.message || e);
    status.className = 'qr-scan-status err';
    if (actions) actions.querySelectorAll('button.primary').forEach(b => { b.disabled = false; });
    return;
  }
  video.srcObject = stream;
  try { await video.play(); } catch(e) {}
  status.textContent = 'Zoeken naar QR-code… (richt op de QR)';
  status.className = 'qr-scan-status';

  const canvas = document.createElement('canvas');
  const ctx = canvas.getContext('2d', { willReadFrequently: true });
  _qrScanState = {
    stream: stream, video: video, canvas: canvas, ctx: ctx, raf: null,
    active: true, isChannel: !!isChannel, frames: 0, lastStatusFrame: 0,
  };

  function loop(){
    if (!_qrScanState || !_qrScanState.active) return;
    // readyState >=2 (HAVE_CURRENT_DATA) + videoWidth>0 is robuuster dan
    // wachten op HAVE_ENOUGH_DATA (=4) — sommige browsers blijven op 3 hangen.
    if (video.readyState >= 2 && video.videoWidth > 0) {
      _qrScanState.frames++;
      canvas.width = video.videoWidth;
      canvas.height = video.videoHeight;
      ctx.drawImage(video, 0, 0, canvas.width, canvas.height);
      try {
        const img = ctx.getImageData(0, 0, canvas.width, canvas.height);
        // attemptBoth: probeer ook geïnverteerde QR (donker scherm met lichte
        // QR-modules of v.v. — Android-scherm-reflecties kunnen 'm omdraaien).
        // Iets duurder qua CPU maar op moderne devices vrijwel onmerkbaar bij
        // 1280x720@30fps.
        const code = jsQR(img.data, img.width, img.height, { inversionAttempts: 'attemptBoth' });
        if (code && code.data) {
          if (_qrScanState && _qrScanState.isChannel) _qrOnChannelDecoded(code.data);
          else _qrOnDecoded(code.data);
          return;
        }
      } catch(e) {}
      // Periodieke status-update zodat user ziet dat scan actief is.
      if (_qrScanState.frames - _qrScanState.lastStatusFrame >= 30) {
        _qrScanState.lastStatusFrame = _qrScanState.frames;
        status.textContent = 'Zoeken naar QR-code… (frames: ' + _qrScanState.frames + ', res: ' + video.videoWidth + '×' + video.videoHeight + ')';
      }
    } else {
      status.textContent = 'Wachten op camera-frames… (readyState=' + video.readyState + ')';
    }
    _qrScanState.raf = requestAnimationFrame(loop);
  }
  loop();
}

function _qrStopScan(){
  if (!_qrScanState) return;
  _qrScanState.active = false;
  if (_qrScanState.raf) cancelAnimationFrame(_qrScanState.raf);
  if (_qrScanState.stream) {
    _qrScanState.stream.getTracks().forEach(function(t){ t.stop(); });
  }
  _qrScanState = null;
}

function _qrHandleFile(ev, isChannel){
  const f = ev.target.files && ev.target.files[0];
  if (!f) return;
  const url = URL.createObjectURL(f);
  const img = new Image();
  img.onload = function(){
    const canvas = document.createElement('canvas');
    canvas.width = img.naturalWidth;
    canvas.height = img.naturalHeight;
    const ctx = canvas.getContext('2d');
    ctx.drawImage(img, 0, 0);
    URL.revokeObjectURL(url);
    try {
      const data = ctx.getImageData(0, 0, canvas.width, canvas.height);
      // attemptBoth = ook geïnverteerde QR (bv. lichte modules op donker).
      const code = jsQR(data.data, data.width, data.height, { inversionAttempts: 'attemptBoth' });
      if (code && code.data) {
        if (isChannel) _qrOnChannelDecoded(code.data);
        else _qrOnDecoded(code.data);
      } else {
        toast('Geen QR-code gevonden in afbeelding', 'err');
      }
    } catch(e) {
      toast('Decode-fout: ' + (e.message || e), 'err');
    }
  };
  img.onerror = function(){
    URL.revokeObjectURL(url);
    toast('Kon afbeelding niet laden', 'err');
  };
  img.src = url;
  // Reset input zodat dezelfde file opnieuw geselecteerd kan worden.
  ev.target.value = '';
}

/* ============== Privé-kanaal QR export ============== */
async function showChannelQR(slot){
  let resp;
  try {
    resp = await api('/admin/channels/' + encodeURIComponent(slot) + '/export');
  } catch(e) { return; }
  const uri = (resp && resp.uri) || '';
  if (!uri) {
    toast('Geen channel-data ontvangen', 'err');
    return;
  }
  const name = resp.name || ('slot ' + slot);
  const copyEsc = uri.replace(/'/g, "\\'");
  openModal('Privé-kanaal — ' + name,
    '<p>Scan deze QR om iemand anders aan dit privé-kanaal toe te voegen.</p>' +
    '<div class="qr-display"><canvas id="qr-channel-canvas"></canvas></div>' +
    '<p class="note">Kanaal: <b>' + escapeHTML(name) + '</b> (slot ' + slot + ')<br>' +
    'URL (' + uri.length + ' chars): <code style="font-size:11px;word-break:break-all">' +
    escapeHTML(uri) + '</code></p>' +
    '<div class="modal-actions">' +
      '<button type="button" onclick="_qrCopyCard(\'' + copyEsc + '\')">Kopieer URL</button>' +
      '<button type="button" class="primary" onclick="_qrCloseAndStopScan()">Sluit</button>' +
    '</div>'
  );
  try {
    _qrRenderToCanvas(document.getElementById('qr-channel-canvas'), uri);
  } catch(e) {
    toast('QR genereren faalde: ' + (e.message || e), 'err');
  }
}

/* ============== Privé-kanaal QR import ==============
   Hergebruikt de modal-flow van showImportQR, maar parsed channel-URI's en
   POST't naar /admin/channels/import. */
function showImportChannelQR(){
  const supportsCamera = !!(navigator.mediaDevices && navigator.mediaDevices.getUserMedia);
  const camBtn = supportsCamera
    ? '<button type="button" class="primary" onclick="_qrStartScan(true)">Camera scannen</button>'
    : '<span class="note">Camera niet beschikbaar (vereist HTTPS of localhost).</span>';
  openModal('Privé-kanaal importeren via QR',
    '<p>Scan een privé-kanaal-QR, of upload een afbeelding ervan.</p>' +
    '<div id="qr-scan-area" style="display:none">' +
      '<div class="qr-scan-wrap"><video id="qr-scan-video" playsinline muted></video></div>' +
      '<div class="qr-scan-status" id="qr-scan-status">Camera starten…</div>' +
    '</div>' +
    '<div id="qr-import-actions" class="modal-actions">' +
      camBtn +
      '<button type="button" onclick="document.getElementById(\'qr-file-input\').click()">Foto uploaden</button>' +
      '<input id="qr-file-input" type="file" accept="image/*" style="display:none" onchange="_qrHandleFile(event, true)">' +
      '<button type="button" onclick="_qrCloseAndStopScan()">Annuleer</button>' +
    '</div>'
  );
}

/* Modus-flag op _qrScanState: 'channel' true betekent dat decode → channel-import gaat. */
async function _qrOnChannelDecoded(text){
  const raw = String(text || '').trim();
  if (!raw) return;
  if (!raw.toLowerCase().startsWith('meshcore://channel/')) {
    toast('Dit is geen channel-QR (probeer DM → Contactpersonen voor contact-QR)', 'err');
    return;
  }
  _qrStopScan();
  try {
    const r = await api('/admin/channels/import', {
      method: 'POST',
      body: JSON.stringify({ uri: raw })
    });
    toast(r.message || 'Privé-kanaal geïmporteerd', 'ok');
    _qrCloseAndStopScan();
    if (typeof refreshAndRerender === 'function') refreshAndRerender();
    else if (typeof refresh === 'function') refresh();
  } catch(e) {
    // toast via api(); modal blijft open voor nieuwe poging
  }
}

async function _qrOnDecoded(text){
  // Sinds v1.1.037 accepteert backend drie formaten:
  //   1) 'meshcore://contact/add?name=...&public_key=...&type=...'  — officieel
  //   2) 'meshcore://channel/add?name=...&secret=<32hex>'           — channel (in showImportChannelQR)
  //   3) 'meshcore://<rawhex>' of blote hex                          — legacy
  // Hier (contact-flow): formaat 1 of 3. Channel-flow heeft eigen decoder.
  // Quick client-side detectie zodat we channel-QR's niet als contact-import sturen.
  const raw = String(text || '').trim();
  if (!raw) return;
  if (raw.toLowerCase().startsWith('meshcore://channel/')) {
    toast('Dit is een channel-QR, gebruik Privé → Beheren', 'err');
    return;
  }
  // Sanity: ofwel officieel contact-URL, ofwel raw hex.
  const isContactUrl = raw.toLowerCase().startsWith('meshcore://contact/add');
  if (!isContactUrl) {
    let hex = raw;
    if (hex.toLowerCase().startsWith('meshcore://')) hex = hex.slice('meshcore://'.length);
    if (!/^[0-9a-fA-F]+$/.test(hex) || hex.length < 16 || hex.length % 2 !== 0) {
      toast('QR bevat geen herkenbaar contact-formaat', 'err');
      return;
    }
  }
  _qrStopScan();
  let r;
  try {
    r = await api('/contacts/import', {
      method: 'POST',
      body: JSON.stringify({ uri: raw })
    });
  } catch(e) {
    // api() heeft al getoast — modal blijft open voor nieuwe poging.
    return;
  }
  toast(r.message || 'Contact geïmporteerd', 'ok');

  // Auto-add aan /my/contacts: alleen mogelijk als backend name+pubkey
  // teruggaf (= officieel URL-formaat). Voor legacy raw-hex import heeft de
  // backend die info niet en moet de user 'm zelf toevoegen via DM-Contactpersonen.
  if (r && r.name && r.public_key) {
    try {
      await api('/my/contacts/add', {
        method: 'POST',
        body: JSON.stringify({
          name: r.name, pubkey: r.public_key, notes: null,
        })
      });
      toast('Ook toegevoegd aan jouw contactpersonen', 'ok');
    } catch(e) {
      // Mogelijke fout: dubbel toevoegen. /my/contacts/add gooit een 409 in dat geval;
      // de toast van api() is voldoende. Geen modal-blokkering.
    }
  }

  _qrCloseAndStopScan();
  // Refresh: DM-tree, contactenlijst, evt. admin-contacten-view.
  if (typeof refreshAndRerender === 'function') refreshAndRerender();
  else if (typeof refresh === 'function') refresh();
}
