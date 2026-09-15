// What the service did, readable without opening a file.

import { api, query } from '../api.js';
import { button, h, replace } from '../dom.js';

const LEVELS = [
  ['INFO', 'Info'],
  ['DEBUG', 'Everything'],
  ['WARNING', 'Warnings'],
  ['ERROR', 'Errors'],
];

export function create() {
  const element = h('section', { class: 'screen screen-wide' });
  const view = h('div', { class: 'logview', tabindex: '0' });
  const footer = h('div', { class: 'row' });
  let entries = [];
  let follow = true;
  let timer = null;
  let newest = null;

  const level = h('select', { class: 'select', 'aria-label': 'Level', onChange: reload },
    ...LEVELS.map(([value, label]) => h('option', { value }, label)));
  const runFilter = h('input', {
    class: 'input input-narrow', type: 'number', min: '1',
    placeholder: 'Any run', 'aria-label': 'Run', onChange: reload,
  });
  const search = h('input', {
    class: 'input', type: 'search', placeholder: 'Search', 'aria-label': 'Search logs',
  });
  search.addEventListener('input', debounce(reload, 250));
  const followBox = h('input', {
    type: 'checkbox', checked: true,
    onChange: (event) => {
      follow = event.currentTarget.checked;
      if (follow) tick();
    },
  });

  element.append(
    h('h1', {}, 'Logs'),
    h('p', { class: 'lead' }, 'Everything the background service did, newest first.'),
    h('div', { class: 'logbar' },
      h('div', { class: 'row-tight' },
        h('label', { class: 'field-inline' }, h('span', { class: 'muted' }, 'Level'), level),
        h('label', { class: 'field-inline' }, h('span', { class: 'muted' }, 'Run'), runFilter),
        search),
      h('label', { class: 'field-inline' }, followBox, h('span', {}, 'Follow'))),
    view,
    footer,
  );

  function debounce(work, delay) {
    let handle = null;
    return (...args) => {
      clearTimeout(handle);
      handle = setTimeout(() => work(...args), delay);
    };
  }

  function filters() {
    return {
      level: level.value,
      run_id: runFilter.value || null,
      q: search.value || null,
      limit: 300,
    };
  }

  function line(entry) {
    const stamp = String(entry.ts || '').slice(11, 19);
    return h('div', { class: 'logline' },
      h('span', { class: 'logtime' }, stamp),
      h('span', { class: `loglevel lvl-${String(entry.level).toLowerCase()}` }, entry.level),
      h('span', { class: 'logrun' }, entry.run_id ? `run ${entry.run_id}` : '—'),
      h('span', {}, entry.message));
  }

  function render() {
    if (!entries.length) {
      replace(view, h('p', { class: 'muted' }, 'No log lines match.'));
    } else {
      replace(view, ...entries.map(line));
    }
    replace(footer,
      h('span', { class: 'muted' }, 'Files live beside the settings, under logs/'),
      button('Copy for bug report', { class: 'button button-small', onClick: copyAll }));
  }

  async function copyAll() {
    const text = entries
      .slice()
      .reverse()
      .map((entry) => `${entry.ts} ${entry.level} ${entry.logger} ${entry.run_id || ''} ${entry.message}`)
      .join('\n');
    try {
      await navigator.clipboard.writeText(text);
      footer.firstChild.textContent = `Copied ${entries.length} lines.`;
    } catch (_) {
      footer.firstChild.textContent = 'Copying was refused by the browser.';
    }
  }

  async function reload() {
    try {
      const page = await api.get(`/api/logs${query(filters())}`);
      entries = page.entries;
      newest = page.newest_id;
      render();
    } catch (error) {
      replace(view, h('p', { class: 'message error' }, error.message));
    }
  }

  async function tick() {
    if (!follow) return;
    try {
      const page = await api.get(`/api/logs${query({ ...filters(), after_id: newest, limit: 100 })}`);
      if (page.entries.length) {
        entries = [...page.entries, ...entries].slice(0, 500);
        newest = page.newest_id;
        render();
      }
    } catch (_) {
      // A dropped poll is not worth reporting; the next one will catch up.
    }
  }

  return {
    element,
    enter() {
      reload();
      timer = setInterval(tick, 3000);
    },
    leave() {
      clearInterval(timer);
      timer = null;
    },
  };
}
