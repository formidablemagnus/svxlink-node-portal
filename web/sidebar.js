// Gemensam vänstermeny. Alla sidor renderar menyn härifrån så att den är identisk överallt.
(function () {
  const MENU = [
    {id: 'dashboard', label: 'Dashboard', href: '/'},
    {id: 'dtmf', label: 'DTMF-styrning', href: '/dtmf'},
    {heading: 'NODER'},
    {nodes: true},
    {id: 'settings', label: 'Portalinställningar', href: '/#portal-settings'},
  ];
  const FOOTER = 'Central nodöversikt';
  const SVG = 'viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"';
  const ICONS = {
    // En enkel dubbelriktad pil: samma frekvens för sändning och mottagning.
    simplex: `<svg ${SVG}><path d="M4 12h16"></path><path d="M8 8l-4 4 4 4"></path><path d="M16 8l4 4-4 4"></path></svg>`,
    // Antenn med två kanaler: mottagning och återutsändning.
    repeater: `<svg ${SVG}><path d="M12 12v8"></path><circle cx="12" cy="10" r="1.5"></circle><path d="M8.5 6.5a5 5 0 0 0 0 7"></path><path d="M15.5 6.5a5 5 0 0 1 0 7"></path><path d="M5.5 4a9 9 0 0 0 0 12"></path><path d="M18.5 4a9 9 0 0 1 0 12"></path></svg>`,
    unknown: `<svg ${SVG}><circle cx="12" cy="12" r="8"></circle><path d="M12 8v4"></path><path d="M12 16h.01"></path></svg>`,
  };

  function logicType(logic) {
    const value = String(logic || '').toLowerCase();
    if (value.includes('repeater')) return 'repeater';
    if (value.includes('simplex')) return 'simplex';
    return 'unknown';
  }

  const LOGIC_TITLES = {simplex: 'Simplexlogik', repeater: 'Repeaterlogik', unknown: 'Okänd logik'};

  function currentPath() {
    return window.location.pathname;
  }

  function buildSidebar(aside) {
    const active = aside.dataset.active;
    const brand = document.createElement('a');
    brand.className = 'brand';
    brand.href = '/';
    brand.append('SvxLink ');
    const brandSpan = document.createElement('span');
    brandSpan.textContent = 'PORTAL';
    brand.append(brandSpan);

    const nav = document.createElement('nav');
    nav.setAttribute('aria-label', 'Huvudmeny');
    for (const item of MENU) {
      if (item.heading) {
        const heading = document.createElement('div');
        heading.className = 'nav-heading';
        heading.textContent = item.heading;
        nav.append(heading);
      } else if (item.nodes) {
        const list = document.createElement('div');
        list.id = 'node-nav';
        list.className = 'node-nav';
        list.textContent = 'Läser noder…';
        nav.append(list);
      } else {
        const link = document.createElement('a');
        link.href = item.href;
        link.textContent = item.label;
        if (item.id === active) link.className = 'active';
        nav.append(link);
      }
    }

    const footer = document.createElement('div');
    footer.className = 'sidebar-footer';
    footer.textContent = FOOTER;
    aside.replaceChildren(brand, nav, footer);
  }

  function renderNodes(nodes) {
    const nav = document.getElementById('node-nav');
    nav.replaceChildren();
    if (!nodes.length) {
      nav.textContent = 'Inga registrerade noder';
      return;
    }
    for (const node of nodes) {
      const type = logicType(node.status?.logic);
      const link = document.createElement('a');
      const href = `/nodes/${encodeURIComponent(node.id)}`;
      link.className = `nav-node ${node.online ? 'node-online' : 'node-offline'}`;
      link.href = href;
      if (currentPath() === href || currentPath() === `${href}/config`) link.classList.add('active');
      link.title = `${LOGIC_TITLES[type]} – ${node.online ? 'online' : 'offline'}`;
      const icon = document.createElement('span');
      icon.className = `node-icon logic-${type}`;
      icon.innerHTML = ICONS[type];
      const name = document.createElement('span');
      name.className = 'node-name';
      name.textContent = node.name;
      link.append(icon, name);
      nav.append(link);
    }
  }

  async function refreshNodes() {
    try {
      const response = await fetch('/api/nodes', {cache: 'no-store'});
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const data = await response.json();
      renderNodes(data.nodes);
      document.dispatchEvent(new CustomEvent('nodes-loaded', {detail: data.nodes}));
    } catch (error) {
      const nav = document.getElementById('node-nav');
      if (nav && !nav.querySelector('a')) nav.textContent = 'Kunde inte läsa nodlistan';
    }
  }

  const aside = document.getElementById('sidebar');
  if (aside) {
    buildSidebar(aside);
    refreshNodes();
    setInterval(refreshNodes, 5000);
  }
})();
