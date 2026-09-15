// The screen that answers "what is Face Sync doing?" in one line.

import { api } from '../api.js';
import * as copy from '../copy.js';
import { button, card, clear, h, replace } from '../dom.js';
import { count, percent, plural, when } from '../format.js';
import { menu } from '../menu.js';
import * as notify from '../notify.js';

function tomorrowMorning() {
  const moment = new Date();
  moment.setDate(moment.getDate() + 1);
  moment.setHours(8, 0, 0, 0);
  return moment.toISOString();
}

export function create({ go, refresh }) {
  const element = h('section', { class: 'screen' });
  const headline = h('p', { class: 'headline' });
  const subline = h('p', { class: 'subline' });
  const detail = h('p', { class: 'subline-detail' });
  const actions = h('div', { class: 'state-actions' });
  const personPanel = h('div', { class: 'person-panel', hidden: true });
  const progress = h('div', {});
  const attention = h('div', {});
  const libraries = h('div', {});
  const activity = h('div', {});
  const message = h('p', { class: 'message', role: 'status' });

  element.append(
    headline, subline, detail, actions, personPanel, message,
    progress, attention, libraries, activity,
  );

  function say(text, kind = '') {
    message.textContent = text;
    message.className = `message ${kind}`;
  }

  async function act(work) {
    try {
      say('');
      await work();
      await refresh();
    } catch (error) {
      say(error.message, 'error');
    }
  }

  const syncMenu = () => menu('Sync now', [
    {
      label: 'Sync everything now',
      onChoose: () => act(() => api.post('/api/sync', { scope: 'all' })),
    },
    { label: 'Sync one person…', onChoose: openPersonPanel },
    {
      label: "Preview only (don't apply)",
      separated: true,
      onChoose: () => act(() => api.post('/api/sync', { scope: 'all', preview_only: true })),
    },
  ], { primary: true });

  const pauseMenu = () => menu('Pause', [
    { label: 'Pause for 1 hour', onChoose: () => act(() => api.post('/api/automation/pause', { minutes: 60 })) },
    { label: 'Pause until tomorrow', onChoose: () => act(() => api.post('/api/automation/pause', { until: tomorrowMorning() })) },
    { label: 'Pause until I resume', onChoose: () => act(() => api.post('/api/automation/pause', { until: 'indefinite' })) },
  ]);

  async function openPersonPanel() {
    personPanel.hidden = false;
    replace(personPanel, h('p', { class: 'muted' }, 'Loading people…'));
    try {
      const { people } = await api.get('/api/people');
      const select = h('select', { class: 'select', 'aria-label': 'Person' },
        ...people.map((name) => h('option', { value: name }, name)));
      replace(personPanel,
        h('div', { class: 'person-row' },
          h('label', { class: 'field' }, 'Person', select),
          button('Sync this person', {
            class: 'button button-primary',
            onClick: () => act(async () => {
              personPanel.hidden = true;
              await api.post('/api/sync', { scope: 'person', person: select.value });
            }),
          }),
          button('Cancel', { onClick: () => { personPanel.hidden = true; } })));
    } catch (error) {
      replace(personPanel,
        h('p', { class: 'message error' }, error.message),
        button('Close', { onClick: () => { personPanel.hidden = true; } }));
    }
  }

  function renderActions(status) {
    clear(actions);
    const run = status.run || {};
    switch (status.state) {
      case 'setup':
        actions.append(button('Open setup', { class: 'button button-primary', onClick: () => go('/setup') }));
        break;
      case 'syncing':
        actions.append(
          button('View details', { class: 'button button-primary', onClick: () => go(`/runs/${run.id}`) }),
          pauseMenu());
        break;
      case 'attention':
        actions.append(
          button('Review', { class: 'button button-primary', onClick: () => go('/attention') }),
          syncMenu(), pauseMenu());
        break;
      case 'waiting_digikam':
        actions.append(syncMenu(), pauseMenu());
        break;
      case 'ready':
        actions.append(
          button('Review changes', { onClick: () => go(`/runs/${run.id}`) }),
          button('Apply', { class: 'button button-primary', onClick: () => go(`/runs/${run.id}`) }));
        break;
      case 'paused':
        actions.append(
          button('Resume', { class: 'button button-primary', onClick: () => act(() => api.post('/api/automation/resume')) }),
          syncMenu());
        break;
      case 'off':
        actions.append(
          button('Turn on automatic sync', {
            class: 'button button-primary',
            onClick: () => act(() => api.post('/api/settings/automation', { enabled: true })),
          }),
          syncMenu());
        break;
      default:
        actions.append(syncMenu(), pauseMenu());
    }
  }

  function renderProgress(status) {
    clear(progress);
    if (status.state !== 'syncing') return;
    const run = status.run || {};
    const state = run.progress || {};
    const done = percent(state.current, state.total);
    progress.append(card(
      h('div', { class: 'progress-head' },
        h('strong', {}, copy.phase(state.phase)),
        h('span', { class: 'muted' },
          done === null
            ? 'Counting photos'
            : `${done}% · ${count(state.current)} of ${count(state.total)}`)),
      h('div', { class: 'progress', role: 'progressbar', 'aria-valuemin': '0', 'aria-valuemax': '100', 'aria-valuenow': String(done ?? 0) },
        h('span', { style: { width: `${done ?? 0}%` } })),
      h('div', { class: 'live-counts' },
        h('span', {}, h('strong', {}, count(state.matched || 0)), ' matched'),
        h('span', {}, h('strong', {}, count(copy.changeCount(state))), ' changes'),
        h('span', {}, h('strong', {}, count(state.conflicts || 0)), ' conflicts'))));
  }

  function renderAttention(status) {
    clear(attention);
    const totals = status.attention || {};
    if (!totals.total) return;
    const rows = [];
    if (totals.conflicts) {
      rows.push(h('div', { class: 'attention-row' },
        h('div', {},
          h('h3', {}, `${plural(totals.conflicts, 'face has', 'faces have')} different names in each library`),
          h('div', { class: 'muted' }, 'Face Sync keeps syncing everything else.')),
        button('Review', { class: 'button button-small', onClick: () => go('/attention/conflicts') })));
    }
    if (totals.failures) {
      rows.push(h('div', { class: 'attention-row' },
        h('div', {},
          h('h3', {}, `${plural(totals.failures, 'face', 'faces')} could not be added`),
          h('div', { class: 'muted' }, 'Memories rejected them.')),
        button('Review', { class: 'button button-small', onClick: () => go('/attention/failures') })));
    }
    if (notify.supported() && notify.permission() === 'default') {
      rows.push(h('div', { class: 'attention-row' },
        h('div', { class: 'muted' },
          'Face Sync can tell you when this happens, even when this window is behind something else.'),
        button('Turn these on', {
          class: 'button button-small',
          onClick: async () => { await notify.request(); await refresh(); },
        })));
    }
    attention.append(h('section', { class: 'attention' }, ...rows));
  }

  function pill(text, kind) {
    return h('span', { class: `pill ${kind}` }, text);
  }

  function renderLibraries(status) {
    const digikam = status.libraries.digikam || {};
    const nextcloud = status.libraries.nextcloud || {};
    replace(libraries, card(
      h('h3', {}, 'Libraries'),
      h('div', { class: 'lib-row' },
        h('span', { 'aria-hidden': 'true' }, '▣'), h('span', {}, 'digiKam'),
        h('span', { class: 'lib-detail' }, digikam.path || 'Not set'),
        digikam.running ? pill('open', 'is-wait') : pill('closed', 'is-good')),
      h('div', { class: 'lib-row' },
        h('span', { 'aria-hidden': 'true' }, '☁'), h('span', {}, 'Nextcloud'),
        h('span', { class: 'lib-detail' },
          nextcloud.url ? `${nextcloud.url} · ${nextcloud.user}` : 'Not set'),
        nextcloud.url ? pill('connected', 'is-good') : pill('not set', 'is-off')),
    ));
  }

  async function renderActivity() {
    try {
      const { runs } = await api.get('/api/runs?limit=4');
      if (!runs.length) {
        replace(activity, h('p', { class: 'muted' }, 'Nothing has run yet.'));
        return;
      }
      replace(activity,
        h('div', { class: 'section-head' },
          h('h3', { class: 'section-label' }, 'Recent activity'),
          button('All activity →', { class: 'button button-quiet button-small', onClick: () => go('/activity') })),
        h('section', { class: 'card card-tight' },
          ...runs.map((run) => h('button', {
            type: 'button', class: 'activity-row', onClick: () => go(`/runs/${run.id}`),
          },
            h('span', { class: 'activity-time' }, when(run.started_at)),
            h('span', {}, copy.outcome(run)),
            h('span', { class: 'tag' }, copy.trigger(run.trigger))))));
    } catch (error) {
      replace(activity, h('p', { class: 'message error' }, error.message));
    }
  }

  let lastRunSignature = '';

  return {
    element,
    enter() {
      renderActivity();
    },
    update(status) {
      if (!status) return;
      headline.textContent = copy.headline(status);
      subline.textContent = copy.subline(status);
      const extra = copy.detail(status);
      detail.textContent = extra;
      detail.hidden = !extra;
      renderActions(status);
      renderProgress(status);
      renderAttention(status);
      renderLibraries(status);
      // Only re-fetch the list when a run actually changed.
      const signature = `${(status.run || {}).id}:${(status.run || {}).status}:${(status.last_completed || {}).id}`;
      if (signature !== lastRunSignature) {
        lastRunSignature = signature;
        renderActivity();
      }
    },
  };
}
