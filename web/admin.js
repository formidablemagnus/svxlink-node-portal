const userRows = document.getElementById('user-rows');
const auditRows = document.getElementById('audit-rows');
const userStatus = document.getElementById('user-status');
const auditSearch = document.getElementById('audit-search');
let auditEntries = [];

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
document.getElementById('audit-refresh').addEventListener('click', loadAudit);

api('/api/users').then((data) => renderUsers(data.users)).catch((error) => {
  userStatus.textContent = error.message;
});
loadAudit();
setInterval(loadAudit, 10000);
