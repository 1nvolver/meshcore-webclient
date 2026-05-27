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
    updateCallsignMenuLabel();
    updateThreadingMenuLabel();
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

async function changeCallsignPrompt(){
  // Prompt met huidige waarde als default. Leeg = uitzetten.
  const cur = (STATE.me && STATE.me.callsign) || '';
  const v = prompt('Callsign (3 tekens of emoji; leeg = uit):', cur);
  if (v === null) return;  // cancel
  const cs = v.trim();
  try {
    const r = await api('/me/callsign', {method:'POST', body:JSON.stringify({callsign: cs})});
    if (STATE.me) STATE.me.callsign = r.callsign || '';
    updateCallsignMenuLabel();
    toast(r.message || 'ok', 'ok');
  } catch(e){}
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
}
