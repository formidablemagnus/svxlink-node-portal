'use strict';
const picker = document.getElementById('node-picker');
const form = document.getElementById('bulk-dtmf-form');
const input = document.getElementById('dtmf-sequence');
const sendButton = document.getElementById('dtmf-send');
const resultList = document.getElementById('bulk-results');
const status = document.getElementById('bulk-status');
let sending = false;

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
    const visible = tabs.filter((candidate) => !candidate.hidden);
    const index = visible.indexOf(tab);
    let nextIndex;
    if (event.key === 'ArrowRight') nextIndex = (index + 1) % visible.length;
    else if (event.key === 'ArrowLeft') nextIndex = (index - 1 + visible.length) % visible.length;
    else if (event.key === 'Home') nextIndex = 0;
    else if (event.key === 'End') nextIndex = visible.length - 1;
    else return;
    event.preventDefault();
    activateTab(visible[nextIndex], true);
  });
}
const wait = (milliseconds) => new Promise((resolve) => setTimeout(resolve, milliseconds));

function updateSelection() {
  const selectedCount = picker.querySelectorAll('input[type="checkbox"]:checked').length;
  sendButton.disabled = sending || selectedCount === 0 || selectedCount > 100;
  if (selectedCount > 100) status.textContent = 'Välj högst 100 noder per utskick.';
  else if (status.textContent === 'Välj högst 100 noder per utskick.') status.textContent = '';
}

/** @param {Array<Object>} nodes @returns {void} */
function renderNodes(nodes) {
  picker.replaceChildren();
  for (const node of nodes) {
    const available = node.online && node.status?.active === true;
    const label = document.createElement('label');
    label.className = `node-choice${available ? '' : ' node-choice-offline'}`;
    const checkbox = document.createElement('input');
    checkbox.type = 'checkbox';
    checkbox.value = node.id;
    checkbox.disabled = !available;
    checkbox.addEventListener('change', updateSelection);
    const name = document.createElement('span');
    name.className = 'node-choice-name';
    name.textContent = node.name;
    const nodeStatus = document.createElement('span');
    nodeStatus.className = `state ${available ? 'online' : 'offline'}`;
    nodeStatus.textContent = !node.online
      ? 'Offline'
      : available
        ? 'Online · SvxLink igång'
        : 'SvxLink ej igång';
    label.append(checkbox, name, nodeStatus);
    picker.append(label);
  }
  if (!nodes.length) {
    picker.textContent = 'Inga registrerade noder.';
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

/** @param {string} sequence @returns {Promise<boolean>} */
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
    const row = document.createElement('tr');
    row.dataset.index = String(index);
    row.addEventListener('dragstart', (event) => {
      if (!event.target.closest('.preset-drag-handle')) {
        event.preventDefault();
        return;
      }
      event.dataTransfer.setData('text/plain', String(index));
      event.dataTransfer.effectAllowed = 'move';
      row.classList.add('dragging');
    });
    row.addEventListener('dragover', (event) => {
      event.preventDefault();
      event.dataTransfer.dropEffect = 'move';
      row.classList.add('drag-over');
    });
    row.addEventListener('dragleave', () => row.classList.remove('drag-over'));
    row.addEventListener('drop', async (event) => {
      event.preventDefault();
      row.classList.remove('drag-over');
      const source = event.dataTransfer.getData('text/plain');
      if (!/^\d+$/.test(source)) return;
      const sourceIndex = Number(source);
      const targetIndex = Number(row.dataset.index);
      if (sourceIndex === targetIndex || sourceIndex >= presets.length) return;
      const bounds = row.getBoundingClientRect();
      let destinationIndex = targetIndex + (event.clientY > bounds.top + bounds.height / 2 ? 1 : 0);
      if (sourceIndex < destinationIndex) destinationIndex -= 1;
      if (sourceIndex === destinationIndex) return;
      const nextPresets = [...presets];
      const [movedPreset] = nextPresets.splice(sourceIndex, 1);
      nextPresets.splice(destinationIndex, 0, movedPreset);
      await savePresets(nextPresets);
    });
    row.addEventListener('dragend', () => {
      row.classList.remove('dragging');
      for (const item of editor.querySelectorAll('.drag-over')) item.classList.remove('drag-over');
    });
    const handle = document.createElement('span');
    handle.className = 'preset-drag-handle';
    handle.draggable = true;
    handle.textContent = '↕';
    handle.title = 'Dra för att ändra ordning';
    handle.setAttribute('aria-label', `Ändra ordning på ${preset.label}`);
    const handleCell = document.createElement('td');
    handleCell.append(handle);
    const label = document.createElement('td');
    const labelValue = document.createElement('span');
    labelValue.className = 'preset-value';
    labelValue.textContent = preset.label;
    label.append(labelValue);
    const sequence = document.createElement('td');
    const sequenceValue = document.createElement('span');
    sequenceValue.className = 'preset-value preset-sequence';
    sequenceValue.textContent = preset.sequence;
    sequence.append(sequenceValue);
    const remove = document.createElement('button');
    remove.type = 'button';
    remove.textContent = 'Ta bort';
    remove.addEventListener('click', () => savePresets(presets.filter((_, i) => i !== index)));
    const action = document.createElement('td');
    action.append(remove);
    row.append(handleCell, label, sequence, action);
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
