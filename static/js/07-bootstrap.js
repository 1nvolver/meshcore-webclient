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
    // Idem voor het repeater-manage-paneel in het detail (admin →
    // Repeaters → repeater geselecteerd): dat heeft password-input,
    // CLI-input en een gescrollde history die we niet willen resetten
    // elke 30s. Dat paneel is event-driven (login/status/cli) en kent
    // geen periodieke server-data. v1.1.044.
    if (!(STATE.view === 'admin' && STATE.adminSub === 'repeaters' && STATE.selectedRepeater)) {
      renderDetail();
    }
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
    updateCallsignMenuLabel();
    updateThreadingMenuLabel();
    if (STATE.me.role === 'admin') {
      $('menu-quit').style.display = '';
      // Toon admin-groep in tree (was display:none in HTML)
      const adm = $('grp-admin');
      if (adm) adm.style.display = '';
      // Privé-kanaal-Beheren is admin-only. (v1.1.038: '+ privé-kanaal'
      // shortcut weggehaald — zit al onder Beheren.)
      const privMgr = $('li-priv-mgr');
      if (privMgr) privMgr.style.display = '';
    } else {
      // Geen admin: verwijder de hele admin-groep uit de DOM
      const adm = $('grp-admin');
      if (adm) adm.parentNode.removeChild(adm);
      // Privé-kanaal-beheer items blijven verborgen (display:none in HTML).
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

/* ============== Mijn profiel modal (v1.1.043) ==============
   Submenu vanuit avatar-menu. Bundelt persoonlijke instellingen die eerder
   inline in het avatar-menu zaten (Callsign, Threading-indicators, Mijn QR,
   Wachtwoord) — avatar-menu zelf is nu compacter met alleen username +
   Mijn profiel + Logout (+ Quit voor admin). */
function showProfileModal(){
  const um = document.getElementById('user-menu');
  if (um) um.classList.remove('show');
  const me = STATE.me || {};
  const cs = me.callsign || '';
  const thOn = !!STATE.threadingEnabled;
  openModal('Mijn profiel — ' + (me.username || ''),
    '<p class="note">Beheer je persoonlijke instellingen. Klik op een item om te wijzigen.</p>' +
    '<ul class="profile-actions">' +
      '<li onclick="changeCallsignPrompt()">' +
        '<span class="profile-action-label">Callsign</span>' +
        '<span class="profile-action-value">' + (cs ? escapeHTML(cs) : '(uit)') + ' ›</span>' +
      '</li>' +
      '<li onclick="_profileToggleThreading()">' +
        '<span class="profile-action-label">Threading-indicators</span>' +
        '<span class="profile-action-value">' + (thOn ? 'aan' : 'uit') + ' ⇄</span>' +
      '</li>' +
      '<li onclick="showMyQR()">' +
        '<span class="profile-action-label">Mijn QR (deel contact)</span>' +
        '<span class="profile-action-value">›</span>' +
      '</li>' +
      '<li onclick="changePasswordPrompt()">' +
        '<span class="profile-action-label">Wachtwoord wijzigen</span>' +
        '<span class="profile-action-value">›</span>' +
      '</li>' +
    '</ul>' +
    '<div class="modal-actions">' +
      '<button type="button" class="primary" onclick="closeModal()">Sluit</button>' +
    '</div>'
  );
}

/* Threading-toggle vanuit de profiel-modal: state flippen + modal hertekenen
   zodat de "aan/uit"-label klopt. Modal sluit en heropent — kleine flikkering
   maar simpeler dan in-place DOM-update. */
function _profileToggleThreading(){
  toggleThreadingPref();
  showProfileModal();
}

function changePasswordPrompt(){
  // v1.1.043: HTML-modal ipv prompt(). Eerste-login-flow blijft prompt
  // (forcedChangePasswordPrompt — niet sluitbaar, andere semantiek).
  openModal('Wachtwoord wijzigen',
    '<p>Voer huidig en nieuw wachtwoord in. Minimaal 6 tekens.</p>' +
    '<div class="row" style="align-items:center;gap:8px;margin-top:8px">' +
      '<label style="flex:0;min-width:120px">Huidig</label>' +
      '<input id="cpw-old" type="password" autocomplete="current-password" style="flex:1;font:inherit;padding:8px 10px;border:1px solid #ccc;border-radius:4px;min-height:40px;font-size:16px">' +
    '</div>' +
    '<div class="row" style="align-items:center;gap:8px">' +
      '<label style="flex:0;min-width:120px">Nieuw</label>' +
      '<input id="cpw-new1" type="password" autocomplete="new-password" minlength="6" style="flex:1;font:inherit;padding:8px 10px;border:1px solid #ccc;border-radius:4px;min-height:40px;font-size:16px">' +
    '</div>' +
    '<div class="row" style="align-items:center;gap:8px">' +
      '<label style="flex:0;min-width:120px">Nogmaals</label>' +
      '<input id="cpw-new2" type="password" autocomplete="new-password" minlength="6" style="flex:1;font:inherit;padding:8px 10px;border:1px solid #ccc;border-radius:4px;min-height:40px;font-size:16px">' +
    '</div>' +
    '<div class="modal-actions">' +
      '<button type="button" onclick="closeModal()">Annuleer</button>' +
      '<button type="button" class="primary" onclick="_cpwSave()">Opslaan</button>' +
    '</div>'
  );
  // Focus op huidig-veld; Enter in laatste veld = opslaan
  const oldEl = document.getElementById('cpw-old');
  const new2El = document.getElementById('cpw-new2');
  if (oldEl) oldEl.focus();
  if (new2El) {
    new2El.addEventListener('keydown', (e) => {
      if (e.key === 'Enter') { e.preventDefault(); _cpwSave(); }
    });
  }
}

async function _cpwSave(){
  const oldp  = (document.getElementById('cpw-old') || {}).value || '';
  const newp  = (document.getElementById('cpw-new1') || {}).value || '';
  const newp2 = (document.getElementById('cpw-new2') || {}).value || '';
  if (!oldp || !newp || !newp2) { toast('alle velden invullen', 'err'); return; }
  if (newp !== newp2) { toast('nieuwe wachtwoorden komen niet overeen', 'err'); return; }
  if (newp.length < 6) { toast('min 6 tekens', 'err'); return; }
  try {
    const r = await api('/change-password', {method:'POST', body:JSON.stringify({old:oldp, new:newp})});
    toast(r.message || 'ok', 'ok');
    closeModal();
  } catch(e) {
    // toast via api(); modal blijft open
  }
}

function changeCallsignPrompt(){
  // v1.1.041: HTML-modal ipv prompt(). Desktop Chrome biedt geen emoji-picker
  // in prompt(); modal toont een inline emoji-row die in de input invoegt,
  // plus een live voorbeeld '[XX] tekst'. Hergebruikt EMOJIS-array uit
  // 03-chat.js (impliciet globaal want plain scripts).
  // Sluit avatar-menu zodat 't niet over de modal valt.
  const menu = document.getElementById('user-menu');
  if (menu) menu.classList.remove('show');
  const cur = (STATE.me && STATE.me.callsign) || '';
  // EMOJIS is een global uit 03-chat.js; defensief naar lege array fallen
  // mocht volgorde ooit kapot gaan.
  const emojis = (typeof EMOJIS !== 'undefined' && Array.isArray(EMOJIS)) ? EMOJIS : [];
  const emojiButtons = emojis.map(em =>
    '<button type="button" class="emoji-pick" data-em="' + escapeHTML(em) + '">' + em + '</button>'
  ).join('');
  openModal('Callsign instellen',
    '<p>Korte identifier (max 16 tekens, mag emoji bevatten) die vóór jouw uitgaande berichten komt als <code>[XX]</code>. Leeg laten = uit.</p>' +
    '<div class="row" style="align-items:center;gap:8px">' +
      '<label style="flex:0;min-width:90px">Callsign</label>' +
      '<input id="cs-input" type="text" maxlength="32" value="' + escapeHTML(cur) + '" style="flex:1;font:inherit;padding:8px 10px;border:1px solid #ccc;border-radius:4px;min-height:40px;font-size:16px" autocomplete="off">' +
    '</div>' +
    '<div class="note" style="margin-top:8px">Voorbeeld: <code id="cs-preview"></code></div>' +
    (emojiButtons
      ? '<div class="note" style="margin-top:12px">Emoji invoegen:</div>' +
        '<div id="cs-emoji-row" class="cs-emoji-row">' + emojiButtons + '</div>'
      : ''
    ) +
    '<div class="modal-actions">' +
      '<button type="button" onclick="_csClear()">Wissen (uit)</button>' +
      '<button type="button" class="primary" onclick="_csSave()">Opslaan</button>' +
    '</div>'
  );
  // Event-handlers na render: emoji-row → invoegen in cs-input; preview live.
  const input = document.getElementById('cs-input');
  const preview = document.getElementById('cs-preview');
  function updatePreview(){
    const v = (input.value || '').trim();
    preview.textContent = v ? ('[' + v + '] Hallo!') : '(geen prefix — uit)';
  }
  input.addEventListener('input', updatePreview);
  updatePreview();
  // Focus + cursor naar eind voor snel typen
  input.focus();
  try { input.setSelectionRange(input.value.length, input.value.length); } catch(e) {}
  // Emoji-row klik → append in input
  const row = document.getElementById('cs-emoji-row');
  if (row) {
    row.addEventListener('click', (e) => {
      const btn = e.target.closest('button.emoji-pick');
      if (!btn) return;
      const em = btn.dataset.em || btn.textContent;
      const start = input.selectionStart ?? input.value.length;
      const end   = input.selectionEnd   ?? input.value.length;
      input.value = input.value.substring(0, start) + em + input.value.substring(end);
      const pos = start + em.length;
      input.focus();
      try { input.setSelectionRange(pos, pos); } catch(e) {}
      updatePreview();
    });
  }
  // Enter in input → opslaan
  input.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') { e.preventDefault(); _csSave(); }
  });
}

async function _csSave(){
  const input = document.getElementById('cs-input');
  if (!input) return;
  const cs = (input.value || '').trim();
  try {
    const r = await api('/me/callsign', {method:'POST', body:JSON.stringify({callsign: cs})});
    if (STATE.me) STATE.me.callsign = r.callsign || '';
    updateCallsignMenuLabel();
    toast(r.message || 'ok', 'ok');
    closeModal();
  } catch(e) {
    // api() heeft al getoast — modal blijft open zodat user 't kan corrigeren.
  }
}

function _csClear(){
  const input = document.getElementById('cs-input');
  if (input) {
    input.value = '';
    input.dispatchEvent(new Event('input'));  // update preview
    input.focus();
  }
}

function updateCallsignMenuLabel(){
  const el = document.getElementById('menu-callsign');
  if (!el) return;
  const cs = (STATE.me && STATE.me.callsign) || '';
  el.textContent = cs ? ('Callsign: ' + cs + ' (wijzig…)') : 'Callsign instellen…';
}

function updateThreadingMenuLabel(){
  const el = document.getElementById('menu-threading');
  if (!el) return;
  el.textContent = 'Threading-indicators: ' + (STATE.threadingEnabled ? 'aan' : 'uit');
}

function toggleThreadingPref(){
  setThreadingEnabled(!STATE.threadingEnabled);
  updateThreadingMenuLabel();
  // Verberg/toon alle badges meteen + verlaat thread-view als die actief is
  if (typeof refreshAllThreadBadges === 'function') refreshAllThreadBadges();
  if (!STATE.threadingEnabled && STATE.threadFilter && typeof exitThreadView === 'function') {
    exitThreadView();
  }
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
  if (STATE.view === 'admin') {
    // Repeaters-subview gebruikt renderReportRepeaters (target: #reports-view).
    if (STATE.adminSub === 'repeaters') renderReportRepeaters();
    else                                renderAdmin();
  }
  if (STATE.view === 'reports')   renderReports();
  if (STATE.view === 'contacts')  renderContactsManager();
  if (STATE.view === 'privchans') renderPrivChansManager();
}
