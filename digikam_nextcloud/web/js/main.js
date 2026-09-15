// The shell: one rail, one screen at a time, one status poll feeding both.

import { clear } from './dom.js';
import { timeOfDay } from './format.js';
import * as notify from './notify.js';
import * as router from './router.js';
import * as store from './store.js';

import { create as createActivity } from './views/activity.js';
import { create as createAttention } from './views/attention.js';
import { create as createConflict } from './views/conflict.js';
import { create as createFailure } from './views/failure.js';
import { create as createHome } from './views/home.js';
import { create as createLogs } from './views/logs.js';
import { create as createRun } from './views/run.js';
import { create as createSettings } from './views/settings.js';
import { create as createSetup } from './views/setup.js';

const VIEWS = {
  home: createHome,
  activity: createActivity,
  run: createRun,
  attention: createAttention,
  conflict: createConflict,
  failure: createFailure,
  logs: createLogs,
  settings: createSettings,
  setup: createSetup,
};

// Which rail entry lights up for a screen that is not itself in the rail.
const RAIL_PARENT = { run: 'activity', conflict: 'attention', failure: 'attention' };

const TOPBAR = {
  setup: ['is-off', 'Setup needed'],
  syncing: ['', 'Syncing'],
  attention: ['is-wait', 'Needs your attention'],
  waiting_digikam: ['is-wait', 'Waiting for digiKam'],
  ready: ['', 'Changes ready to apply'],
  paused: ['is-off', 'Paused'],
  off: ['is-off', 'Automatic sync is off'],
  in_sync: ['', 'In sync'],
};

router.define('/', 'home');
router.define('/activity', 'activity');
router.define('/runs/:id', 'run');
router.define('/attention', 'attention');
router.define('/attention/conflicts', 'conflict');
router.define('/attention/failures', 'failure');
router.define('/logs', 'logs');
router.define('/settings', 'settings');
router.define('/setup', 'setup');

const screen = document.getElementById('screen');
const statusDot = document.getElementById('status-dot');
const statusText = document.getElementById('status-text');
const badge = document.getElementById('attention-badge');
const serviceNote = document.getElementById('service-note');
const railButtons = Array.from(document.querySelectorAll('.rail-item'));

const context = {
  go: (path) => router.go(path),
  refresh: () => store.refresh(),
  status: () => store.status(),
};

const instances = new Map();
let active = null;
let configured = true;

function view(name) {
  if (!instances.has(name)) instances.set(name, VIEWS[name](context));
  return instances.get(name);
}

function highlight(name) {
  const wanted = RAIL_PARENT[name] || name;
  for (const item of railButtons) {
    if (item.dataset.nav === wanted) item.setAttribute('aria-current', 'page');
    else item.removeAttribute('aria-current');
  }
}

function show(route) {
  // Nothing works before the libraries are connected, so send people there.
  if (!configured && route.name !== 'setup') {
    router.go('/setup', { replace: true });
    return;
  }
  const next = view(route.name);
  // Always clean up first, including when the same view is re-entered with
  // different parameters, or its timers and object URLs would pile up.
  if (active && active.instance.leave) active.instance.leave();
  if (!active || active.instance !== next) {
    clear(screen);
    screen.appendChild(next.element);
  }
  active = { name: route.name, instance: next };
  highlight(route.name);
  if (next.enter) next.enter(route.parameters || {});
  const current = store.status();
  if (current && next.update) next.update(current);
}

function onStatus(status, error) {
  if (error) {
    statusDot.className = 'dot is-bad';
    statusText.textContent = 'Face Sync is not responding';
    serviceNote.textContent = 'The background service may have stopped.';
    return;
  }
  if (!status) return;

  const wasConfigured = configured;
  configured = status.configured;
  const [dotClass, label] = TOPBAR[status.state] || TOPBAR.in_sync;
  statusDot.className = `dot ${dotClass}`;
  statusText.textContent = label;

  const total = (status.attention || {}).total || 0;
  badge.textContent = String(total);
  badge.hidden = total === 0;
  notify.setBadge(total);
  notify.raise((status.notifications || {}).raise);

  serviceNote.textContent = status.service && status.service.started_at
    ? `Running since ${timeOfDay(status.service.started_at)}`
    : 'Running in the background';

  if (wasConfigured !== configured) {
    router.go(configured ? '/' : '/setup', { replace: true });
    return;
  }
  if (active && active.instance.update) active.instance.update(status);
}

for (const item of railButtons) {
  item.addEventListener('click', () => router.go(item.dataset.route || `/${item.dataset.nav}`));
}
document.getElementById('brand-home').addEventListener('click', () => router.go('/'));
notify.onPermissionChange(() => store.refresh());

// Decide the first screen from the first status, so a fresh install lands on
// setup rather than flashing an empty home.
store.refresh().then((status) => {
  configured = status ? status.configured : true;
  store.subscribe(onStatus);
  router.start(show);
  store.start();
});
