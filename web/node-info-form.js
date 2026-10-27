// Formulär för node_info.json. Textarean är källan till sanning; formuläret
// speglar den och skriver tillbaka JSON vid varje ändring.
(function () {
  const textarea = document.getElementById('editor-node-info');
  const root = document.getElementById('node-info-form');
  const errorBox = document.getElementById('node-info-errors');
  const tabs = document.querySelectorAll('[data-node-info-view]');
  const NODE_CLASSES = ['simplex', 'duplex', 'repeater', 'link'];
  const SQL_TYPES = ['CTCSS', 'SIGLEV', 'CTCSS & SIGLEV', 'DTMF', 'OPEN', 'VOX', 'Other'];
  const ANT_TYPES = ['omni', 'directional', 'sector', 'yagi', 'other'];
  let model = {};
  let parseError = null;

  const isObject = (v) => v !== null && typeof v === 'object' && !Array.isArray(v);
  const ensureObject = (parent, key) => {
    if (!isObject(parent[key])) parent[key] = {};
    return parent[key];
  };
  const ensureArray = (parent, key) => {
    if (!Array.isArray(parent[key])) parent[key] = [];
    return parent[key];
  };

  function el(tag, attrs = {}, children = []) {
    const node = document.createElement(tag);
    Object.entries(attrs).forEach(([k, v]) => {
      if (k === 'class') node.className = v;
      else if (k === 'text') node.textContent = v;
      else node.setAttribute(k, v);
    });
    children.forEach((c) => node.append(c));
    return node;
  }

  function field(label, input) {
    return el('label', {class: 'nif-field'}, [el('span', {text: label}), input]);
  }

  function textInput(obj, key, {type = 'text', numeric = false, optional = false, list, placeholder} = {}) {
    const input = el('input', {type: numeric ? 'text' : type, autocomplete: 'off'});
    if (numeric) input.inputMode = 'decimal';
    if (placeholder) input.placeholder = placeholder;
    if (list) input.setAttribute('list', list);
    input.value = obj[key] === undefined || obj[key] === null ? '' : String(obj[key]);
    input.addEventListener('input', () => {
      const raw = input.value.trim();
      if (raw === '' && optional) delete obj[key];
      else if (numeric && raw !== '' && /^-?\d+(\.\d+)?$/.test(raw)) obj[key] = Number(raw);
      else obj[key] = input.value;
      changed();
    });
    return input;
  }

  function button(text, onClick, cls = '') {
    const b = el('button', {type: 'button', class: cls, text});
    b.addEventListener('click', () => { onClick(); changed(true); });
    return b;
  }

  // Mottagare lagras som lista, sändare som enskilt tal (flera värden blir lista).
  function ctcssInput(item, isTx) {
    const current = item.ctcssFreq;
    const input = el('input', {type: 'text', autocomplete: 'off', placeholder: isTx ? '79.7' : '79.7, 88.5'});
    input.inputMode = 'decimal';
    input.value = Array.isArray(current) ? current.join(', ') : (current === undefined ? '' : String(current));
    input.addEventListener('input', () => {
      const parts = input.value.split(/[,\s]+/).filter(Boolean);
      if (parts.length === 0) {
        delete item.ctcssFreq;
      } else {
        const values = parts.map((p) => (/^\d+(\.\d+)?$/.test(p) ? Number(p) : p));
        item.ctcssFreq = isTx && values.length === 1 ? values[0] : values;
      }
      changed();
    });
    return input;
  }

  function antennas(parent, withExtras) {
    const list = ensureArray(parent, 'ant');
    const wrap = el('div', {class: 'nif-sub'}, [el('h5', {text: 'Antenner'})]);
    list.forEach((ant, i) => {
      if (!isObject(ant)) { list[i] = {}; ant = list[i]; }
      const row = el('div', {class: 'nif-row'}, [
        field('Beskrivning', textInput(ant, 'comment', {optional: true})),
        field('Höjd (m)', textInput(ant, 'height', {optional: true})),
        field('Riktning (°)', textInput(ant, 'dir', {optional: true}))
      ]);
      if (withExtras) {
        row.append(
          field('Förstärkning (dBi)', textInput(ant, 'gain', {optional: true})),
          field('Typ', textInput(ant, 'Antenna_type', {optional: true, list: 'nif-ant-types'}))
        );
      }
      row.append(button('Ta bort', () => list.splice(i, 1), 'nif-remove'));
      wrap.append(row);
    });
    wrap.append(button('Lägg till antenn', () => list.push({}), 'nif-add'));
    return wrap;
  }

  function radioSection(qth, key, title, isTx) {
    const group = ensureObject(qth, key);
    const wrap = el('div', {class: 'nif-sub'}, [el('h4', {text: title})]);
    Object.keys(group).forEach((id) => {
      const item = isObject(group[id]) ? group[id] : (group[id] = {});
      const idInput = el('input', {type: 'text', maxlength: '8', autocomplete: 'off', value: id});
      idInput.addEventListener('change', () => {
        const next = idInput.value.trim();
        if (!next || next === id || next in group) { idInput.value = id; return; }
        const entries = Object.entries(group).map(([k, v]) => [k === id ? next : k, v]);
        Object.keys(group).forEach((k) => delete group[k]);
        entries.forEach(([k, v]) => { group[k] = v; });
        changed(true);
      });
      const row = el('div', {class: 'nif-row'}, [
        field('ID', idInput),
        field('Namn', textInput(item, 'name')),
        field('Frekvens (MHz)', textInput(item, 'freq', {numeric: true}))
      ]);
      if (isTx) {
        row.append(field('Effekt (W)', textInput(item, 'pwr', {optional: true})));
      } else {
        row.append(field('Squelch', textInput(item, 'sqlType', {optional: true, list: 'nif-sql-types'})));
      }
      row.append(field(isTx ? 'CTCSS-ton (Hz)' : 'CTCSS-toner (Hz, kommaseparerade)', ctcssInput(item, isTx)));
      row.append(button('Ta bort', () => { delete group[id]; }, 'nif-remove'));
      wrap.append(row, antennas(item, isTx));
    });
    wrap.append(button(`Lägg till ${isTx ? 'sändare' : 'mottagare'}`, () => {
      let n = 1;
      const prefix = isTx ? 'T' : 'A';
      let id = prefix;
      while (id in group) { n += 1; id = prefix + n; }
      group[id] = {name: `${isTx ? 'Tx' : 'Rx'}${Object.keys(group).length + 1}`};
    }, 'nif-add'));
    return wrap;
  }

  function qthSection() {
    const list = ensureArray(model, 'qth');
    const wrap = el('fieldset', {class: 'nif-group'}, [el('legend', {text: 'Platser (QTH)'})]);
    list.forEach((qth, i) => {
      if (!isObject(qth)) { list[i] = {}; qth = list[i]; }
      const pos = ensureObject(qth, 'pos');
      const card = el('div', {class: 'nif-card'}, [
        el('div', {class: 'nif-row'}, [
          field('Platsnamn', textInput(qth, 'name')),
          field('Latitud', textInput(pos, 'lat', {optional: true})),
          field('Longitud', textInput(pos, 'long', {optional: true})),
          field('Lokator', textInput(pos, 'loc', {optional: true, placeholder: 'JO65NJ'})),
          button('Ta bort plats', () => list.splice(i, 1), 'nif-remove')
        ]),
        radioSection(qth, 'rx', 'Mottagare', false),
        radioSection(qth, 'tx', 'Sändare', true)
      ]);
      wrap.append(card);
    });
    wrap.append(button('Lägg till plats', () => list.push({name: '', pos: {}, rx: {}, tx: {}}), 'nif-add'));
    return wrap;
  }

  function toneSection() {
    const map = ensureObject(model, 'toneToTalkgroup');
    const wrap = el('fieldset', {class: 'nif-group'}, [
      el('legend', {text: 'CTCSS-ton till talgrupp'})
    ]);
    Object.keys(map).forEach((tone) => {
      const toneInput = el('input', {type: 'text', inputMode: 'decimal', value: tone, autocomplete: 'off'});
      toneInput.addEventListener('change', () => {
        const next = toneInput.value.trim();
        if (!next || next === tone || next in map) { toneInput.value = tone; return; }
        const entries = Object.entries(map).map(([k, v]) => [k === tone ? next : k, v]);
        Object.keys(map).forEach((k) => delete map[k]);
        entries.forEach(([k, v]) => { map[k] = v; });
        changed(true);
      });
      const tg = el('input', {type: 'text', inputMode: 'numeric', autocomplete: 'off', value: String(map[tone])});
      tg.addEventListener('input', () => {
        const raw = tg.value.trim();
        map[tone] = /^\d+$/.test(raw) ? Number(raw) : tg.value;
        changed();
      });
      wrap.append(el('div', {class: 'nif-row'}, [
        field('Ton (Hz)', toneInput),
        field('Talgrupp', tg),
        button('Ta bort', () => { delete map[tone]; }, 'nif-remove')
      ]));
    });
    wrap.append(button('Lägg till ton', () => {
      let n = 1;
      let key = '100';
      while (key in map) { n += 1; key = String(100 + n); }
      map[key] = 0;
    }, 'nif-add'));
    return wrap;
  }

  function render() {
    root.replaceChildren();
    if (parseError) {
      root.append(el('p', {class: 'muted', text: 'Formuläret är inaktivt tills JSON-texten är giltig.'}));
      return;
    }
    const hidden = el('input', {type: 'checkbox'});
    hidden.checked = model.hidden === true;
    hidden.addEventListener('change', () => { model.hidden = hidden.checked; changed(); });
    const classInput = textInput(model, 'nodeClass', {list: 'nif-node-classes'});
    root.append(
      datalist('nif-node-classes', NODE_CLASSES),
      datalist('nif-sql-types', SQL_TYPES),
      datalist('nif-ant-types', ANT_TYPES),
      el('fieldset', {class: 'nif-group'}, [
        el('legend', {text: 'Allmänt'}),
        el('div', {class: 'nif-row'}, [
          field('Nodplats', textInput(model, 'nodeLocation')),
          field('Nodklass', classInput),
          field('Ansvarig (anropssignal)', textInput(model, 'sysop')),
          el('label', {class: 'nif-field nif-check'}, [hidden, el('span', {text: 'Dold nod'})])
        ])
      ]),
      toneSection(),
      qthSection()
    );
  }

  function datalist(id, values) {
    return el('datalist', {id}, values.map((v) => el('option', {value: v})));
  }

  function validate() {
    if (parseError) return [`Ogiltig JSON: ${parseError}`];
    const errors = [];
    const m = model;
    if (!isObject(m)) return ['Roten måste vara ett JSON-objekt.'];
    if (typeof m.nodeLocation !== 'string' || !m.nodeLocation.trim()) errors.push('Nodplats saknas.');
    if (typeof m.nodeClass !== 'string' || !m.nodeClass.trim()) errors.push('Nodklass saknas.');
    if (typeof m.sysop !== 'string' || !/^[A-Za-z0-9\/]{3,12}$/.test(m.sysop.trim())) {
      errors.push('Ansvarig måste vara en anropssignal (t.ex. SA7MHN).');
    }
    if (m.hidden !== undefined && typeof m.hidden !== 'boolean') errors.push('"hidden" måste vara true eller false.');
    const tones = isObject(m.toneToTalkgroup) ? m.toneToTalkgroup : {};
    Object.entries(tones).forEach(([tone, tg]) => {
      if (!/^\d+(\.\d+)?$/.test(tone)) errors.push(`Ogiltig ton "${tone}".`);
      if (!Number.isInteger(tg) || tg < 0) errors.push(`Talgrupp för ton ${tone} måste vara ett heltal.`);
    });
    const qths = Array.isArray(m.qth) ? m.qth : [];
    if (qths.length === 0) errors.push('Minst en plats (QTH) krävs.');
    qths.forEach((q, i) => {
      const label = `Plats ${i + 1}`;
      if (!isObject(q)) { errors.push(`${label} har fel format.`); return; }
      if (typeof q.name !== 'string' || !q.name.trim()) errors.push(`${label}: namn saknas.`);
      const pos = isObject(q.pos) ? q.pos : {};
      if (pos.lat !== undefined && !(Math.abs(Number(pos.lat)) <= 90 && String(pos.lat).trim() !== '')) {
        errors.push(`${label}: latitud måste vara mellan -90 och 90.`);
      }
      if (pos.long !== undefined && !(Math.abs(Number(pos.long)) <= 180 && String(pos.long).trim() !== '')) {
        errors.push(`${label}: longitud måste vara mellan -180 och 180.`);
      }
      if (pos.loc !== undefined && !/^[A-Ra-r]{2}\d{2}([A-Xa-x]{2}(\d{2})?)?$/.test(pos.loc)) {
        errors.push(`${label}: ogiltig lokator.`);
      }
      [['rx', 'mottagare'], ['tx', 'sändare']].forEach(([key, name]) => {
        Object.entries(isObject(q[key]) ? q[key] : {}).forEach(([id, item]) => {
          const where = `${label}, ${name} ${id}`;
          if (!isObject(item)) { errors.push(`${where}: fel format.`); return; }
          if (typeof item.name !== 'string' || !item.name.trim()) errors.push(`${where}: namn saknas.`);
          if (typeof item.freq !== 'number' || !(item.freq > 0)) errors.push(`${where}: frekvens måste vara ett positivt tal.`);
          if (item.pwr !== undefined && !(Number(item.pwr) >= 0 && String(item.pwr).trim() !== '')) {
            errors.push(`${where}: effekt måste vara ett tal.`);
          }
          if (item.ctcssFreq !== undefined) {
            const tones = Array.isArray(item.ctcssFreq) ? item.ctcssFreq : [item.ctcssFreq];
            if (!tones.every((t) => typeof t === 'number' && t > 0)) {
              errors.push(`${where}: CTCSS-toner måste vara positiva tal.`);
            }
          }
          (Array.isArray(item.ant) ? item.ant : []).forEach((a, n) => {
            ['height', 'dir', 'gain'].forEach((k) => {
              if (isObject(a) && a[k] !== undefined && !Number.isFinite(Number(a[k]))) {
                errors.push(`${where}, antenn ${n + 1}: "${k}" måste vara ett tal.`);
              }
            });
          });
        });
      });
    });
    return errors;
  }

  function showErrors() {
    const errors = validate();
    errorBox.replaceChildren(...errors.map((e) => el('li', {text: e})));
    errorBox.hidden = errors.length === 0;
    return errors;
  }

  function changed(rerender = false) {
    textarea.value = JSON.stringify(model, null, 4);
    if (rerender) render();
    showErrors();
  }

  function refresh() {
    try {
      const parsed = JSON.parse(textarea.value || '{}');
      if (!isObject(parsed)) throw new Error('Roten måste vara ett objekt.');
      model = parsed;
      parseError = null;
    } catch (error) {
      parseError = error.message;
    }
    render();
    showErrors();
  }

  function setView(view) {
    const showForm = view === 'form';
    root.hidden = !showForm;
    errorBox.hidden = errorBox.childElementCount === 0;
    textarea.hidden = showForm;
    tabs.forEach((t) => t.setAttribute('aria-pressed', String(t.dataset.nodeInfoView === view)));
  }

  textarea.addEventListener('input', refresh);
  tabs.forEach((t) => t.addEventListener('click', () => setView(t.dataset.nodeInfoView)));
  setView('form');
  window.NodeInfoForm = {refresh, validate: () => { refresh(); return validate(); }};
})();
