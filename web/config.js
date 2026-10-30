'use strict';
const pathParts = window.location.pathname.split('/').filter(Boolean);
const nodeId = pathParts[pathParts.length - 2];
const setText = (id, value) => { document.getElementById(id).textContent = value; };
const configHashes = {};
const loadedContents = {};
const configNames = {
  'svxlink.conf': 'editor-svxlink',
  'node_info.json': 'editor-node-info'
};
document.getElementById('detail-link').href = `/nodes/${encodeURIComponent(nodeId)}`;
const otherLink = document.getElementById('other-config-link');
if (otherLink) {
  otherLink.href = `/nodes/${encodeURIComponent(nodeId)}/${otherLink.dataset.page}`;
}

function configStatus(documentName, message, isError = false) {
  const element = document.getElementById(
    documentName === 'svxlink.conf' ? 'status-svxlink' : 'status-node-info'
  );
  element.textContent = message;
  element.className = `editor-status${isError ? ' error' : ' muted'}`;
}

/** @param {Object} command @returns {Promise<Object>} */
async function enqueueConfigCommand(command) {
  const response = await fetch(`/api/nodes/${encodeURIComponent(nodeId)}/config-commands`, {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(command)
  });
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || `HTTP ${response.status}`);
  for (let attempt = 0; attempt < 300; attempt += 1) {
    await new Promise((resolve) => setTimeout(resolve, 1000));
    const resultResponse = await fetch(`/api/config-commands/${encodeURIComponent(data.command_id)}`, {
      cache: 'no-store'
    });
    const result = await resultResponse.json();
    if (resultResponse.status === 404) throw new Error(result.error || 'Åtgärden hittades inte.');
    if (!resultResponse.ok) throw new Error(result.error || `HTTP ${resultResponse.status}`);
    if (result.status === 'expired') throw new Error('Noden hann inte behandla begäran. Kontrollera att agenten kör.');
    if (result.status === 'completed') return result.result;
    if (result.status === 'failed') throw new Error(result.result.error || 'Åtgärden misslyckades.');
  }
  throw new Error('Begäran tog för lång tid. Kontrollera nodens agentlogg.');
}

/** @param {string} documentName @param {boolean} confirmOverwrite @returns {Promise<void>} */
async function readConfig(documentName, confirmOverwrite = true) {
  const editor = document.getElementById(configNames[documentName]);
  if (
    confirmOverwrite
    && !editor.disabled
    && editor.value !== loadedContents[documentName]
    && !window.confirm(`Läs om ${documentName} och kasta lokala ändringar?`)
  ) return;
  const buttons = document.querySelectorAll(`[data-load="${documentName}"], [data-save="${documentName}"]`);
  buttons.forEach((button) => { button.disabled = true; });
  configStatus(documentName, 'Hämtar filen från noden…');
  try {
    const result = await enqueueConfigCommand({action: 'read', document: documentName});
    editor.value = result.content;
    editor.disabled = false;
    configHashes[documentName] = result.sha256;
    loadedContents[documentName] = result.content;
    if (documentName === 'node_info.json' && window.NodeInfoForm) window.NodeInfoForm.refresh();
    document.querySelector(`[data-save="${documentName}"]`).disabled = false;
    configStatus(documentName, 'Filen är inläst. Ändringar sparas först efter att du bekräftat.');
  } catch (error) {
    configStatus(documentName, error.message, true);
  } finally {
    document.querySelector(`[data-load="${documentName}"]`).disabled = false;
  }
}

/** @param {string} documentName @returns {Promise<void>} */
async function saveConfig(documentName) {
  const editor = document.getElementById(configNames[documentName]);
  if (!configHashes[documentName]) {
    configStatus(documentName, 'Läs in filen från noden innan du sparar.', true);
    return;
  }
  if (documentName === 'node_info.json') {
    try {
      JSON.parse(editor.value);
    } catch (error) {
      configStatus(documentName, `Ogiltig JSON: ${error.message}`, true);
      return;
    }
    const problems = window.NodeInfoForm ? window.NodeInfoForm.validate() : [];
    if (problems.length) {
      configStatus(documentName, `Åtgärda ${problems.length} fel innan du sparar.`, true);
      return;
    }
  }
  if (!window.confirm(`Spara ändringarna till ${documentName} på noden? En säkerhetskopia skapas.`)) return;
  const buttons = document.querySelectorAll(`[data-load="${documentName}"], [data-save="${documentName}"]`);
  buttons.forEach((button) => { button.disabled = true; });
  configStatus(documentName, 'Sparar filen på noden…');
  try {
    const result = await enqueueConfigCommand({
      action: 'write',
      document: documentName,
      content: editor.value,
      sha256: configHashes[documentName]
    });
    configHashes[documentName] = result.sha256;
    loadedContents[documentName] = editor.value;
    configStatus(documentName, `Sparad. Säkerhetskopia: ${result.backup}`);
  } catch (error) {
    configStatus(documentName, error.message, true);
  } finally {
    buttons.forEach((button) => { button.disabled = false; });
  }
}

async function loadNode() {
  try {
    const response = await fetch(`/api/nodes/${encodeURIComponent(nodeId)}`, {cache: 'no-store'});
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const data = await response.json();
    setText('node-name', data.node.name);
  } catch (error) {
    setText('node-name', `Kunde inte läsa nodnamn: ${error.message}`);
  }
}

document.querySelectorAll('[data-load]').forEach((button) => {
  button.addEventListener('click', () => readConfig(button.dataset.load));
});
document.querySelectorAll('[data-save]').forEach((button) => {
  button.addEventListener('click', () => saveConfig(button.dataset.save));
});
loadNode();
Object.keys(configNames)
  .filter((name) => document.getElementById(configNames[name]))
  .forEach((name) => readConfig(name, false));
