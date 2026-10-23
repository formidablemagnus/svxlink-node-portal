const radioNames = {tx: 'TX', rx: 'RX', idle: 'Vila', unknown: 'Okänd'};
const nodeId = window.location.pathname.split('/').filter(Boolean).pop();
const setText = (id, value) => { document.getElementById(id).textContent = value; };
document.getElementById('config-link').href = `/nodes/${encodeURIComponent(nodeId)}/config`;
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
const wait = (milliseconds) => new Promise((resolve) => setTimeout(resolve, milliseconds));

function updateServiceControls() {
  serviceStartButton.disabled = !nodeOnline || serviceCommandRunning || serviceActive !== false;
  serviceStopButton.disabled = !nodeOnline || serviceCommandRunning || serviceActive !== true;
}

function renderNode(node) {
  const status = node.status || {};
  const radio = status.radio || {};
  const mode = radioNames[radio.mode] || 'Okänd';
  const signal = Number.isInteger(radio.signal_level) ? `${radio.signal_level}%` : 'Okänd';
  const lastSeen = node.last_seen ? new Date(node.last_seen).toLocaleString('sv-SE') : 'Aldrig';
  setText('node-name', node.name);
  setText('node-callsign', status.callsign || 'SvxLink-status från nodens webpanel');
  setText('online', node.online ? 'Online' : 'Offline');
  setText('service', status.service_status || 'Okänd');
  setText('radio', mode);
  setText('talkgroup', status.current_tg ? `TG ${status.current_tg}` : 'Okänd');
  setText('radio-mode', mode);
  setText('signal-level', signal);
  setText('current-tg', status.current_tg ? `TG ${status.current_tg}` : 'Okänd');
  setText('logic', status.logic || 'Okänd');
  setText('callsign', status.callsign || 'Okänd');
  setText('service-status', status.service_status || 'Okänd');
  setText('last-seen', lastSeen);
  setText('node-id', node.id);
  nodeOnline = Boolean(node.online);
  serviceActive = typeof status.active === 'boolean' ? status.active : null;
  updateServiceControls();
  const logs = Array.isArray(status.logs) ? status.logs : [];
  const logList = document.getElementById('log-lines');
  logList.replaceChildren();
  if (logs.length) {
    for (const line of logs.slice().reverse()) {
      const item = document.createElement('li');
      item.textContent = line;
      logList.append(item);
    }
  } else {
    const item = document.createElement('li');
    item.className = 'muted';
    item.textContent = 'Inga loggrader rapporterade.';
    logList.append(item);
  }
  setText('log-count', `${logs.length} ${logs.length === 1 ? 'rad' : 'rader'}`);
  document.getElementById('online').className = node.online ? 'online' : 'offline';
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
    serviceCommandStatus.textContent = await waitForCommand(
      data.command_id,
      operation === 'start' ? 'SvxLink-tjänsten startades.' : 'SvxLink-tjänsten stängdes av.',
      'Nodagenten kunde inte ändra tjänstestatusen.'
    );
    await refresh();
  } catch (error) {
    serviceCommandStatus.textContent = `Kunde inte ändra SvxLink-tjänsten: ${error.message}`;
  } finally {
    serviceCommandRunning = false;
    updateServiceControls();
  }
}

serviceStartButton.addEventListener('click', () => controlService('start'));
serviceStopButton.addEventListener('click', () => controlService('stop'));

const presetContainer = document.getElementById('dtmf-presets');

function setPresetsDisabled(disabled) {
  for (const button of presetContainer.querySelectorAll('button')) button.disabled = disabled;
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
  } catch (error) {
    setText('error', `Kunde inte hämta nodstatus: ${error.message}`);
    setText('updated', 'Uppdatering misslyckades');
  }
}

loadPresets();
refresh();
setInterval(refresh, 5000);
