/* ============== tree rendering ==============
   Channel-split (v1.1.037): public + hashtag → grp-chat (#tree-channels);
   private → grp-private (#tree-private). Het 'Beheren…'-item bovenaan in
   grp-private blijft staan (zichtbaarheid wordt door loadMe gestuurd:
   admin-only).
*/
function renderTree(){
  const ulChat = $('tree-channels');
  const ulPriv = $('tree-private');
  ulChat.innerHTML = '';
  // grp-private: behoud li-priv-mgr en voeg channels eronder toe.
  if (ulPriv) {
    const mgr = $('li-priv-mgr');
    ulPriv.innerHTML = '';
    if (mgr) ulPriv.appendChild(mgr);
  }
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
    const li = makeChanLi({
      kind: c.kind, idx: c.idx, name: display, rawName: c.name,
    });
    if (c.kind === 'private' && ulPriv) {
      ulPriv.appendChild(li);
    } else {
      ulChat.appendChild(li);
    }
  });
  // Admin-sub-items markeren
  document.querySelectorAll('#grp-admin li[data-sub]').forEach(li => {
    li.classList.toggle('active', STATE.view === 'admin' && li.dataset.sub === STATE.adminSub);
  });
  // Reports-sub-items markeren
  document.querySelectorAll('#grp-reports li[data-report]').forEach(li => {
    li.classList.toggle('active', STATE.view === 'reports' && li.dataset.report === STATE.reportSub);
  });
  // Privé-Beheren-item active markeren
  const privMgr = $('li-priv-mgr');
  if (privMgr) privMgr.classList.toggle('active', STATE.view === 'privchans');
  // DM-tree
  renderDmTree();
}

function renderDmTree(){
  const ul = $('tree-dms');
  if (!ul) return;
  // v1.1.040: Contactpersonen-beheer-item ONDERAAN ipv bovenaan. Daarboven
  // verschijnen de feitelijke DM-contacten. Lege staat: italic placeholder
  // tussen DM-lijst en mgr-item.
  const mgr = $('li-contacts-mgr');
  ul.innerHTML = '';

  const my = STATE.myContacts || [];
  if (my.length === 0) {
    const placeholder = document.createElement('li');
    placeholder.style.color = '#bbb';
    placeholder.style.fontStyle = 'italic';
    placeholder.style.fontSize = '0.85em';
    placeholder.textContent = '(geen opgeslagen contacten)';
    ul.appendChild(placeholder);
  } else {
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

  if (mgr) {
    mgr.classList.toggle('active', STATE.view === 'contacts');
    // Visuele scheiding boven het Beheren-item.
    mgr.style.borderTop = '1px solid #eee';
    mgr.style.borderBottom = '';
    mgr.style.marginTop = '4px';
    mgr.style.marginBottom = '';
    ul.appendChild(mgr);
  }
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
  // DM-titel: alleen de naam. Pubkey-prefix verschijnt elders (detail-pane,
  // header van tree) — niet bovenin het chat-scherm.
  // Als ch.name leeg/identiek aan prefix is, probeer alsnog lookup.
  let title = ch.name;
  if (ch.kind === 'dm' && ch.peer && (!title || title === ch.peer)) {
    title = (typeof _resolveContactName === 'function' && _resolveContactName(ch.peer)) || ch.peer;
  }
  $('view-title').textContent = title;
  $('txt').placeholder = 'Bericht naar ' + title + '…';
  loadChatHistory();
  renderTree();
  renderDetail();
  _maybeCloseDrawerOnNav();
}
function _hideAllViews(){
  $('chat-view').style.display = 'none';
  $('admin-view').style.display = 'none';
  $('reports-view').style.display = 'none';
  const cv = $('contacts-view');
  if (cv) cv.style.display = 'none';
  const pv = $('privchans-view');
  if (pv) pv.style.display = 'none';
}

function selectAdminView(sub){
  if (!STATE.me || STATE.me.role !== 'admin') return;
  STATE.view = 'admin';
  STATE.adminSub = sub;
  _hideAllViews();
  const titles = {radio:'Radio', node:'Node', prefs:'Voorkeuren',
                  channels:'Channels', contacts:'Contacten', bots:'Bots',
                  housekeeping:'Housekeeping', users:'Gebruikers',
                  repeaters:'Repeaters'};
  $('view-title').textContent = 'Admin — ' + (titles[sub] || sub);
  if (sub === 'repeaters') {
    // Repeaters hergebruikt de reports-view container omdat de renderer
    // (renderReportRepeaters) op #reports-view target. Selectie resetten
    // zodat het detail-paneel niet meteen het manage-paneel toont.
    STATE.selectedRepeater = null;
    $('reports-view').style.display = 'block';
    renderReportRepeaters();
  } else {
    $('admin-view').style.display = 'block';
    renderAdmin();
  }
  renderTree();
  renderDetail();
  _maybeCloseDrawerOnNav();
}

function selectContactsManager(){
  STATE.view = 'contacts';
  _hideAllViews();
  $('contacts-view').style.display = 'block';
  $('view-title').textContent = 'DM — Contactpersonen';
  renderContactsManager();
  renderTree();
  renderDetail();
  _maybeCloseDrawerOnNav();
}

function selectPrivChansManager(){
  // Admin-only: backend /admin/channels/* endpoints zijn admin-only.
  if (!STATE.me || STATE.me.role !== 'admin') {
    toast('Privé-kanaal-beheer is een admin-actie', 'err');
    return;
  }
  STATE.view = 'privchans';
  _hideAllViews();
  $('privchans-view').style.display = 'block';
  $('view-title').textContent = 'Privé-kanalen — Beheren';
  renderPrivChansManager();
  renderTree();
  renderDetail();
  _maybeCloseDrawerOnNav();
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
      '<td data-label="Status · Naam">' + status + ' ' + escapeHTML(c.name || '?') + '</td>' +
      '<td data-label="Pubkey"><code style="font-size:11px;word-break:break-all" title="'+escapeHTML(c.pubkey)+'">' + escapeHTML(c.pubkey_prefix) + '…</code></td>' +
      '<td data-label="Notitie">' + escapeHTML(c.notes || '') + '</td>' +
      '<td data-label="Toegevoegd">' + escapeHTML(created) + '</td>' +
      '<td><button class="small danger" onclick="removeMyContact(\''+c.pubkey+'\',\''+safeName+'\')">verwijder</button></td>' +
    '</tr>';
  }).join('');

  // Import-knop alleen voor admin (backend /contacts/import is admin-only).
  const isAdmin = STATE.me && STATE.me.role === 'admin';
  const importBtn = isAdmin
    ? '<button onclick="showImportQR()" title="contact importeren via QR-code (camera of foto)">Importeer contact via QR…</button>'
    : '<span class="note">Importeren van nieuwe contacten is een admin-actie.</span>';

  el.innerHTML = `
    <section><h2>Mijn contactpersonen (${mine.length})</h2>
      <div style="margin-bottom:10px">${importBtn}</div>
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
      <div class="note">
        Per-user opgeslagen — andere web-gebruikers zien jouw contacten niet.
        Een succesvolle QR-import voegt het contact zowel aan de companion als
        aan jouw lijst hierboven toe.
      </div>
    </section>`;
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

/* ============== Privé-kanaal beheer (v1.1.037, admin-only) ==============
   Toont privé-kanalen (uit STATE.channels filtered), aanmaken-form, QR-import
   knop bovenaan en per-row QR-export + remove. Endpoints: /admin/channels/*. */
function renderPrivChansManager(){
  const el = $('privchans-view');
  if (!el) return;
  const priv = (STATE.channels || []).filter(c => c.kind === 'private');

  const rows = priv.map(c => {
    const display = escapeHTML(c.alias || c.name || ('slot ' + c.idx));
    return '<tr>' +
      '<td data-label="Slot">' + c.idx + '</td>' +
      '<td data-label="Naam">' + display + '</td>' +
      '<td data-label="Scope">' + escapeHTML(c.scope || '—') + '</td>' +
      '<td style="white-space:nowrap">' +
        '<button class="small" onclick="showChannelQR(' + c.idx + ')" title="QR delen">QR</button> ' +
        '<button class="small danger" onclick="removePrivChan(' + c.idx + ',\'' +
          (c.name || '').replace(/'/g, "\\'") + '\')" title="verwijder">verwijder</button>' +
      '</td>' +
    '</tr>';
  }).join('');

  el.innerHTML = `
    <section><h2>Privé-kanalen (${priv.length})</h2>
      <div style="margin-bottom:10px;display:flex;gap:8px;flex-wrap:wrap">
        <button class="primary" onclick="showImportChannelQR()">Importeer via QR…</button>
      </div>
      <table style="width:100%">
        <thead><tr>
          <th style="width:50px">Slot</th><th>Naam</th><th>Scope</th><th></th>
        </tr></thead>
        <tbody>${rows || '<tr><td colspan="4" style="color:#888">geen privé-kanalen</td></tr>'}</tbody>
      </table>
      <div class="note">
        Privé-kanalen gebruiken een 128-bit AES-key die je via QR met anderen
        deelt. De Public-slot (0) en hashtag-kanalen blijven in de <i>Chat</i>-tak.
      </div>
    </section>

    <section><h2>Privé-kanaal aanmaken</h2>
      <div class="row"><label>Naam</label><input id="priv-name" type="text" placeholder="bv 'Team Noord'"></div>
      <div class="row"><label>Slot</label><input id="priv-slot" type="number" min="1" max="7" placeholder="auto (1-7)" style="flex:0;width:120px"></div>
      <div class="row"><label>Sleutel</label><input id="priv-key" type="text" placeholder="32 hex chars (16 bytes), leeg = random" style="font-family:monospace"></div>
      <div class="row"><button class="primary" onclick="addPrivChan()">Aanmaken</button></div>
      <div class="note">
        Na aanmaken kun je de QR delen via de <b>QR</b>-knop in de tabel
        hierboven. De ontvanger scant de QR (of upload een foto) en het kanaal
        verschijnt automatisch in zijn Privé-tak.
      </div>
    </section>`;
}

async function addPrivChan(){
  const name = $('priv-name').value.trim();
  const slotRaw = $('priv-slot').value.trim();
  const keyRaw = $('priv-key').value.trim().toLowerCase();
  if (!name) { toast('naam vereist', 'err'); return; }
  if (keyRaw && (keyRaw.length !== 32 || !/^[0-9a-f]+$/.test(keyRaw))) {
    toast('sleutel moet 32 hex chars zijn (gaf ' + keyRaw.length + ')', 'err');
    return;
  }
  const body = { kind: 'private', name: name };
  if (slotRaw !== '') body.slot = parseInt(slotRaw, 10);
  if (keyRaw) body.key = keyRaw;
  try {
    const r = await api('/admin/channels/add', { method: 'POST', body: JSON.stringify(body) });
    toast(r.message || 'aangemaakt', 'ok');
    $('priv-name').value = ''; $('priv-slot').value = ''; $('priv-key').value = '';
    await refresh();
    renderPrivChansManager();
  } catch(e) {}
}

async function removePrivChan(slot, name){
  if (!confirm('Privé-kanaal "' + (name || ('slot ' + slot)) + '" verwijderen?\n\nDit verwijdert het kanaal alleen van de companion en uit de DB; ontvangers van een eerdere QR kunnen het opnieuw importeren.')) return;
  try {
    const r = await api('/admin/channels/remove', { method: 'POST', body: JSON.stringify({ slot: slot }) });
    toast(r.message || 'verwijderd', 'ok');
    await refresh();
    renderPrivChansManager();
  } catch(e) {}
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
  _maybeCloseDrawerOnNav();
}
