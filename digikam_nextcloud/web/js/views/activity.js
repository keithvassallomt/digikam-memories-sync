// Every run, newest first, grouped by day.

import { api, query } from '../api.js';
import * as copy from '../copy.js';
import { button, h, replace } from '../dom.js';
import { dayLabel, timeOfDay } from '../format.js';

export function create({ go }) {
  const element = h('section', { class: 'screen' });
  const list = h('div', {});
  const more = h('div', { class: 'actions' });
  let runs = [];
  let hasMore = false;
  let oldest = null;

  element.append(
    h('h1', {}, 'Activity'),
    h('p', { class: 'lead' }, 'Every sync, what started it and what it changed.'),
    list,
    more,
  );

  function render() {
    if (!runs.length) {
      replace(list, h('p', { class: 'muted' }, 'Nothing has run yet.'));
      replace(more);
      return;
    }
    const groups = [];
    for (const run of runs) {
      const day = dayLabel(run.started_at);
      const last = groups[groups.length - 1];
      if (!last || last.day !== day) groups.push({ day, runs: [run] });
      else last.runs.push(run);
    }
    replace(list, ...groups.map((group) => h('div', {},
      h('h3', { class: 'section-label' }, group.day),
      h('section', { class: 'card card-tight' },
        ...group.runs.map((run) => h('button', {
          type: 'button', class: 'activity-row', onClick: () => go(`/runs/${run.id}`),
        },
          h('span', { class: 'activity-time' }, timeOfDay(run.started_at)),
          h('span', {}, copy.outcome(run)),
          h('span', { class: 'tag' }, copy.trigger(run.trigger))))))));
    replace(more, hasMore
      ? button('Show older', { onClick: () => load({ append: true }) })
      : h('span', { class: 'muted' }, 'That is the whole history.'));
  }

  async function load({ append = false } = {}) {
    try {
      const page = await api.get(`/api/runs${query({ limit: 25, before: append ? oldest : null })}`);
      runs = append ? [...runs, ...page.runs] : page.runs;
      hasMore = page.has_more;
      oldest = page.oldest_id;
      render();
    } catch (error) {
      replace(list, h('p', { class: 'message error' }, error.message));
    }
  }

  return {
    element,
    enter() {
      runs = [];
      oldest = null;
      load();
    },
  };
}
