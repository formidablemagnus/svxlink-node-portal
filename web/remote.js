'use strict';
const nodeId = window.location.pathname.split('/').filter(Boolean)[1];
const setText = (id, value) => { document.getElementById(id).textContent = value; };
let liveRadioAt = 0;

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
    setText('node-name', node.name);
    document.title = `${node.name} – Fjärrstyrning`;
    setText('online-text', node.online ? 'online' : 'offline');
    setText('tg-event', status.tg_event || 'Okänd');
    if (Date.now() - liveRadioAt > 5000) renderRadio(status.radio || {});
    setText('error', '');
    if (window.SvxAudio) window.SvxAudio.update(status.udp_audio, node.online);
  } catch (error) {
    setText('error', `Kunde inte hämta nodstatus: ${error.message}`);
  }
}

refresh();
startRadioStream();
setInterval(refresh, 3000);
