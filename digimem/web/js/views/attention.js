// One inbox for everything waiting on a person, across every run.

import { api } from '../api.js';
import { button, card, h, replace } from '../dom.js';
import { plural, when } from '../format.js';
import * as notify from '../notify.js';

export function create({ go, refresh }) {
  const element = h('section', { class: 'screen' });
  const body = h('div', {});

  element.append(
    h('h1', {}, 'Needs attention'),
    h('p', { class: 'lead' }, 'DigiMem keeps syncing everything else. These faces need you.'),
    body,
  );

  function section(title, description, total, noun, target) {
    return card(h('div', { class: 'row' },
      h('div', {}, h('h3', {}, title), h('div', { class: 'muted' }, description)),
      h('div', { class: 'row-tight' },
        h('span', { class: 'count' }, plural(total, noun, `${noun}s`)),
        button('Review', { class: 'button button-primary', onClick: () => go(target) }))));
  }

  async function load() {
    try {
      const inbox = await api.get('/api/attention');
      if (!inbox.total) {
        replace(body, card(
          h('p', { class: 'completion-mark', 'aria-hidden': 'true' }, '✓'),
          h('h3', {}, 'Nothing is waiting'),
          h('p', { class: 'muted' }, 'Every face DigiMem found agrees in both libraries.')));
        return;
      }
      const newest = inbox.conflicts[0];
      const parts = [];
      if (inbox.conflict_count) {
        parts.push(section(
          'Different names',
          `The same face has a different name in digiKam and Memories.${
            newest ? ` Newest from ${when(newest.run_started_at)}.` : ''}`,
          inbox.conflict_count, 'face', '/attention/conflicts'));
      }
      if (inbox.failure_count) {
        parts.push(section(
          "Couldn't be added",
          'Memories rejected these faces. Adjust the box, or keep them in digiKam only.',
          inbox.failure_count, 'face', '/attention/failures'));
      }
      if (notify.supported() && notify.permission() === 'default') {
        parts.push(card(h('div', { class: 'row' },
          h('div', {},
            h('h3', {}, 'Get told when this happens'),
            h('div', { class: 'muted' },
              'DigiMem can notify you while this window is behind another one.')),
          button('Turn on notifications', {
            onClick: async () => { await notify.request(); await load(); await refresh(); },
          }))));
      }
      parts.push(h('p', { class: 'note' },
        'Faces you keep in one library are remembered. They are not offered again unless their name or box changes.'));
      replace(body, ...parts);
    } catch (error) {
      replace(body, h('p', { class: 'message error' }, error.message));
    }
  }

  return { element, enter: load };
}
