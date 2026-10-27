const pathParts = window.location.pathname.split('/').filter(Boolean);
const nodeId = pathParts[pathParts.length - 2];
const endpoint = `/api/nodes/${encodeURIComponent(nodeId)}/service-log`;
const form = document.getElementById('service-log-form');
const input = document.getElementById('service-log-entry');
const submit = document.getElementById('service-log-submit');
const status = document.getElementById('service-log-status');
const entriesElement = document.getElementById('service-log-entries');

document.getElementById('detail-link').href = `/nodes/${encodeURIComponent(nodeId)}`;

async function api(url, options = {}) {
  const response = await fetch(url, {
    cache: 'no-store',
    ...options,
    headers: options.body ? {'Content-Type': 'application/json'} : undefined,
  });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.error || `HTTP ${response.status}`);
  return data;
}

function renderEntries(entries) {
  entriesElement.replaceChildren();
  document.getElementById('service-log-count').textContent =
    `${entries.length} ${entries.length === 1 ? 'anteckning' : 'anteckningar'}`;
  if (!entries.length) {
    const empty = document.createElement('li');
    empty.className = 'muted';
    empty.textContent = 'Serviceloggen är tom.';
    entriesElement.append(empty);
    return;
  }
  for (const entry of entries) {
    const item = document.createElement('li');
    const heading = document.createElement('div');
    heading.className = 'service-log-meta';
    const timestamp = document.createElement('time');
    timestamp.dateTime = entry.created_at;
    timestamp.textContent = new Date(entry.created_at).toLocaleString('sv-SE');
    const actor = document.createElement('strong');
    actor.textContent = entry.actor;
    heading.append(timestamp, actor);
    const text = document.createElement('p');
    text.textContent = entry.entry;
    item.append(heading, text);
    entriesElement.append(item);
  }
}

async function loadEntries() {
  try {
    const data = await api(endpoint);
    renderEntries(data.entries);
  } catch (error) {
    document.getElementById('service-log-count').textContent = 'Kunde inte läsa loggen';
    status.textContent = error.message;
  }
}

api(`/api/nodes/${encodeURIComponent(nodeId)}`)
  .then((data) => { document.getElementById('node-name').textContent = data.node.name; })
  .catch((error) => { status.textContent = error.message; });
loadEntries();

form.addEventListener('submit', async (event) => {
  event.preventDefault();
  status.textContent = '';
  submit.disabled = true;
  try {
    await api(endpoint, {
      method: 'POST',
      body: JSON.stringify({entry: input.value}),
    });
    input.value = '';
    status.textContent = 'Anteckningen har lagts till.';
    await loadEntries();
  } catch (error) {
    status.textContent = error.message;
  } finally {
    submit.disabled = false;
  }
});
