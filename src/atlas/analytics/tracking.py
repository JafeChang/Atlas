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
  // Accessing storage itself may throw in restricted browser contexts.
  let visitor, session;
  try { visitor = storedId(window.localStorage, 'atlas_visitor_id'); } catch (_) { visitor = id(); }
  try { session = storedId(window.sessionStorage, 'atlas_session_id'); } catch (_) { session = id(); }
  const view = id(), params = new URLSearchParams(location.search);
  const context = {};
  for (const key of ['offset', 'limit']) {
    const value = params.get(key);
    if (value && /^\d{1,9}$/.test(value)) context[key] = Number(value);
  }
  if (['asc', 'desc'].includes(params.get('order'))) context.order = params.get('order');
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
  const send = (type, extra = {}) => {
    const event = {event_id: id(), visitor_id: visitor, session_id: session, view_id: view,
      event_type: type, page, occurred_at: new Date().toISOString(), ...context, ...extra};
    const body = JSON.stringify(event);
    if (navigator.sendBeacon && navigator.sendBeacon('/events', new Blob([body], {type: 'application/json'}))) return;
    if (window.fetch) fetch('/events', {method: 'POST', headers: {'Content-Type': 'application/json'},
      body, keepalive: true}).catch(() => {});
  };
  send('page_view');
  const detail = document.querySelector('[data-tracking-detail]');
  if (detail) send('detail_view', itemData(detail));
  const cards = Array.from(document.querySelectorAll('[data-tracking-list-item]'));
  cards.forEach((card, index) => { card.dataset.position = String((context.offset || 0) + index); });
  if ('IntersectionObserver' in window) {
    const observer = new IntersectionObserver((entries) => {
      for (const entry of entries) {
        if (entry.isIntersecting && entry.intersectionRect.height > 0) {
          send('list_impression', itemData(entry.target)); observer.unobserve(entry.target);
        }
      }
    }, {threshold: 0});
    cards.forEach(card => observer.observe(card));
  }
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
    if (action) send('click', {...itemData(card), action});
  }, true);
})();
</script>"""
