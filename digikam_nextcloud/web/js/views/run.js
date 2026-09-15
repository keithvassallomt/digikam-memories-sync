// One sync, start to finish: what it found, what it will change, and applying it.

import { api, query } from '../api.js';
import * as copy from '../copy.js';
import { button, card, h, replace, stat } from '../dom.js';
import { count, percent, plural, timeOfDay, when } from '../format.js';

const RESULT_ROWS = [
  ['files_digikam', 'Photos checked in digiKam'],
  ['files_matched', 'Photos matched to Nextcloud'],
  ['files_unmatched_digikam', 'Photos not found in Nextcloud'],
  ['assigned', 'Name existing faces in Memories'],
  ['inserted', 'Create face boxes in Memories'],
  ['created_in_digikam', 'Create face boxes in digiKam'],
  ['reassigned_in_digikam', 'Rename faces in digiKam'],
  ['ignored', 'Kept in one library'],
];

export function create({ go, refresh }) {
  const element = h('section', { class: 'screen' });
  const body = h('div', {});
  let runId = null;
  let timer = null;
  let closing = false;
  let lastReview = null;

  element.append(body);

  function timeline(run, review) {
    const summary = run.summary || {};
    const applyState = run.apply || {};
    const open = run.conflicts_open || 0;
    const settled = run.conflicts_resolved || 0;
    const items = [];
    const finished = ['previewed', 'applying', 'applied', 'applied_with_issues', 'apply_failed', 'no_changes'];
    if (finished.includes(run.status)) {
      items.push(['●', 'good', `Checked ${plural(summary.files_digikam || 0, 'photo', 'photos')} in both libraries`,
        `${timeOfDay(run.started_at)}`]);
    } else {
      items.push(['◐', 'wait', copy.phase((run.progress || {}).phase), 'now']);
    }
    if (settled) {
      items.push(['●', 'good', `You decided ${plural(settled, 'name', 'names')}`, 'done']);
    }
    if (open) {
      items.push(['◐', 'wait', `${plural(open, 'face needs', 'faces need')} a decision`, 'waiting for you']);
    }
    if (applyState.applied) {
      items.push(['●', 'good', `Applied ${plural(applyState.applied, 'change', 'changes')}`, timeOfDay(run.finished_at)]);
    }
    if (run.status === 'previewed' && review && review.total) {
      items.push(['○', 'idle', `${plural(review.total, 'change is', 'changes are')} ready to apply`, 'waiting for you']);
    }
    if (applyState.failed) {
      items.push(['◐', 'wait', `${plural(applyState.failed, 'change', 'changes')} could not be applied`, 'waiting for you']);
    }
    return card(h('ul', { class: 'timeline' },
      ...items.map(([mark, kind, text, right]) => h('li', {},
        h('span', { class: `mark is-${kind}`, 'aria-hidden': 'true' }, mark),
        h('span', {}, text),
        h('span', { class: 'muted' }, right)))));
  }

  function stats(run, review) {
    // One set of numbers for the whole screen. When a plan exists it is the
    // authority, because it includes the decisions you have made.
    const summary = run.summary || {};
    const memories = review
      ? review.memories
      : (summary.assigned || 0) + (summary.inserted || 0);
    const digikam = review ? review.digikam : copy.digikamChangeCount(summary);
    const open = run.conflicts_open ?? summary.conflicts ?? 0;
    return h('div', { class: 'stats' },
      stat(count(summary.skipped || 0), 'Already correct'),
      stat(count(memories), 'Changes for Memories'),
      stat(count(digikam), 'Changes for digiKam'),
      stat(count(open), open ? 'Need your decision' : 'Still to decide'));
  }

  function results(run) {
    const summary = run.summary || {};
    const rows = RESULT_ROWS.filter(([key]) => summary[key] !== undefined);
    if (!rows.length) return null;
    return card(h('h3', {}, 'What the sync found'),
      h('ul', { class: 'result-list' },
        ...rows.map(([key, label]) => h('li', {},
          h('span', {}, label), h('strong', {}, count(summary[key]))))));
  }

  function applyCard(run, review) {
    if (run.status !== 'previewed') return null;
    if (!review || !review.total) {
      return card(h('h3', {}, 'Nothing to apply'),
        h('p', { class: 'muted' }, 'Both libraries already agree about these faces.'),
        h('div', { class: 'actions' },
          button('Discard this preview', { onClick: () => discard() })));
    }
    const message = h('p', { class: 'message', role: 'status' });
    const confirmed = h('input', { type: 'checkbox', onChange: () => { apply.disabled = !allowed(); } });
    const needsClose = review.requires_digikam_closed;

    const allowed = () => !needsClose || confirmed.checked;
    const apply = h('button', {
      type: 'button', class: 'button button-primary', disabled: needsClose,
      onClick: async () => {
        apply.disabled = true;
        message.textContent = 'Starting…';
        message.className = 'message';
        try {
          await api.post(`/api/runs/${runId}/apply`, { digikam_closed: confirmed.checked });
          await load();
        } catch (error) {
          message.textContent = error.message;
          message.className = 'message error';
          apply.disabled = !allowed();
        }
      },
    }, `Apply ${plural(review.total, 'change', 'changes')}`);

    const closeButton = h('button', {
      type: 'button', class: 'button',
      onClick: async () => {
        if (closing) return;
        closing = true;
        closeButton.disabled = true;
        closeButton.textContent = 'Closing digiKam…';
        try {
          await api.post('/api/digikam/close');
          await load();
        } catch (error) {
          message.textContent = error.message;
          message.className = 'message error';
          closeButton.disabled = false;
          closeButton.textContent = 'Close digiKam for me';
        } finally {
          closing = false;
        }
      },
    }, 'Close digiKam for me');

    return h('div', {},
      card(
        h('div', { class: 'row-start' },
          h('span', { class: 'iconbox', 'aria-hidden': 'true' }, '✓'),
          h('div', {},
            h('h3', {}, 'A digiKam backup is made automatically'),
            h('div', { class: 'muted' },
              'Face Sync copies the database before the first local change.')))),
      needsClose
        ? card(
            h('h3', {}, 'Close digiKam before continuing'),
            h('p', { class: 'muted' },
              'digiKam must be closed while Face Sync updates its face database.'),
            h('div', { class: 'row' },
              h('label', { class: 'confirmation' }, confirmed,
                h('span', {}, 'I have closed digiKam')),
              review.digikam_running ? closeButton : null))
        : null,
      message,
      h('div', { class: 'actions' },
        button('Discard this preview', { onClick: () => discard() }),
        apply));
  }

  function applyProgress(run) {
    if (run.status !== 'applying') return null;
    const state = run.progress || {};
    const done = percent(state.current, state.total) ?? 0;
    return card(
      h('div', { class: 'progress-head' },
        h('strong', {}, copy.phase(state.phase)),
        h('span', { class: 'muted' }, `${count(state.current)} of ${count(state.total)}`)),
      h('div', { class: 'progress', role: 'progressbar', 'aria-valuemin': '0', 'aria-valuemax': '100', 'aria-valuenow': String(done) },
        h('span', { style: { width: `${done}%` } })),
      h('p', { class: 'note' },
        'You can close this window. Applying continues while Face Sync is running.'));
  }

  function finishedCard(run) {
    const applyState = run.apply;
    if (!applyState || !['applied', 'applied_with_issues', 'apply_failed'].includes(run.status)) return null;
    const failed = applyState.failed || 0;
    return card(
      h('p', { class: `completion-mark ${failed ? 'is-failed' : ''}`, 'aria-hidden': 'true' }, failed ? '!' : '✓'),
      h('h3', {}, failed
        ? `${plural(failed, 'change', 'changes')} could not be applied`
        : `${plural(applyState.applied || 0, 'change', 'changes')} applied`),
      applyState.ignored
        ? h('p', { class: 'muted' }, `${plural(applyState.ignored, 'face', 'faces')} kept in one library.`)
        : null,
      applyState.backup_path
        ? h('p', { class: 'backup-path' }, `Backup: ${applyState.backup_path}`)
        : null,
      (applyState.errors || []).length
        ? h('ul', { class: 'errors' },
            ...applyState.errors.slice(0, 8).map((item) =>
              h('li', {}, `${item.path}: ${item.error}`)))
        : null,
      failed
        ? h('div', { class: 'actions' },
            button('Review these faces', {
              class: 'button button-primary', onClick: () => go('/attention/failures'),
            }))
        : null);
  }

  async function discard() {
    // Checking both libraries takes minutes, and discarding throws that away
    // along with any decisions already made. Worth one question.
    const review = lastReview || {};
    const parts = [];
    if (review.total) parts.push(plural(review.total, 'proposed change', 'proposed changes'));
    if (review.conflicts) parts.push(plural(review.conflicts, 'decision', 'decisions'));
    const what = parts.length ? parts.join(' and ') : 'this preview';
    const confirmed = window.confirm(
      `Discard ${what}?\n\n`
      + 'Nothing has been changed in either library. Face Sync will have to '
      + 'check both of them again, which takes a few minutes.',
    );
    if (!confirmed) return;
    try {
      await api.post(`/api/runs/${runId}/discard`);
      await refresh();
      go('/');
    } catch (error) {
      window.alert(error.message);
    }
  }

  async function logTail() {
    try {
      const page = await api.get(`/api/logs${query({ run_id: runId, limit: 6 })}`);
      if (!page.entries.length) return null;
      return h('div', {},
        h('div', { class: 'section-head' },
          h('h3', { class: 'section-label' }, 'Log'),
          button('Open Logs →', { class: 'button button-quiet button-small', onClick: () => go('/logs') })),
        h('div', { class: 'logview logview-short' },
          ...page.entries.map((entry) => h('div', { class: 'logline' },
            h('span', { class: 'logtime' }, String(entry.ts).slice(11, 19)),
            h('span', { class: `loglevel lvl-${String(entry.level).toLowerCase()}` }, entry.level),
            h('span', {}, entry.message)))));
    } catch (_) {
      return null;
    }
  }

  async function load() {
    try {
      const run = await api.get(`/api/runs/${runId}`);
      let review = null;
      if (['previewed', 'applying', 'apply_failed', 'applied'].includes(run.status)) {
        try {
          review = await api.get(`/api/runs/${runId}/apply`);
        } catch (_) {
          review = null;
        }
      }
      lastReview = review;
      replace(body,
        button('← Activity', { class: 'button button-quiet button-small back', onClick: () => go('/activity') }),
        h('h1', {}, `Sync · ${when(run.started_at)}`),
        h('p', { class: 'lead' }, `Started by ${copy.trigger(run.trigger)}. ${copy.outcome(run)}.`),
        (run.progress || {}).error
          ? h('p', { class: 'message error' }, run.progress.error)
          : null,
        timeline(run, review),
        stats(run, review),
        results(run),
        applyProgress(run),
        applyCard(run, review),
        finishedCard(run));

      const tail = await logTail();
      if (tail) body.appendChild(tail);

      const busy = ['previewing', 'applying'].includes(run.status);
      clearInterval(timer);
      timer = busy ? setInterval(load, 1500) : null;
    } catch (error) {
      replace(body, h('p', { class: 'message error' }, error.message),
        button('Back to activity', { onClick: () => go('/activity') }));
    }
  }

  return {
    element,
    enter(parameters) {
      runId = Number(parameters.id);
      load();
    },
    leave() {
      clearInterval(timer);
      timer = null;
    },
  };
}
