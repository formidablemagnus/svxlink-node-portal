const esc = (value) => String(value ?? '').replace(/[&<>"']/g, (char) => ({
  '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
})[char]);
const radioNames = {tx: 'TX', rx: 'RX', idle: 'Vila', unknown: 'Okänd'};
function renderNodes(nodes) {
  const body = document.getElementById('nodes');
  const online = nodes.filter((node) => node.online);
  document.getElementById('total').textContent = String(nodes.length);
  document.getElementById('online').textContent = String(online.length);
  document.getElementById('transmitting').textContent = String(online.filter((node) => node.status?.radio?.mode === 'tx').length);
  document.getElementById('receiving').textContent = String(online.filter((node) => node.status?.radio?.mode === 'rx').length);
  document.getElementById('node-count').textContent = `${nodes.length} ${nodes.length === 1 ? 'nod' : 'noder'}`;
  if (!nodes.length) {
    body.innerHTML = '<tr><td colspan="10" class="muted">Inga noder är registrerade ännu.</td></tr>';
    return;
  }
  body.innerHTML = nodes.map((node) => {
    const status = node.status || {};
    const radio = status.radio || {};
    const lastSeen = node.last_seen ? new Date(node.last_seen).toLocaleString('sv-SE') : 'Aldrig';
    const logic = window.SvxLogic;
    const type = logic.logicType(status.logic);
    let serviceClass = 'offline';
    let serviceLabel = status.service_status || 'Avstängd';
    if (status.active === true) {
      serviceClass = node.online ? 'online' : 'unknown';
      serviceLabel = node.online ? 'Igång' : 'Okänd';
    } else if (status.active === undefined || status.active === null) {
      serviceClass = 'unknown';
      serviceLabel = 'Okänd';
    }
    return `<tr>
      <td><strong><span class="node-icon ${node.online ? 'node-online' : 'node-offline'}" title="${esc(logic.LOGIC_TITLES[type])}" role="img" aria-label="${esc(logic.LOGIC_TITLES[type])}">${logic.ICONS[type]}</span> <a href="/nodes/${encodeURIComponent(node.id)}">${esc(node.name)}</a></strong>${status.callsign ? `<small>${esc(status.callsign)}</small>` : ''}</td>
      <td><span class="state ${node.online ? 'online' : 'offline'}">${node.online ? esc(status.service_status || 'Online') : 'Offline'}</span></td>
      <td><span class="status-item"><span class="status-dot ${serviceClass}" role="img" aria-label="SvxLink-tjänst: ${esc(serviceLabel)}" title="SvxLink-tjänst: ${esc(serviceLabel)}"></span><small>${esc(serviceLabel)}</small></span></td>
      <td>${esc(radioNames[radio.mode] || 'Okänd')}</td>
      <td>${node.online && Number.isInteger(radio.signal_level) ? `${esc(radio.signal_level)}%` : '—'}</td>
      <td>${status.current_tg ? `TG ${esc(status.current_tg)}` : 'Okänd'}</td>
      <td>${status.node_version ? `v${esc(status.node_version)}` : 'Okänd'}</td>
      <td>${esc(status.svxlink_version || 'Okänd')}</td>
      <td>${esc(lastSeen)}</td>
      <td><a class="node-settings" href="/nodes/${encodeURIComponent(node.id)}/config" aria-label="Redigera konfiguration för ${esc(node.name)}" title="Redigera konfiguration">
        <svg aria-hidden="true" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="3"></circle><path d="M19.4 15a1.7 1.7 0 0 0 .33 1.82l.06.06-1.65 2.86-.08-.03a1.65 1.65 0 0 0-1.82.34l-.06.06h-3.3l-.04-.08a1.65 1.65 0 0 0-1.82-.33l-.08.03-2.86-1.65.03-.08a1.65 1.65 0 0 0-.34-1.82l-.06-.06v-3.3l.08-.04a1.65 1.65 0 0 0 .33-1.82l-.03-.08 1.65-2.86.08.03a1.65 1.65 0 0 0 1.82-.34l.06-.06h3.3l.04.08a1.65 1.65 0 0 0 1.82.33l.08-.03 2.86 1.65-.03.08a1.65 1.65 0 0 0 .34 1.82l.06.06v3.3z"></path></svg>
      </a></td>
    </tr>`;
  }).join('');
}
async function refresh() {
  try {
    const response = await fetch('/api/nodes', {cache: 'no-store'});
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const data = await response.json();
    renderNodes(data.nodes);
    document.getElementById('error').textContent = '';
    document.getElementById('updated').textContent = `Uppdaterad ${new Date().toLocaleTimeString('sv-SE')}`;
  } catch (error) {
    document.getElementById('error').textContent = `Kunde inte hämta nodstatus: ${error.message}`;
    document.getElementById('updated').textContent = 'Uppdatering misslyckades';
  }
}
document.getElementById('create-node-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  const button = form.querySelector('button[type="submit"]');
  const error = document.getElementById('create-node-error');
  const result = document.getElementById('created-node');
  error.textContent = '';
  result.hidden = true;
  button.disabled = true;
  try {
    const response = await fetch('/api/nodes', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({name: document.getElementById('new-node-name').value})
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || `HTTP ${response.status}`);
    document.getElementById('created-node-id').value = data.node_id;
    document.getElementById('created-node-token').value = data.token;
    result.hidden = false;
    form.reset();
    refresh();
  } catch (cause) {
    error.textContent = `Kunde inte skapa noden: ${cause.message}`;
  } finally {
    button.disabled = false;
  }
});
refresh();
setInterval(refresh, 5000);
