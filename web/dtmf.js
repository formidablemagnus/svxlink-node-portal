const picker = document.getElementById('node-picker');
const form = document.getElementById('bulk-dtmf-form');
const input = document.getElementById('dtmf-sequence');
const sendButton = document.getElementById('dtmf-send');
const resultList = document.getElementById('bulk-results');
const status = document.getElementById('bulk-status');
let sending = false;
const wait = (milliseconds) => new Promise((resolve) => setTimeout(resolve, milliseconds));

function updateSelection() {
  const selectedCount = picker.querySelectorAll('input[type="checkbox"]:checked').length;
  sendButton.disabled = sending || selectedCount === 0 || selectedCount > 100;
  if (selectedCount > 100) status.textContent = 'Välj högst 100 noder per utskick.';
  else if (status.textContent === 'Välj högst 100 noder per utskick.') status.textContent = '';
}

function renderNodes(nodes) {
  picker.replaceChildren();
  for (const node of nodes) {
    const label = document.createElement('label');
    label.className = `node-choice${node.online ? '' : ' node-choice-offline'}`;
    const checkbox = document.createElement('input');
    checkbox.type = 'checkbox';
    checkbox.value = node.id;
    checkbox.disabled = !node.online;
    checkbox.addEventListener('change', updateSelection);
    const name = document.createElement('span');
    name.className = 'node-choice-name';
    name.textContent = node.name;
    const nodeStatus = document.createElement('span');
    nodeStatus.className = `state ${node.online ? 'online' : 'offline'}`;
    nodeStatus.textContent = node.online ? 'Online' : 'Offline';
    label.append(checkbox, name, nodeStatus);
    picker.append(label);
  }
  if (!nodes.length) {
    picker.textContent = 'Inga registrerade noder.';
    nav.textContent = 'Inga registrerade noder';
  }
  updateSelection();
  document.getElementById('updated').textContent =
    `${nodes.length} ${nodes.length === 1 ? 'nod' : 'noder'} lästa`;
}

async function loadNodes() {
  try {
    const response = await fetch('/api/nodes', {cache: 'no-store'});
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const data = await response.json();
    renderNodes(data.nodes);
  } catch (error) {
    picker.textContent = 'Kunde inte läsa nodlistan.';
    document.getElementById('node-error').textContent =
      `Kunde inte läsa noder: ${error.message}`;
    document.getElementById('updated').textContent = 'Uppdatering misslyckades';
  }
}

async function waitForCommand(commandId) {
  for (let attempt = 0; attempt < 60; attempt += 1) {
    await wait(1000);
    const response = await fetch(`/api/commands/${encodeURIComponent(commandId)}`, {cache: 'no-store'});
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const result = await response.json();
    if (result.status === 'completed') return 'Skickad';
    if (result.status === 'failed') throw new Error(result.result?.error || 'Agenten kunde inte skicka sekvensen.');
    if (result.status === 'expired') throw new Error('Åtgärden hann löpa ut innan agenten tog emot den.');
  }
  throw new Error('Agenten har ännu inte bekräftat åtgärden.');
}

document.getElementById('select-online').addEventListener('click', () => {
  for (const checkbox of picker.querySelectorAll('input[type="checkbox"]:not(:disabled)')) {
    checkbox.checked = true;
  }
  updateSelection();
});
document.getElementById('clear-selection').addEventListener('click', () => {
  for (const checkbox of picker.querySelectorAll('input[type="checkbox"]')) {
    checkbox.checked = false;
  }
  updateSelection();
});

async function sendToSelected(sequence) {
  const nodeIds = Array.from(
    picker.querySelectorAll('input[type="checkbox"]:checked'),
    (checkbox) => checkbox.value
  );
  if (!nodeIds.length || nodeIds.length > 100) return false;
  sending = true;
  sendButton.disabled = true;
  setPresetsDisabled(true);
  status.textContent = `Skickar till ${nodeIds.length} ${nodeIds.length === 1 ? 'nod' : 'noder'}…`;
  resultList.replaceChildren();
  try {
    const response = await fetch('/api/dtmf-commands', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({node_ids: nodeIds, sequence})
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || `HTTP ${response.status}`);
    const rows = new Map();
    for (const result of data.results) {
      const item = document.createElement('li');
      const name = document.createElement('strong');
      name.textContent = result.name;
      const detail = document.createElement('span');
      detail.textContent = result.error ? ` — ${result.error}` : ' — I kö';
      item.append(name, detail);
      resultList.append(item);
      rows.set(result.node_id, detail);
    }
    await Promise.all(data.results.map(async (result) => {
      if (!result.command_id) return;
      const row = rows.get(result.node_id);
      try {
        row.textContent = ` — ${await waitForCommand(result.command_id)}`;
      } catch (error) {
        row.textContent = ` — Fel: ${error.message}`;
      }
    }));
    status.textContent = 'Bearbetningen är klar. Se resultat per nod.';
    return true;
  } catch (error) {
    status.textContent = `DTMF kunde inte skickas: ${error.message}`;
    return false;
  } finally {
    sending = false;
    updateSelection();
  }
}

form.addEventListener('submit', async (event) => {
  event.preventDefault();
  if (!form.reportValidity()) return;
  if (await sendToSelected(input.value)) input.value = '';
});

const presetContainer = document.getElementById('dtmf-presets');
const presetStatus = document.getElementById('preset-status');
const editor = document.getElementById('preset-editor');
let presets = [];

function setPresetsDisabled(disabled) {
  for (const button of presetContainer.querySelectorAll('button')) button.disabled = disabled;
}

function renderPresets() {
  presetContainer.replaceChildren();
  for (const preset of presets) {
    const button = document.createElement('button');
    button.type = 'button';
    button.textContent = preset.label;
    button.title = preset.sequence;
    button.addEventListener('click', () => {
      const count = picker.querySelectorAll('input[type="checkbox"]:checked').length;
      if (!count) {
        status.textContent = 'Välj minst en nod först.';
        return;
      }
      if (window.confirm(`Skicka "${preset.label}" (${preset.sequence}) till ${count} valda noder?`)) {
        sendToSelected(preset.sequence);
      }
    });
    presetContainer.append(button);
  }
  if (!presets.length) presetContainer.textContent = 'Inga snabbknappar har skapats ännu.';
  renderEditor();
  setPresetsDisabled(sending);
}

function renderEditor() {
  editor.replaceChildren();
  presets.forEach((preset, index) => {
    const row = document.createElement('li');
    const text = document.createElement('span');
    text.textContent = `${preset.label} — ${preset.sequence}`;
    const remove = document.createElement('button');
    remove.type = 'button';
    remove.textContent = 'Ta bort';
    remove.addEventListener('click', () => savePresets(presets.filter((_, i) => i !== index)));
    row.append(text, remove);
    editor.append(row);
  });
}

async function savePresets(nextPresets) {
  presetStatus.textContent = 'Sparar…';
  try {
    const response = await fetch('/api/dtmf-buttons', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({buttons: nextPresets})
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || `HTTP ${response.status}`);
    presets = data.buttons;
    renderPresets();
    presetStatus.textContent = 'Snabbknapparna sparades.';
    return true;
  } catch (error) {
    presetStatus.textContent = `Kunde inte spara: ${error.message}`;
    return false;
  }
}

document.getElementById('preset-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  const presetForm = event.currentTarget;
  if (!presetForm.reportValidity()) return;
  const label = document.getElementById('preset-label');
  const sequence = document.getElementById('preset-sequence');
  if (await savePresets([...presets, {label: label.value, sequence: sequence.value}])) {
    label.value = '';
    sequence.value = '';
  }
});

async function loadPresets() {
  try {
    const response = await fetch('/api/dtmf-buttons', {cache: 'no-store'});
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    presets = (await response.json()).buttons;
    renderPresets();
  } catch (error) {
    presetContainer.textContent = `Kunde inte läsa snabbknappar: ${error.message}`;
  }
}

loadNodes();
loadPresets();
