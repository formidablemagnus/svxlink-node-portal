const esc = (value) => String(value ?? '').replace(/[&<>"']/g, (char) => ({
  '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
})[char]);
const radioNames = {tx: 'TX', rx: 'RX', idle: 'Vila', unknown: 'Okänd'};

// Ring som fylls från 0 till 100 %: röd under 30, gul under 50, annars grön.
function signalRing(value) {
  const level = Math.min(100, Math.max(0, value));
  const tone = level < 30 ? 'low' : level < 50 ? 'mid' : 'high';
  return `<svg class="signal-ring signal-${tone}" viewBox="0 0 36 36" role="img" aria-label="Signal RX ${level}%"><title>Signal RX ${level}%</title><circle class="ring-track" cx="18" cy="18" r="15.915" pathLength="100"></circle><circle class="ring-fill" cx="18" cy="18" r="15.915" pathLength="100" stroke-dasharray="${level} 100" transform="rotate(-90 18 18)"></circle></svg>`;
}
const SERVICE_LABELS = {
  active: 'Igång', inactive: 'Avstängd', failed: 'Fel', activating: 'Startar',
  deactivating: 'Stoppas', reloading: 'Laddar om', unknown: 'Okänd',
};
const versionParts = (value) => (String(value || '').match(/\d+/g) || []).map(Number);
const deleteNodeDialog = document.getElementById('delete-node-dialog');
const deleteNodeForm = document.getElementById('delete-node-form');
let pendingDeleteNodeId = null;
function compareVersions(current, latest) {
  const a = versionParts(current);
  const b = versionParts(latest);
  if (!a.length || !b.length) return 0;
  for (let i = 0; i < Math.max(a.length, b.length); i += 1) {
    const diff = (a[i] || 0) - (b[i] || 0);
    if (diff) return diff < 0 ? -1 : 1;
  }
  return 0;
}
const isOutdated = (current, latest) => compareVersions(current, latest) < 0;
const isPrerelease = (current, latest) => compareVersions(current, latest) > 0;
function renderNodes(nodes, latestSvxlink, canDeleteNodes) {
  const body = document.getElementById('nodes');
  const online = nodes.filter((node) => node.online);
  document.getElementById('total').textContent = String(nodes.length);
  document.getElementById('online').textContent = String(online.length);
  document.getElementById('transmitting').textContent = String(online.filter((node) => node.status?.radio?.mode === 'tx').length);
  document.getElementById('receiving').textContent = String(online.filter((node) => node.status?.radio?.mode === 'rx').length);
  document.getElementById('node-count').textContent = `${nodes.length} ${nodes.length === 1 ? 'nod' : 'noder'}`;
  if (!nodes.length) {
    body.innerHTML = '<tr><td colspan="11" class="muted">Inga noder är registrerade ännu.</td></tr>';
    return;
  }
  body.innerHTML = nodes.map((node) => {
    const status = node.status || {};
    const radio = status.radio || {};
    const lastSeen = node.last_seen ? new Date(node.last_seen).toLocaleString('sv-SE') : 'Aldrig';
    const logic = window.SvxLogic;
    const type = logic.logicType(status.logic);
    let serviceClass = 'offline';
    let serviceLabel = SERVICE_LABELS[status.service_status] || status.service_status || 'Avstängd';
    if (status.active === true) {
      serviceClass = node.online ? 'online' : 'unknown';
      serviceLabel = node.online ? 'Igång' : 'Okänd';
    } else if (status.active === undefined || status.active === null) {
      serviceClass = 'unknown';
      serviceLabel = 'Okänd';
    }
    const logs = Array.isArray(status.logs) ? status.logs : [];
    const errorCount = logs.filter((line) => logic.logLevel(line) === 'error').length;
    const warningCount = logs.filter((line) => logic.logLevel(line) === 'warning').length;
    let logClass = 'log-ok';
    let logLabel = 'OK';
    if (errorCount) {
      logClass = 'log-error';
      logLabel = `${errorCount} fel`;
    } else if (warningCount) {
      logClass = 'log-warning';
      logLabel = `${warningCount} ${warningCount === 1 ? 'varning' : 'varningar'}`;
    }
    const logTitle = `Senaste loggrader: ${errorCount} fel, ${warningCount} varningar`;
    return `<tr>
      <td><strong><span class="node-icon ${node.online ? 'node-online' : 'node-offline'}" title="${esc(logic.LOGIC_TITLES[type])}" role="img" aria-label="${esc(logic.LOGIC_TITLES[type])}">${logic.ICONS[type]}</span> <a href="/nodes/${encodeURIComponent(node.id)}">${esc(node.name)}</a></strong>${status.callsign ? `<small>${esc(status.callsign)}</small>` : ''}</td>
      <td><span class="status-badge ${node.online ? 'online' : 'offline'}">${node.online ? esc(SERVICE_LABELS[status.service_status] || status.service_status || 'Online') : 'Offline'}</span></td>
      <td><span class="status-badge ${serviceClass}" title="SvxLink-tjänst: ${esc(serviceLabel)}">${esc(serviceLabel)}</span></td>
      <td>${esc(radioNames[radio.mode] || 'Okänd')}</td>
      <td>${node.online && Number.isInteger(radio.signal_level) ? signalRing(radio.signal_level) : '—'}</td>
      <td>${status.current_tg ? `TG ${esc(status.current_tg)}` : 'Okänd'}</td>
      <td><span class="status-badge ${logClass}" title="${esc(logTitle)}">${esc(logLabel)}</span></td>
      <td>${status.node_version ? `v${esc(status.node_version)}` : 'Okänd'}</td>
      <td>${esc(status.svxlink_version || 'Okänd')}${node.update ? ` <span class="update-spinner" role="img" aria-label="SvxLink-uppdatering pågår" title="${esc(node.update.progress?.message || 'SvxLink-uppdatering pågår')}"></span>` : ''}${isOutdated(status.svxlink_version, latestSvxlink) ? ` <a class="version-warning" href="https://github.com/sm0svx/svxlink" target="_blank" rel="noopener noreferrer" title="Ny SvxLink-version finns (${esc(latestSvxlink)})" aria-label="Ny SvxLink-version finns (${esc(latestSvxlink)})">&#9888;</a>` : ''}${isPrerelease(status.svxlink_version, latestSvxlink) ? ` <span class="prerelease-marker" role="img" title="Pre-release, nyare än senaste stabila release (${esc(latestSvxlink)})" aria-label="Pre-release, nyare än senaste stabila release (${esc(latestSvxlink)})"><svg viewBox="0 0 20 18" width="18" height="16" aria-hidden="true"><path d="M10 1.5 19 16.5H1Z" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linejoin="round"/><text x="10" y="14" text-anchor="middle" font-size="9" font-weight="700" fill="currentColor">B</text></svg></span>` : ''}</td>
      <td>${esc(lastSeen)}</td>
      <td>${canDeleteNodes ? `<a class="node-settings" href="/nodes/${encodeURIComponent(node.id)}/config" aria-label="Redigera konfiguration för ${esc(node.name)}" title="Redigera konfiguration">
        <svg aria-hidden="true" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="3"></circle><path d="M19.4 15a1.7 1.7 0 0 0 .33 1.82l.06.06-1.65 2.86-.08-.03a1.65 1.65 0 0 0-1.82.34l-.06.06h-3.3l-.04-.08a1.65 1.65 0 0 0-1.82-.33l-.08.03-2.86-1.65.03-.08a1.65 1.65 0 0 0-.34-1.82l-.06-.06v-3.3l.08-.04a1.65 1.65 0 0 0 .33-1.82l-.03-.08 1.65-2.86.08.03a1.65 1.65 0 0 0 1.82-.34l.06-.06h3.3l.04.08a1.65 1.65 0 0 0 1.82.33l.08-.03 2.86 1.65-.03.08a1.65 1.65 0 0 0 .34 1.82l.06.06v3.3z"></path></svg>
      </a>` : ''}${canDeleteNodes ? ` <button type="button" class="delete-node" data-node-id="${esc(node.id)}" data-node-name="${esc(node.name)}" aria-label="Ta bort ${esc(node.name)}" title="Ta bort nod">
        <svg aria-hidden="true" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M3 6h18"></path><path d="M8 6V4h8v2"></path><path d="m19 6-1 14H6L5 6"></path><path d="M10 11v5M14 11v5"></path></svg>
      </button>` : ''}</td>
    </tr>`;
  }).join('');
  for (const button of body.querySelectorAll('.delete-node')) {
    button.addEventListener('click', () => {
      pendingDeleteNodeId = button.dataset.nodeId;
      document.getElementById('delete-node-message').textContent =
        `Noden ${button.dataset.nodeName} och dess väntande agentkommandon tas bort permanent.`;
      document.getElementById('delete-node-password').value = '';
      document.getElementById('delete-node-error').textContent = '';
      deleteNodeDialog.showModal();
      document.getElementById('delete-node-password').focus();
    });
  }
}
let refreshing = false;
async function refresh() {
  if (refreshing || document.hidden) return;
  refreshing = true;
  try {
    const response = await fetch('/api/nodes', {cache: 'no-store'});
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const data = await response.json();
    renderNodes(data.nodes, data.latest_svxlink_version, data.can_delete_nodes === true);
    document.getElementById('error').textContent = '';
    document.getElementById('updated').textContent = `Uppdaterad ${new Date().toLocaleTimeString('sv-SE')}`;
  } catch (error) {
    document.getElementById('error').textContent = `Kunde inte hämta nodstatus: ${error.message}`;
    document.getElementById('updated').textContent = 'Uppdatering misslyckades';
  } finally {
    refreshing = false;
  }
}
document.getElementById('cancel-delete-node').addEventListener('click', () => {
  deleteNodeDialog.close();
});
deleteNodeDialog.addEventListener('close', () => {
  deleteNodeForm.reset();
  pendingDeleteNodeId = null;
});
deleteNodeForm.addEventListener('submit', async (event) => {
  event.preventDefault();
  const submit = deleteNodeForm.querySelector('button[type="submit"]');
  const error = document.getElementById('delete-node-error');
  error.textContent = '';
  submit.disabled = true;
  try {
    const response = await fetch(`/api/nodes/${encodeURIComponent(pendingDeleteNodeId)}`, {
      method: 'DELETE',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({password: document.getElementById('delete-node-password').value})
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || `HTTP ${response.status}`);
    deleteNodeDialog.close();
    await refresh();
  } catch (cause) {
    document.getElementById('delete-node-password').value = '';
    error.textContent = `Kunde inte ta bort noden: ${cause.message}`;
  } finally {
    submit.disabled = false;
  }
});
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
    const command = data.install_command || '';
    document.getElementById('created-node-command').value = command;
    document.getElementById('install-command-block').hidden = !command;
    document.getElementById('install-command-missing').hidden = Boolean(command);
    result.hidden = false;
    form.reset();
    refresh();
  } catch (cause) {
    error.textContent = `Kunde inte skapa noden: ${cause.message}`;
  } finally {
    button.disabled = false;
  }
});
document.getElementById('copy-install-command').addEventListener('click', async (event) => {
  const field = document.getElementById('created-node-command');
  const button = event.currentTarget;
  try {
    await navigator.clipboard.writeText(field.value);
    button.textContent = 'Kopierat';
  } catch (cause) {
    field.select();
    button.textContent = 'Markerat – tryck Ctrl+C';
  }
  setTimeout(() => { button.textContent = 'Kopiera kommando'; }, 3000);
});
refresh();
setInterval(refresh, 1500);
document.addEventListener('visibilitychange', () => {
  if (!document.hidden) refresh();
});
