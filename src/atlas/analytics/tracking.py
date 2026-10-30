"""Browser-side interaction collection; no third-party JavaScript or build step."""

TRACKING_SCRIPT = r"""<script>
(() => {
  const page = location.pathname;
  if (!['/', '/feed', '/article', '/entry'].includes(page)) return;
  const id = () => (window.crypto && crypto.randomUUID) ? crypto.randomUUID() :
    Date.now().toString(36) + '-' + Math.random().toString(36).slice(2) + Math.random().toString(36).slice(2);
  const storedId = (storage, key) => {
    try { let value = storage.getItem(key); if (!value) { value = id(); storage.setItem(key, value); } return value; }
    catch (_) { return id(); }
  };
  let visitor, session;
  try { visitor = storedId(window.localStorage, 'atlas_visitor_id'); } catch (_) { visitor = id(); }
  try { session = storedId(window.sessionStorage, 'atlas_session_id'); } catch (_) { session = id(); }
  const view = id();
  const owner = document.querySelector('meta[name="atlas-user-id"]')?.content || '';
  const prefix = 'atlas_events_v2:' + (owner || 'anonymous') + ':';
  const queue = new Map();
  const maxAge = 7 * 24 * 60 * 60 * 1000, maxEvents = 1000;
  let storage;
  try {
    storage = window.localStorage;
    const stale = [];
    for (let i = 0; i < storage.length; i++) {
      const key = storage.key(i);
      if (key && key.startsWith(prefix)) {
        try {
          const event = JSON.parse(storage.getItem(key));
          if (event && event.event_id && Date.now() - Date.parse(event.occurred_at) <= maxAge) queue.set(key, event);
          else stale.push(key);
        } catch (_) { stale.push(key); }
      }
    }
    stale.forEach(key => storage.removeItem(key));
  } catch (_) { storage = null; }
  const feed = document.querySelector('[data-feed-context]');
  const context = {collection_version: 'v2'};
  if (feed) {
    context.strategy = feed.dataset.strategy;
    context.order = feed.dataset.order;
    for (const key of ['offset', 'limit', 'total']) context[key] = Number(feed.dataset[key]);
    context.filter_mask = Number(feed.dataset.filterMask);
  }
  const itemData = (node) => {
    if (!node) return {};
    const result = {};
    for (const [attribute, key] of [['rawId', 'raw_id'], ['entryId', 'entry_id'],
      ['rawSha256', 'raw_sha256'], ['channelId', 'channel_id']]) {
      if (node.dataset[attribute]) result[key] = node.dataset[attribute];
    }
    for (const [attribute, key] of [['charStart', 'char_start'], ['charEnd', 'char_end'], ['position', 'position']]) {
      if (/^\d{1,9}$/.test(node.dataset[attribute] || '')) result[key] = Number(node.dataset[attribute]);
    }
    return result;
  };
  let flushing = false, retryTimer = null, retryDelay = 1000;
  const remove = (key) => {
    queue.delete(key);
    try { storage?.removeItem(key); } catch (_) { /* storage unavailable */ }
  };
  const retry = () => {
    if (retryTimer) return;
    retryTimer = setTimeout(() => { retryTimer = null; flush(); }, retryDelay);
    retryDelay = Math.min(retryDelay * 2, 60000);
  };
  async function flush() {
    if (flushing || !window.fetch || !queue.size) return;
    flushing = true;
    try {
      for (const [key, event] of queue) {
        let response;
        try {
          response = await fetch('/events', {method: 'POST', headers: {'Content-Type': 'application/json'},
            body: JSON.stringify(event), keepalive: true});
        } catch (_) { retry(); break; }
        if (response.ok || [400, 413, 415, 422].includes(response.status)) {
          remove(key); retryDelay = 1000;
        } else { retry(); break; }
      }
    } finally { flushing = false; }
  }
  const send = (type, extra = {}) => {
    const event = {event_id: id(), visitor_id: visitor, session_id: session, view_id: view,
      event_type: type, page, occurred_at: new Date().toISOString(), ...context, ...extra};
    if (owner) event.expected_user_id = owner;
    const key = prefix + event.event_id;
    while (queue.size >= maxEvents) remove(queue.keys().next().value);
    queue.set(key, event);
    try { storage?.setItem(key, JSON.stringify(event)); } catch (_) { /* keep in memory */ }
    flush();
  };
  window.addEventListener('online', flush);
  send('page_view');
  const detail = document.querySelector('[data-tracking-detail]');
  if (detail) {
    try {
      const parent = JSON.parse(window.sessionStorage.getItem('atlas_parent_view') || 'null');
      const item = itemData(detail);
      if (parent && Date.now() - parent.time < 600000 && parent.raw_id === item.raw_id &&
          parent.char_start === item.char_start && parent.char_end === item.char_end) context.parent_view_id = parent.view_id;
      window.sessionStorage.removeItem('atlas_parent_view');
    } catch (_) { /* session storage unavailable */ }
    send('detail_view', itemData(detail));
  }
  const cards = Array.from(document.querySelectorAll('[data-tracking-list-item]'));
  cards.forEach((card, index) => {
    card.dataset.position = String((context.offset || 0) + index);
    send('list_served', itemData(card));
  });
  if ('IntersectionObserver' in window) {
    const visible = new Set(), started = new Map();
    const observer = new IntersectionObserver((entries) => {
      for (const entry of entries) {
        if (entry.isIntersecting) visible.add(entry.target);
        else { visible.delete(entry.target); started.delete(entry.target); }
      }
    }, {threshold: 0});
    cards.forEach(card => observer.observe(card));
    const check = () => {
      if (document.visibilityState !== 'visible' || !document.hasFocus()) { started.clear(); return; }
      const now = performance.now(), width = window.innerWidth, height = window.innerHeight;
      for (const card of visible) {
        const rect = card.getBoundingClientRect();
        const scrollport = card.closest('.feed-items');
        const clip = scrollport ? scrollport.getBoundingClientRect() : {left: 0, top: 0, right: width, bottom: height};
        const left = Math.max(0, clip.left), right = Math.min(width, clip.right);
        let top = Math.max(0, clip.top);
        const header = document.querySelector('.feed-header');
        if (header && getComputedStyle(header).position === 'sticky') {
          top = Math.max(top, header.getBoundingClientRect().bottom);
        }
        const bottom = Math.min(height, clip.bottom);
        const area = Math.max(0, Math.min(rect.right, right) - Math.max(rect.left, left)) *
          Math.max(0, Math.min(rect.bottom, bottom) - Math.max(rect.top, top));
        const reference = Math.min(rect.width, Math.max(0, right - left)) * Math.min(rect.height, Math.max(0, bottom - top));
        if (reference <= 0 || area < reference * 0.5) { started.delete(card); continue; }
        if (!started.has(card)) started.set(card, now);
        else if (now - started.get(card) >= 1000) {
          send('list_impression', {...itemData(card), visible_ms: Math.round(now - started.get(card))});
          observer.unobserve(card); visible.delete(card); started.delete(card);
        }
      }
    };
    setInterval(check, 250);
    document.addEventListener('visibilitychange', check);
    window.addEventListener('blur', check);
  }
  if (detail) {
    let active = 0, since = null, maxScroll = 0, lastReported = 0;
    let lastActivity = performance.now();
    const idleLimit = 60000;
    const update = () => {
      const now = performance.now();
      if (since !== null) active += Math.max(0, Math.min(now, lastActivity + idleLimit) - since);
      since = document.visibilityState === 'visible' && document.hasFocus() && now < lastActivity + idleLimit ? now : null;
      const height = Math.max(0, document.documentElement.scrollHeight - window.innerHeight);
      maxScroll = Math.max(maxScroll, height ? Math.min(100, Math.round(window.scrollY / height * 100)) : 100);
    };
    const report = () => {
      update();
      const read = Math.round(active);
      if (read >= 1000 && read > lastReported) {
        send('read_progress', {...itemData(detail), read_ms: read, scroll_pct: maxScroll});
        lastReported = read;
      }
    };
    update();
    setInterval(report, 15000);
    document.addEventListener('visibilitychange', () => { report(); flush(); });
    window.addEventListener('blur', report);
    const activity = () => { update(); lastActivity = performance.now(); update(); };
    window.addEventListener('focus', activity);
    window.addEventListener('pageshow', activity);
    for (const type of ['scroll', 'pointerdown', 'keydown', 'touchstart']) {
      window.addEventListener(type, activity, {passive: true});
    }
    window.addEventListener('pagehide', () => { report(); since = null; });
  }
  window.addEventListener('pagehide', flush);
  document.addEventListener('click', (event) => {
    const target = event.target instanceof Element ? event.target : event.target.parentElement;
    if (!target) return;
    const control = target.closest('button, a, summary');
    const card = target.closest('[data-tracking-list-item], [data-tracking-detail]');
    let action;
    if (control) {
      if (control.matches('button')) {
        const form = control.closest('form');
        const value = form && form.querySelector('input[name="label_value"]');
        if (value && ['valid', 'invalid'].includes(value.value)) action = 'label_' + value.value;
        else if (control.id === 'tab-original' || control.id === 'tab-chinese') action = control.id;
        else if (control.classList.contains('menu-trigger')) action = 'open_menu';
        else if (control.classList.contains('sheet-close')) action = 'close_menu';
        else if (form && form.action.endsWith('/label')) action = 'submit_label';
        else if (form && form.action.includes('/feed')) action = 'filter';
        else action = 'button';
      } else if (control.matches('summary')) action = 'expand';
      else if (control.classList.contains('title')) action = 'open_detail';
      else if (control.matches('a[aria-label="阅读原文"]') || control.closest('.endpoint')) action = 'open_source';
      else action = 'link';
    } else if (card && card.hasAttribute('data-tracking-list-item') && event.button === 0 &&
      !event.metaKey && !event.ctrlKey && !event.shiftKey && !event.altKey && !window.getSelection().toString()) action = 'open_detail';
    if (action === 'open_detail' && card && card.hasAttribute('data-tracking-list-item')) {
      try { window.sessionStorage.setItem('atlas_parent_view', JSON.stringify({...itemData(card), view_id: view, time: Date.now()})); }
      catch (_) { /* session storage unavailable */ }
    }
    if (action) send('click', {...itemData(card), action});
  }, true);
  flush();
})();
</script>"""
