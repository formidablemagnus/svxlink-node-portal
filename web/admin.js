const userRows = document.getElementById('user-rows');
const auditRows = document.getElementById('audit-rows');
const userStatus = document.getElementById('user-status');
const auditSearch = document.getElementById('audit-search');
let auditEntries = [];

const tabs = [...document.querySelectorAll('[role="tab"]')];

function activateTab(tab, focus = false) {
  for (const candidate of tabs) {
    const selected = candidate === tab;
    candidate.setAttribute('aria-selected', String(selected));
    candidate.tabIndex = selected ? 0 : -1;
    document.getElementById(candidate.getAttribute('aria-controls')).hidden = !selected;
  }
  if (focus) tab.focus();
}

for (const tab of tabs) {
  tab.addEventListener('click', () => activateTab(tab));
  tab.addEventListener('keydown', (event) => {
    const index = tabs.indexOf(tab);
    let nextIndex;
    if (event.key === 'ArrowRight') nextIndex = (index + 1) % tabs.length;
    else if (event.key === 'ArrowLeft') nextIndex = (index - 1 + tabs.length) % tabs.length;
    else if (event.key === 'Home') nextIndex = 0;
    else if (event.key === 'End') nextIndex = tabs.length - 1;
    else return;
    event.preventDefault();
    activateTab(tabs[nextIndex], true);
  });
}

const ROLE_LABELS = {admin: 'Administratör', operator: 'Operatör'};
const OUTCOME_LABELS = {
  queued: 'Köad', completed: 'Utförd', failed: 'Misslyckades', expired: 'Förföll', ok: 'OK',
};

async function api(path, options = {}) {
  const response = await fetch(path, {
    cache: 'no-store',
    ...options,
    headers: options.body ? {'Content-Type': 'application/json'} : undefined,
  });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.error || `HTTP ${response.status}`);
  return data;
}

function cell(text) {
  const td = document.createElement('td');
  td.textContent = text;
  return td;
}

function renderUsers(users) {
  userRows.replaceChildren();
  for (const user of users) {
    const row = document.createElement('tr');
    row.append(cell(user.username), cell(ROLE_LABELS[user.role] || user.role),
      cell(user.created_at ? new Date(user.created_at).toLocaleString('sv-SE') : 'Huvudadministratör'));
    const actions = document.createElement('td');
    if (!user.builtin) {
      const password = document.createElement('button');
      password.type = 'button';
      password.textContent = 'Nytt lösenord';
      password.addEventListener('click', () => changePassword(user.username));
      const toggle = document.createElement('button');
      toggle.type = 'button';
      const newRole = user.role === 'admin' ? 'operator' : 'admin';
      toggle.textContent = `Gör till ${ROLE_LABELS[newRole].toLowerCase()}`;
      toggle.addEventListener('click', () => userAction(
        () => api(`/api/users/${encodeURIComponent(user.username)}`, {method: 'POST', body: JSON.stringify({role: newRole})}),
        'Rollen är uppdaterad.'));
      const remove = document.createElement('button');
      remove.type = 'button';
      remove.textContent = 'Ta bort';
      remove.addEventListener('click', () => {
        if (!window.confirm(`Ta bort användaren ${user.username}?`)) return;
        userAction(() => api(`/api/users/${encodeURIComponent(user.username)}`, {method: 'DELETE'}),
          'Användaren är borttagen.');
      });
      actions.append(password, ' ', toggle, ' ', remove);
    }
    row.append(actions);
    userRows.append(row);
  }
}

async function userAction(action, message) {
  userStatus.textContent = '';
  try {
    const data = await action();
    renderUsers(data.users);
    userStatus.textContent = message;
    await loadAudit();
  } catch (error) {
    userStatus.textContent = error.message;
  }
}

function changePassword(username) {
  const password = window.prompt(`Nytt lösenord för ${username} (minst 8 tecken med stora och små bokstäver samt siffror):`);
  if (!password) return;
  userAction(() => api(`/api/users/${encodeURIComponent(username)}`, {method: 'POST', body: JSON.stringify({password})}),
    'Lösenordet är uppdaterat.');
}

function renderAudit() {
  const needle = auditSearch.value.trim().toLowerCase();
  auditRows.replaceChildren();
  const entries = auditEntries.filter((entry) => !needle || [
    entry.actor, entry.action, entry.node_name, entry.detail, entry.outcome,
  ].some((value) => String(value || '').toLowerCase().includes(needle)));
  if (!entries.length) {
    const row = document.createElement('tr');
    const td = cell('Inga händelser.');
    td.colSpan = 6;
    td.className = 'muted';
    row.append(td);
    auditRows.append(row);
    return;
  }
  for (const entry of entries) {
    const row = document.createElement('tr');
    row.append(cell(new Date(entry.created_at).toLocaleString('sv-SE')), cell(entry.actor),
      cell(entry.action), cell(entry.node_name || '—'), cell(entry.detail || '—'),
      cell(OUTCOME_LABELS[entry.outcome] || entry.outcome));
    auditRows.append(row);
  }
}

async function loadAudit() {
  try {
    auditEntries = (await api('/api/audit')).entries;
    renderAudit();
    document.getElementById('updated').textContent = `Uppdaterad ${new Date().toLocaleTimeString('sv-SE')}`;
  } catch (error) {
    document.getElementById('updated').textContent = error.message;
  }
}

document.getElementById('user-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  const body = JSON.stringify({
    username: document.getElementById('user-name').value.trim(),
    password: document.getElementById('user-password').value,
    role: document.getElementById('user-role').value,
  });
  await userAction(() => api('/api/users', {method: 'POST', body}), 'Användaren är tillagd.');
  if (userStatus.textContent === 'Användaren är tillagd.') event.target.reset();
});
const backupStatus = document.getElementById('backup-status');

document.getElementById('backup-download').addEventListener('click', async () => {
  backupStatus.textContent = '';
  try {
    const response = await fetch('/api/backup', {cache: 'no-store'});
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const url = URL.createObjectURL(await response.blob());
    const link = document.createElement('a');
    link.href = url;
    link.download = (/filename="([^"]+)"/.exec(response.headers.get('Content-Disposition') || '') || [])[1]
      || 'svxlink-portal-backup.json';
    link.click();
    URL.revokeObjectURL(url);
    backupStatus.textContent = 'Säkerhetskopian är nedladdad.';
    await loadAudit();
  } catch (error) {
    backupStatus.textContent = error.message;
  }
});

document.getElementById('restore-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  backupStatus.textContent = '';
  if (!window.confirm('All befintlig portaldata ersätts av säkerhetskopian. Fortsätta?')) return;
  try {
    const backup = JSON.parse(await document.getElementById('restore-file').files[0].text());
    const password = document.getElementById('restore-password').value;
    const data = await api('/api/restore', {method: 'POST', body: JSON.stringify({password, backup})});
    event.target.reset();
    backupStatus.textContent = `${data.message} Logga in igen om användarna ändrats.`;
    api('/api/users').then((users) => renderUsers(users.users)).catch(() => {});
    await loadAudit();
  } catch (error) {
    backupStatus.textContent = error instanceof SyntaxError ? 'Filen är inte giltig JSON.' : error.message;
  }
});
auditSearch.addEventListener('input', renderAudit);

const nodeConfigFields = {
  portal_url: 'nc-portal-url', repo_url: 'nc-repo-url', repo_ref: 'nc-repo-ref',
  svxlink_config: 'nc-config', svxlink_logfile: 'nc-log', interval_seconds: 'nc-interval',
};
const nodeConfigStatus = document.getElementById('nodeconfig-status');

function fillNodeConfig(config) {
  for (const [key, id] of Object.entries(nodeConfigFields)) {
    document.getElementById(id).value = config[key];
  }
  const portalUrl = document.getElementById('nc-portal-url');
  if (!config.portal_url) portalUrl.placeholder = window.location.origin;
}

document.getElementById('nodeconfig-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  nodeConfigStatus.textContent = '';
  const config = {};
  for (const [key, id] of Object.entries(nodeConfigFields)) {
    const value = document.getElementById(id).value;
    config[key] = key === 'interval_seconds' ? Number(value) : value;
  }
  try {
    const data = await api('/api/node-config', {method: 'POST', body: JSON.stringify(config)});
    fillNodeConfig(data.config);
    nodeConfigStatus.textContent = 'Nodkonfigurationen är sparad.';
    await loadAudit();
  } catch (error) {
    nodeConfigStatus.textContent = error.message;
  }
});
api('/api/node-config').then((data) => fillNodeConfig(data.config)).catch((error) => {
  nodeConfigStatus.textContent = error.message;
});
document.getElementById('audit-refresh').addEventListener('click', loadAudit);

api('/api/users').then((data) => renderUsers(data.users)).catch((error) => {
  userStatus.textContent = error.message;
});
loadAudit();
setInterval(loadAudit, 10000);
