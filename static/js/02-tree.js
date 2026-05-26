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
