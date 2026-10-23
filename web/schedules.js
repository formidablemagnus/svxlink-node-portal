const pathParts = window.location.pathname.split('/').filter(Boolean);
const nodeId = pathParts[pathParts.length - 2];
const $ = (id) => document.getElementById(id);
const FIELDS = ['minute', 'hour', 'day', 'month', 'weekday'];
const RANGES = [['minute', 0, 59], ['hour', 0, 23], ['day', 1, 31], ['month', 1, 12], ['weekday', 0, 7]];
let schedules = [];
let cronTarget = '/dev/shm/dtmf_ctrl';
let crontabHash = null;
let editTarget = null;
let dirty = false;

$('detail-link').href = `/nodes/${encodeURIComponent(nodeId)}`;

function setMessage(text, isError = false) {
  $('schedule-message').textContent = text;
  $('schedule-message').className = `editor-status${isError ? ' error' : ' muted'}`;
}

function currentSchedule() {
  const schedule = {label: $('schedule-label').value.trim(), dtmf: $('schedule-dtmf').value.trim()};
  for (const field of FIELDS) schedule[field] = $(`cron-${field}`).value.trim();
  return schedule;
}

function cronLine(schedule) {
  const expression = FIELDS.map((field) => schedule[field]).join(' ');
  return `${expression} printf '\\%s\\n' '${schedule.dtmf}' > ${schedule.target || cronTarget}`;
}

function updatePreview() {
  $('cron-preview').textContent = cronLine(currentSchedule());
}

function cronDescription(schedule) {
  const weekdays = ['söndagar', 'måndagar', 'tisdagar', 'onsdagar', 'torsdagar', 'fredagar', 'lördagar'];
  const months = ['januari', 'februari', 'mars', 'april', 'maj', 'juni', 'juli', 'augusti', 'september', 'oktober', 'november', 'december'];
  const hour = Number(schedule.hour);
  const minute = Number(schedule.minute);
  const time = /^\d+$/.test(schedule.hour) && /^\d+$/.test(schedule.minute) && hour <= 23 && minute <= 59;
  const clock = time ? ` kl. ${String(hour).padStart(2, '0')}:${String(minute).padStart(2, '0')}` : '';
  const dayAll = schedule.day === '*';
  const monthAll = schedule.month === '*';
  const weekdayAll = schedule.weekday === '*';
  if (time && dayAll && monthAll && /^[0-7]$/.test(schedule.weekday)) return weekdays[Number(schedule.weekday) % 7] + clock;
  if (time && dayAll && monthAll && weekdayAll) return `Varje dag${clock}`;
  if (time && /^\d{1,2}$/.test(schedule.day) && monthAll && weekdayAll) {
    const day = Number(schedule.day);
    if (day >= 1 && day <= 31) {
      const suffix = (day % 10 === 1 && day !== 11) || (day % 10 === 2 && day !== 12) ? 'a' : 'e';
      return `Den ${day}:${suffix} varje månad${clock}`;
    }
  }
  if (time && /^\d{1,2}$/.test(schedule.day) && /^\d{1,2}$/.test(schedule.month) && weekdayAll) {
    const day = Number(schedule.day);
    const month = Number(schedule.month);
    if (day >= 1 && day <= 31 && month >= 1 && month <= 12) return `${day} ${months[month - 1]}${clock}`;
  }
  return `Cron-schema: ${FIELDS.map((field) => schedule[field]).join(' ')}`;
}

function validCronField(value, min, max) {
  if (!value || value.length > 20) return false;
  return value.split(',').every((part) => {
    const match = part.match(/^(\*|(\d+)(?:-(\d+))?)(?:\/(\d+))?$/);
    if (!match) return false;
    if (match[4] !== undefined && Number(match[4]) < 1) return false;
    if (match[2] === undefined) return true;
    const start = Number(match[2]);
    const end = match[3] === undefined ? start : Number(match[3]);
    return start >= min && end <= max && start <= end;
  });
}

function renderSchedules() {
  $('schedule-count').textContent = `${schedules.length} schemalägg${schedules.length === 1 ? 'ning' : 'ningar'}`;
  const list = $('schedule-list');
  list.replaceChildren();
  if (!schedules.length) {
    const empty = document.createElement('p');
    empty.className = 'muted';
    empty.textContent = 'Inga schemalagda DTMF-kommandon.';
    list.append(empty);
  }
  schedules.forEach((schedule, index) => {
    const row = document.createElement('div');
    row.className = 'schedule-row';
    const info = document.createElement('div');
    info.className = 'schedule-info';
    const title = document.createElement('strong');
    title.textContent = schedule.label || schedule.dtmf;
    const description = document.createElement('div');
    description.className = 'schedule-description';
    description.textContent = cronDescription(schedule);
    const dtmf = document.createElement('div');
    dtmf.className = 'muted';
    dtmf.append('DTMF: ');
    const code = document.createElement('code');
    code.textContent = schedule.dtmf;
    dtmf.append(code);
    info.append(title, description, dtmf);
    const actions = document.createElement('div');
    actions.className = 'schedule-actions';
    const edit = document.createElement('button');
    edit.type = 'button';
    edit.textContent = 'Ändra';
    edit.addEventListener('click', () => startEdit(index));
    const remove = document.createElement('button');
    remove.type = 'button';
    remove.textContent = 'Ta bort';
    remove.addEventListener('click', () => {
      schedules.splice(index, 1);
      markDirty();
      cancelEdit();
      renderSchedules();
    });
    actions.append(edit, remove);
    row.append(info, actions);
    list.append(row);
  });
  $('save-schedules').disabled = crontabHash === null || !dirty;
}

function markDirty() {
  dirty = true;
  setMessage('Osparade ändringar. Spara för att skriva dem till crontab.');
}

function setForm(schedule) {
  $('schedule-label').value = schedule.label;
  $('schedule-dtmf').value = schedule.dtmf;
  for (const field of FIELDS) $(`cron-${field}`).value = schedule[field];
  updatePreview();
}

function startEdit(index) {
  editTarget = index;
  setForm(schedules[index]);
  $('form-title').textContent = 'Ändra schemaläggning';
  $('add-schedule').textContent = 'Uppdatera schemaläggning';
  $('cancel-edit').hidden = false;
  $('schedule-label').focus();
}

function cancelEdit() {
  editTarget = null;
  $('form-title').textContent = 'Skapa schemaläggning';
  $('add-schedule').textContent = 'Lägg till schemaläggning';
  $('cancel-edit').hidden = true;
}

function applyResult(result) {
  schedules = result.schedules;
  cronTarget = result.cron_target;
  crontabHash = result.sha256;
  dirty = false;
  $('other-crontab').textContent = result.other_lines.length ? result.other_lines.join('\n') : 'Inga övriga crontab-rader.';
  cancelEdit();
  renderSchedules();
  updatePreview();
}

async function enqueueCommand(command) {
  const response = await fetch(`/api/nodes/${encodeURIComponent(nodeId)}/config-commands`, {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(command)
  });
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || `HTTP ${response.status}`);
  for (let attempt = 0; attempt < 120; attempt += 1) {
    await new Promise((resolve) => setTimeout(resolve, 1000));
    const resultResponse = await fetch(`/api/config-commands/${encodeURIComponent(data.command_id)}`, {cache: 'no-store'});
    const result = await resultResponse.json();
    if (!resultResponse.ok) throw new Error(result.error || `HTTP ${resultResponse.status}`);
    if (result.status === 'expired') throw new Error('Noden hann inte behandla begäran. Kontrollera att agenten kör.');
    if (result.status === 'completed') return result.result;
    if (result.status === 'failed') throw new Error(result.result.error || 'Åtgärden misslyckades.');
  }
  throw new Error('Begäran tog för lång tid. Kontrollera nodens agentlogg.');
}

async function loadSchedules() {
  if (dirty && !window.confirm('Läs om crontab och kasta osparade ändringar?')) return;
  $('reload-schedules').disabled = true;
  $('save-schedules').disabled = true;
  setMessage('Läser crontab från noden…');
  try {
    applyResult(await enqueueCommand({action: 'schedules', operation: 'read'}));
    setMessage('Crontab inläst från noden.');
  } catch (error) {
    $('schedule-count').textContent = 'Inte tillgänglig';
    setMessage(`Kunde inte läsa crontab: ${error.message}`, true);
  } finally {
    $('reload-schedules').disabled = false;
  }
}

async function saveSchedules() {
  if (!window.confirm('Spara schemaläggningarna i crontab på noden?')) return;
  $('save-schedules').disabled = true;
  $('reload-schedules').disabled = true;
  setMessage('Sparar i crontab på noden…');
  try {
    applyResult(await enqueueCommand({action: 'schedules', operation: 'write', schedules, sha256: crontabHash}));
    setMessage('Schemaläggningarna sparades i crontab.');
  } catch (error) {
    setMessage(`Kunde inte spara crontab: ${error.message}`, true);
    renderSchedules();
  } finally {
    $('reload-schedules').disabled = false;
  }
}

$('schedule-form').addEventListener('submit', (event) => {
  event.preventDefault();
  const schedule = currentSchedule();
  const invalid = RANGES.find(([field, min, max]) => !validCronField(schedule[field], min, max));
  if (invalid) return setMessage(`Ogiltigt cron-fält: ${invalid[0]}. Kontrollera värde, intervall eller steg.`, true);
  if (!/^[0-9*#]{1,20}$/.test(schedule.dtmf)) return setMessage('DTMF-koden måste innehålla 1–20 tecken: 0–9, * eller #.', true);
  if (editTarget === null && schedules.length >= 100) return setMessage('Du kan ha högst 100 schemaläggningar.', true);
  if (editTarget === null) {
    schedules.push(schedule);
  } else {
    schedules[editTarget] = {...schedule, target: schedules[editTarget].target};
  }
  cancelEdit();
  markDirty();
  renderSchedules();
});
$('cancel-edit').addEventListener('click', cancelEdit);
$('save-schedules').addEventListener('click', saveSchedules);
$('reload-schedules').addEventListener('click', loadSchedules);
document.querySelectorAll('#schedule-form input').forEach((input) => input.addEventListener('input', updatePreview));
window.addEventListener('beforeunload', (event) => { if (dirty) event.preventDefault(); });

(async () => {
  updatePreview();
  try {
    const response = await fetch(`/api/nodes/${encodeURIComponent(nodeId)}`, {cache: 'no-store'});
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    $('node-name').textContent = (await response.json()).node.name;
  } catch (error) {
    $('node-name').textContent = `Kunde inte läsa nodnamn: ${error.message}`;
  }
  loadSchedules();
})();
