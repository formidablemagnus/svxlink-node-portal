const radioNames = {tx: 'TX', rx: 'RX', idle: 'Vila', unknown: 'Okänd'};
const nodeId = window.location.pathname.split('/').filter(Boolean).pop();
const setText = (id, value) => { document.getElementById(id).textContent = value; };
document.getElementById('config-link').href = `/nodes/${encodeURIComponent(nodeId)}/config`;
document.getElementById('schedules-link').href = `/nodes/${encodeURIComponent(nodeId)}/schedules`;
const dtmfForm = document.getElementById('dtmf-form');
const dtmfInput = document.getElementById('dtmf-sequence');
const dtmfButton = document.getElementById('dtmf-send');
const dtmfStatus = document.getElementById('dtmf-status');
let dtmfSending = false;
const serviceStartButton = document.getElementById('service-start');
const serviceStopButton = document.getElementById('service-stop');
const serviceCommandStatus = document.getElementById('service-command-status');
let serviceCommandRunning = false;
let nodeOnline = false;
let serviceActive = null;
let serviceStatusExpected = null;
const wait = (milliseconds) => new Promise((resolve) => setTimeout(resolve, milliseconds));

function updateServiceControls() {
  const waitingForStatus = serviceStatusExpected !== null;
  serviceStartButton.disabled = !nodeOnline || serviceCommandRunning || waitingForStatus || serviceActive !== false;
  serviceStopButton.disabled = !nodeOnline || serviceCommandRunning || waitingForStatus || serviceActive !== true;
}

function renderRadio(radio) {
  const mode = radioNames[radio.mode] || 'Okänd';
  const signal = Number.isInteger(radio.signal_level) ? `${radio.signal_level}%` : 'Okänd';
  setText('signal-level', signal);
  const devices = document.getElementById('radio-devices');
  devices.replaceChildren();
  const rows = [
    ...(radio.receivers || []).map((r) => `RX ${r.name}: ${r.receiving ? `öppen, ${r.siglev}%` : `stängd, ${r.siglev}%`}`),
    ...(radio.transmitters || []).map((t) => `TX ${t.name}: ${t.transmit ? 'sänder' : 'vila'}`)
  ];
  for (const text of rows.length ? rows : ['Inga mottagare eller sändare rapporterade.']) {
    const item = document.createElement('li');
    item.textContent = text;
    if (!rows.length) item.className = 'muted';
    devices.append(item);
  }
  if (radio.error) setText('error', radio.error);
}

let liveRadioAt = 0;
let radioSource = null;

function startRadioStream() {
  if (radioSource) radioSource.close();
  radioSource = new EventSource(`/api/nodes/${encodeURIComponent(nodeId)}/radio-events`);
  radioSource.addEventListener('radio', (event) => {
    liveRadioAt = Date.now();
    setText('live-state', 'Live');
    renderRadio(JSON.parse(event.data));
  });
  radioSource.onerror = () => setText('live-state', 'Återansluter…');
}

let allLogs = [];
const ERROR_RE = /\b(error|fatal|critical|\*{3})\b|\bfel\b/i;
const WARNING_RE = /\b(warn(ing)?|varning)\b/i;

function logLevel(line) {
  if (ERROR_RE.test(line)) return 'error';
  if (WARNING_RE.test(line)) return 'warning';
  return 'info';
}

function renderLogs() {
  const query = document.getElementById('log-search').value;
  const level = document.getElementById('log-level').value;
  const useRegex = document.getElementById('log-regex').checked;
  const errorBox = document.getElementById('log-filter-error');
  let matches = () => true;
  errorBox.textContent = '';
  if (query) {
    if (useRegex) {
      try {
        const re = new RegExp(query, 'i');
        matches = (line) => re.test(line);
      } catch (error) {
        errorBox.textContent = 'Ogiltigt reguljärt uttryck.';
      }
    } else {
      const needle = query.toLowerCase();
      matches = (line) => line.toLowerCase().includes(needle);
    }
  }
  const filtered = allLogs.filter((line) => matches(line) && (!level || logLevel(line) === level));
  const logList = document.getElementById('log-lines');
  logList.replaceChildren();
  if (filtered.length) {
    for (const line of filtered.slice().reverse()) {
      const item = document.createElement('li');
      item.textContent = line;
      logList.append(item);
    }
  } else {
    const item = document.createElement('li');
    item.className = 'muted';
    item.textContent = allLogs.length ? 'Inga loggrader matchar filtret.' : 'Inga loggrader rapporterade.';
    logList.append(item);
  }
  const filterActive = Boolean(query || level);
  setText('log-count', filterActive
    ? `${filtered.length} av ${allLogs.length} rader`
    : `${allLogs.length} ${allLogs.length === 1 ? 'rad' : 'rader'}`);
}

for (const id of ['log-search', 'log-level', 'log-regex']) {
  document.getElementById(id).addEventListener('input', renderLogs);
}
document.getElementById('log-clear').addEventListener('click', () => {
  document.getElementById('log-search').value = '';
  document.getElementById('log-level').value = '';
  document.getElementById('log-regex').checked = false;
  renderLogs();
});

function infoValue(value, unit) {
  if (value === null || value === undefined || value === '' || (Array.isArray(value) && !value.length)) return '—';
  const text = Array.isArray(value) ? value.join(', ') : String(value);
  return unit ? `${text} ${unit}` : text;
}

function appendMeta(list, label, value) {
  const wrapper = document.createElement('div');
  const dt = document.createElement('dt');
  const dd = document.createElement('dd');
  dt.textContent = label;
  dd.textContent = value;
  wrapper.append(dt, dd);
  list.append(wrapper);
}

function deviceSection(title, devices, transmitter) {
  const section = document.createElement('div');
  section.className = 'station-sub';
  const heading = document.createElement('h4');
  heading.textContent = `${title} (${Array.isArray(devices) ? devices.length : 0})`;
  section.append(heading);
  if (!Array.isArray(devices) || !devices.length) {
    const none = document.createElement('p');
    none.className = 'muted';
    none.textContent = 'Inga uppgifter.';
    section.append(none);
    return section;
  }
  for (const device of devices) {
    const list = document.createElement('dl');
    list.className = 'detail-meta device-card';
    appendMeta(list, 'Namn', infoValue(device.name ? `${device.name} (${device.id})` : device.id));
    appendMeta(list, 'Frekvens', infoValue(device.freq, 'MHz'));
    if (transmitter) appendMeta(list, 'Effekt', infoValue(device.pwr, 'W'));
    else appendMeta(list, 'Squelch', infoValue(device.sql_type));
    appendMeta(list, 'CTCSS', infoValue(device.ctcss, 'Hz'));
    const antennas = (device.ants || []).map((ant) => [
      ant.comment, ant.height !== null && ant.height !== undefined ? `${ant.height} m` : '',
      ant.dir !== null && ant.dir !== undefined ? `${ant.dir}°` : '',
    ].filter(Boolean).join(' · ')).filter(Boolean);
    appendMeta(list, 'Antenn', antennas.length ? antennas.join('; ') : '—');
    section.append(list);
  }
  return section;
}

function renderNodeInfo(info) {
  const panel = document.getElementById('node-info-panel');
  panel.hidden = !info;
  if (!info) return;
  const meta = document.getElementById('node-info-meta');
  meta.replaceChildren();
  appendMeta(meta, 'Plats', infoValue(info.location));
  appendMeta(meta, 'Nodtyp', infoValue(info.node_class));
  appendMeta(meta, 'Sysop', infoValue(info.sysop));
  appendMeta(meta, 'Dold i listor', info.hidden === null || info.hidden === undefined ? '—' : (info.hidden ? 'Ja' : 'Nej'));
  appendMeta(meta, 'Ton → talgrupp', (info.tone_to_tg || []).map((t) => `${t.tone} Hz → TG ${t.tg}`).join(', ') || '—');
  const container = document.getElementById('node-info-qth');
  container.replaceChildren();
  for (const qth of info.qth || []) {
    const block = document.createElement('section');
    block.className = 'station';
    const heading = document.createElement('h3');
    heading.className = 'station-title';
    heading.textContent = `Station: ${qth.name || 'Namnlös'}`;
    const position = document.createElement('dl');
    position.className = 'detail-meta';
    appendMeta(position, 'Latitud', infoValue(qth.lat));
    appendMeta(position, 'Longitud', infoValue(qth.long));
    appendMeta(position, 'Lokator', infoValue(qth.loc));
    block.append(heading, position, deviceSection('Mottagare', qth.rx, false),
      deviceSection('Sändare', qth.tx, true));
    container.append(block);
  }
}

function renderNode(node) {
  const status = node.status || {};
  const radio = status.radio || {};
  const lastSeen = node.last_seen ? new Date(node.last_seen).toLocaleString('sv-SE') : 'Aldrig';
  setText('node-name', node.name);
  setText('node-callsign', status.callsign || 'Lokal SvxLink-status från nodagenten');
  const setDot = (id, ok, label) => {
    const el = document.getElementById(id);
    el.className = `status-dot ${ok ? 'online' : 'offline'}`;
    el.title = label;
    el.setAttribute('aria-label', label);
  };
  setDot('online', node.online, `Nodanslutning: ${node.online ? 'online' : 'offline'}`);
  setDot('service', status.active === true, `SvxLink-tjänst: ${status.active === true ? 'igång' : (status.service_status || 'avstängd/okänd')}`);
  if (Date.now() - liveRadioAt > 5000) renderRadio(radio);
  setText('talkgroup', status.current_tg ? `TG ${status.current_tg}` : 'Okänd');
  currentTg = status.current_tg ? String(status.current_tg) : '';
  renderReflector();
  setText('tg-event', status.tg_event || 'Okänd');
  setText('monitor-tgs', Array.isArray(status.monitor_tgs) && status.monitor_tgs.length ? status.monitor_tgs.join(', ') : 'Inga');
  setText('pty-checks', (status.pty_checks || []).map((check) => check.reason).filter(Boolean).join(' '));
  setText('logic', status.logic || 'Okänd');
  setText('callsign', status.callsign || 'Okänd');
  setText('service-status', status.service_status || 'Okänd');
  setText('last-seen', lastSeen);
  setText('node-id', node.id);
  nodeOnline = Boolean(node.online);
  serviceActive = typeof status.active === 'boolean' ? status.active : null;
  if (serviceActive === serviceStatusExpected) serviceStatusExpected = null;
  updateServiceControls();
  allLogs = Array.isArray(status.logs) ? status.logs : [];
  renderNodeInfo(status.node_info);
  renderLogs();
  dtmfButton.disabled = !node.online || dtmfSending;
  setPresetsDisabled(!node.online || dtmfSending);
  document.getElementById('error').textContent = node.online
    ? ''
    : 'Noden är offline. Visade statusvärden kommer från den senaste rapporten.';
  document.getElementById('updated').textContent = `Uppdaterad ${new Date().toLocaleTimeString('sv-SE')}`;
}

async function waitForCommand(commandId, successMessage, failureMessage) {
  for (let attempt = 0; attempt < 60; attempt += 1) {
    await wait(1000);
    const response = await fetch(`/api/commands/${encodeURIComponent(commandId)}`, {cache: 'no-store'});
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const result = await response.json();
    if (result.status === 'completed') return successMessage;
    if (result.status === 'failed') throw new Error(result.result?.error || failureMessage);
    if (result.status === 'expired') throw new Error('Åtgärden hann löpa ut innan nodagenten tog emot den.');
  }
  throw new Error('Nodagenten har ännu inte bekräftat åtgärden.');
}

async function waitForServiceStatus(expectedActive) {
  serviceStatusExpected = expectedActive;
  updateServiceControls();
  const expectedStatus = expectedActive ? 'aktiv' : 'avstängd';
  serviceCommandStatus.textContent = `Åtgärden är utförd. Väntar på rapporterad status: ${expectedStatus}…`;
  for (let attempt = 0; attempt < 60; attempt += 1) {
    const node = await refresh();
    if (node?.online && node.status?.active === expectedActive) {
      return expectedActive
        ? 'SvxLink-tjänsten är aktiv. Du kan nu stänga av den.'
        : 'SvxLink-tjänsten är avstängd. Du kan nu starta den.';
    }
    if (attempt < 59) await wait(1000);
  }
  throw new Error(`Noden bekräftade inte att tjänsten blev ${expectedStatus}.`);
}

async function controlService(operation) {
  serviceCommandRunning = true;
  updateServiceControls();
  serviceCommandStatus.textContent = operation === 'start'
    ? 'Startar SvxLink-tjänsten…'
    : 'Stänger av SvxLink-tjänsten…';
  try {
    const response = await fetch(`/api/nodes/${encodeURIComponent(nodeId)}/service-commands`, {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({operation})
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || `HTTP ${response.status}`);
    await waitForCommand(
      data.command_id,
      'Tjänståtgärden genomfördes.',
      'Nodagenten kunde inte ändra tjänstestatusen.'
    );
    serviceCommandStatus.textContent = await waitForServiceStatus(operation === 'start');
  } catch (error) {
    serviceCommandStatus.textContent = `SvxLink-åtgärden kunde inte bekräftas: ${error.message}`;
  } finally {
    serviceCommandRunning = false;
    serviceStatusExpected = null;
    updateServiceControls();
  }
}

serviceStartButton.addEventListener('click', () => controlService('start'));
serviceStopButton.addEventListener('click', () => controlService('stop'));

const presetContainer = document.getElementById('dtmf-presets');
const reflectorContainer = document.getElementById('reflector-tgs');
let currentTg = '';
let reflectorState = null;

function renderReflector() {
  const d = reflectorState;
  if (!d) return;
  if (!d.ok) {
    setText('reflector-nodes', 'Status saknas');
    setText('reflector-updated', d.error || 'Ansluter till reflektorströmmen…');
    return;
  }
  const online = document.getElementById('online').classList.contains('online');
  reflectorContainer.replaceChildren();
  for (const tg of d.talkgroups) {
    const button = document.createElement('button');
    button.type = 'button';
    button.className = `tg-button${tg.talkers ? ' tx' : ''}${String(tg.tg) === currentTg ? ' active' : ''}`;
    button.title = `Anslut till TG ${tg.tg} (91${tg.tg}#)`;
    button.disabled = !online || dtmfSending;
    button.append(`TG ${tg.tg}`);
    const nodes = document.createElement('span');
    nodes.className = 'tg-badge';
    nodes.textContent = `${tg.nodes} nod${tg.nodes === 1 ? '' : 'er'}`;
    button.append(nodes);
    if (tg.talkers) {
      const tx = document.createElement('span');
      tx.className = 'tg-badge tx';
      tx.textContent = `TX ${tg.talkers}`;
      button.append(tx);
    }
    button.addEventListener('click', () => sendDtmf(`91${tg.tg}#`));
    reflectorContainer.append(button);
  }
  if (!d.talkgroups.length) reflectorContainer.textContent = 'Inga noder har valt en talgrupp över 0 just nu.';
  setText('reflector-nodes', `${d.node_count} synliga noder`);
  setText('reflector-updated', d.connected
    ? `Live · uppdaterad ${new Date(d.updated_at || Date.now()).toLocaleTimeString('sv-SE')}`
    : (d.error || 'Reflektorströmmen är frånkopplad.'));
}

function startReflectorStream() {
  const source = new EventSource('/api/reflector-events');
  source.addEventListener('reflector', (event) => {
    try {
      reflectorState = JSON.parse(event.data);
      renderReflector();
    } catch (error) {
      setText('reflector-updated', 'Kunde inte läsa reflektorströmmen.');
    }
  });
}

function setPresetsDisabled(disabled) {
  for (const button of presetContainer.querySelectorAll('button')) button.disabled = disabled;
  for (const button of reflectorContainer.querySelectorAll('button')) button.disabled = disabled;
}

async function sendDtmf(sequence) {
  dtmfSending = true;
  dtmfButton.disabled = true;
  setPresetsDisabled(true);
  dtmfStatus.textContent = 'Skickar DTMF-sekvens…';
  try {
    const response = await fetch(`/api/nodes/${encodeURIComponent(nodeId)}/dtmf-commands`, {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({sequence})
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || `HTTP ${response.status}`);
    dtmfStatus.textContent = await waitForCommand(
      data.command_id,
      'DTMF-sekvensen skickades.',
      'Nodagenten kunde inte skicka sekvensen.'
    );
    return true;
  } catch (error) {
    dtmfStatus.textContent = `DTMF kunde inte skickas: ${error.message}`;
    return false;
  } finally {
    dtmfSending = false;
    const online = document.getElementById('online').classList.contains('online');
    dtmfButton.disabled = !online;
    setPresetsDisabled(!online);
  }
}

dtmfForm.addEventListener('submit', async (event) => {
  event.preventDefault();
  if (!dtmfForm.reportValidity()) return;
  if (await sendDtmf(dtmfInput.value)) dtmfInput.value = '';
});

async function loadPresets() {
  try {
    const response = await fetch('/api/dtmf-buttons', {cache: 'no-store'});
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const data = await response.json();
    presetContainer.replaceChildren();
    for (const preset of data.buttons) {
      const button = document.createElement('button');
      button.type = 'button';
      button.textContent = preset.label;
      button.title = preset.sequence;
      button.disabled = !document.getElementById('online').classList.contains('online');
      button.addEventListener('click', () => sendDtmf(preset.sequence));
      presetContainer.append(button);
    }
    if (!data.buttons.length) presetContainer.textContent = 'Inga snabbknappar har skapats ännu.';
  } catch (error) {
    presetContainer.textContent = `Kunde inte läsa snabbknappar: ${error.message}`;
  }
}

async function refresh() {
  try {
    const response = await fetch(`/api/nodes/${encodeURIComponent(nodeId)}`, {cache: 'no-store'});
    if (response.status === 404) throw new Error('Noden hittades inte.');
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const data = await response.json();
    renderNode(data.node);
    return data.node;
  } catch (error) {
    setText('error', `Kunde inte hämta nodstatus: ${error.message}`);
    setText('updated', 'Uppdatering misslyckades');
    return null;
  }
}

loadPresets();
startReflectorStream();
refresh();
startRadioStream();
setInterval(refresh, 3000);
