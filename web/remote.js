'use strict';
const nodeId = window.location.pathname.split('/').filter(Boolean)[1];
const setText = (id, value) => { document.getElementById(id).textContent = value; };
let liveRadioAt = 0;
const dtmfForm = document.getElementById('dtmf-form');
const dtmfInput = document.getElementById('dtmf-sequence');
const dtmfButton = document.getElementById('dtmf-send');
const dtmfStatus = document.getElementById('dtmf-status');
const presetContainer = document.getElementById('dtmf-presets');
let nodeOnline = false;
let nodeUpdating = false;
let dtmfSending = false;

function updateDtmfControls() {
  const disabled = !nodeOnline || nodeUpdating || dtmfSending;
  dtmfInput.disabled = nodeUpdating;
  dtmfButton.disabled = disabled;
  for (const button of presetContainer.querySelectorAll('button')) button.disabled = disabled;
}

async function sendDtmf(sequence) {
  if (!nodeOnline || nodeUpdating || dtmfSending) return false;
  dtmfSending = true;
  updateDtmfControls();
  dtmfStatus.textContent = 'Skickar DTMF-sekvens…';
  try {
    const commandId = await window.SvxCommands.sendDtmf(nodeId, sequence);
    dtmfStatus.textContent = await window.SvxCommands.waitForCommand(
      commandId, 'DTMF-sekvensen skickades.', 'Nodagenten kunde inte skicka sekvensen.'
    );
    return true;
  } catch (error) {
    dtmfStatus.textContent = `DTMF kunde inte skickas: ${error.message}`;
    return false;
  } finally {
    dtmfSending = false;
    updateDtmfControls();
  }
}

dtmfForm.addEventListener('submit', async (event) => {
  event.preventDefault();
  if (!dtmfForm.reportValidity()) return;
  const sequence = dtmfInput.value;
  if (await sendDtmf(sequence) && dtmfInput.value === sequence) dtmfInput.value = '';
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
      button.addEventListener('click', () => sendDtmf(preset.sequence));
      presetContainer.append(button);
    }
    if (!data.buttons.length) presetContainer.textContent = 'Inga snabbknappar har skapats ännu.';
    updateDtmfControls();
  } catch (error) {
    presetContainer.textContent = `Kunde inte läsa snabbknappar: ${error.message}`;
  }
}

function renderRadio(radio) {
  for (const [key, label, activeText, activeClass] of [['tx', 'TX', 'sänder', 'radio-tx'], ['rx', 'RX', 'tar emot', 'radio-rx']]) {
    const active = radio.mode === key;
    const dot = document.getElementById(`radio-${key}`);
    dot.className = `status-dot ${active ? activeClass : 'radio-off'}`;
    dot.setAttribute('aria-label', `${label}: ${active ? activeText : 'passiv'}`);
    setText(`radio-${key}-text`, `${label} ${active ? activeText : 'passiv'}`);
  }
}

function startRadioStream() {
  const source = new EventSource(`/api/nodes/${encodeURIComponent(nodeId)}/radio-events`);
  source.addEventListener('radio', (event) => {
    liveRadioAt = Date.now();
    renderRadio(JSON.parse(event.data));
  });
}

async function refresh() {
  try {
    const response = await fetch(`/api/nodes/${encodeURIComponent(nodeId)}`, {cache: 'no-store'});
    if (!response.ok) throw new Error(response.status === 404 ? 'Noden hittades inte.' : `HTTP ${response.status}`);
    const {node} = await response.json();
    const status = node.status || {};
    nodeOnline = Boolean(node.online);
    nodeUpdating = Boolean(node.update);
    updateDtmfControls();
    setText('node-name', node.name);
    document.title = `${node.name} – Fjärrstyrning`;
    setText('online-text', node.online ? 'online' : 'offline');
    setText('talkgroup', status.current_tg ? `TG ${status.current_tg}` : 'Okänd');
    setText('tg-event', status.tg_event || 'Okänd');
    if (Date.now() - liveRadioAt > 5000) renderRadio(status.radio || {});
    setText('error', '');
    if (window.SvxAudio) window.SvxAudio.update(status.udp_audio, node.online);
  } catch (error) {
    nodeOnline = false;
    updateDtmfControls();
    setText('error', `Kunde inte hämta nodstatus: ${error.message}`);
  }
}

refresh();
loadPresets();
startRadioStream();
setInterval(refresh, 3000);
