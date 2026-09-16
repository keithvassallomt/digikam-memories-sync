// "Who is this?" One face at a time, across every run that still has one.

import { api } from '../api.js';
import { drawCrop, faceCrop, placeBox } from '../crop.js';
import { button, card, h, replace } from '../dom.js';
import { count } from '../format.js';

export function create({ go, refresh }) {
  const element = h('section', { class: 'screen' });
  const body = h('div', {});
  let conflicts = [];
  let index = 0;
  let photoUrl = null;
  let photoRequest = 0;

  element.append(body);

  function releasePhoto() {
    photoRequest += 1;
    if (photoUrl) URL.revokeObjectURL(photoUrl);
    photoUrl = null;
  }

  function name(value) {
    return value || 'Unnamed';
  }

  async function load() {
    releasePhoto();
    replace(body, h('p', { class: 'muted' }, 'Loading faces…'));
    try {
      const inbox = await api.get('/api/attention');
      conflicts = inbox.conflicts;
      index = 0;
      if (!conflicts.length) showFinished();
      else show(0);
    } catch (error) {
      replace(body, h('p', { class: 'message error' }, error.message));
    }
  }

  async function showFinished() {
    releasePhoto();
    // A decision made after its own run finished needs a run to carry it.
    // One whose run can still carry it is left where it is.
    let collected = { created: false, decisions: 0, carried_by: [] };
    try {
      collected = await api.post('/api/decisions/apply');
    } catch (error) {
      console.warn('The decisions could not be collected', error);
    }
    refresh();

    const carrier = (collected.carried_by || [])[0];
    let explanation;
    let onward;
    if (collected.created) {
      explanation = `${count(collected.decisions)} of them are ready to apply on their own.`;
      onward = button('Review them', {
        class: 'button button-primary', onClick: () => go(`/runs/${collected.run_id}`),
      });
    } else if (carrier !== undefined) {
      explanation = 'They will be applied along with the rest of that sync, '
        + 'which is still waiting for you.';
      onward = button('Review that sync', {
        class: 'button button-primary', onClick: () => go(`/runs/${carrier}`),
      });
    } else {
      explanation = 'They are applied with the rest of their sync.';
      onward = button('Needs attention', {
        class: 'button button-primary', onClick: () => go('/attention'),
      });
    }

    replace(body,
      h('h1', {}, 'Every name is decided'),
      h('p', { class: 'lead' }, 'Nothing has been changed yet.'),
      card(
        h('p', { class: 'completion-mark', 'aria-hidden': 'true' }, '✓'),
        h('h3', {}, 'Your decisions are saved'),
        h('p', { class: 'muted' }, explanation)),
      h('div', { class: 'actions' },
        button('Back to Home', { onClick: () => go('/') }),
        onward));
  }

  function show(position) {
    index = position;
    const conflict = conflicts[position];

    const loading = h('span', { class: 'photo-message' }, 'Loading photo…');
    const image = h('img', { alt: '', hidden: true });
    const canvas = h('canvas', {
      width: '960', height: '720', role: 'img',
      'aria-label': `Zoomed face: digiKam says ${name(conflict.digikam_person)}, Memories says ${name(conflict.nextcloud_person)}`,
      hidden: true,
    });
    const digikamBox = h('span', { class: 'face-box digikam-box', hidden: true },
      h('small', {}, `digiKam: ${name(conflict.digikam_person)}`));
    const memoriesBox = h('span', { class: 'face-box memories-box', hidden: true },
      h('small', {}, `Memories: ${name(conflict.nextcloud_person)}`));
    const frame = h('div', { class: 'photo-frame' }, loading, image, canvas, digikamBox, memoriesBox);

    const message = h('p', { class: 'message', role: 'status' });
    const applyAll = h('input', { type: 'checkbox' });
    const always = h('input', {
      type: 'checkbox',
      // Always implies the rest of this sync, so the two cannot contradict.
      onChange: (event) => {
        if (!event.currentTarget.checked) return;
        applyAll.checked = true;
      },
    });

    const choose = (resolution) =>
      resolve(conflict, resolution, applyAll.checked || always.checked,
        always.checked, message, [keep, take]);
    const keep = h('button', {
      type: 'button', class: 'name-choice', 'aria-pressed': 'false', onClick: () => choose('digikam'),
    }, h('span', { 'aria-hidden': 'true' }, '▣'),
       h('span', {}, h('strong', {}, name(conflict.digikam_person)), h('small', {}, 'Keep the name from digiKam')));
    const take = h('button', {
      type: 'button', class: 'name-choice', 'aria-pressed': 'false', onClick: () => choose('memories'),
    }, h('span', { 'aria-hidden': 'true' }, '☁'),
       h('span', {}, h('strong', {}, name(conflict.nextcloud_person)), h('small', {}, 'Keep the name from Memories')));

    replace(body,
      h('div', { class: 'row' },
        h('div', {},
          h('h1', {}, 'Who is this?'),
          h('p', { class: 'lead' },
            `${count(conflicts.length)} left · choose the correct name for the highlighted face.`)),
        h('strong', { class: 'position' }, `${position + 1} / ${conflicts.length}`)),
      h('div', { class: 'review-grid' },
        h('div', {}, frame,
          h('p', { class: 'photo-caption' }, conflict.path),
          h('p', { class: 'photo-caption' },
            `The two face boxes overlap ${Math.round(100 * (conflict.iou || 0))}%.`)),
        h('div', {}, keep, take,
          h('label', { class: 'check' }, applyAll,
            h('span', {},
              h('strong', {}, 'Use this choice for the rest of this sync'),
              h('small', {}, 'Every unresolved conflict in the same run gets the same choice.'))),
          h('label', { class: 'check' }, always,
            h('span', {},
              h('strong', {}, 'Always use this library from now on'),
              h('small', {}, 'Later syncs stop asking and rename the other library to match. '
                + 'Change it again under Settings.'))))),
      message,
      h('div', { class: 'actions' },
        button('Back', { onClick: () => go('/attention') })));

    loadPhoto(conflict, { image, canvas, loading, digikamBox, memoriesBox });
  }

  async function loadPhoto(conflict, parts) {
    const request = ++photoRequest;
    if (photoUrl) URL.revokeObjectURL(photoUrl);
    photoUrl = null;
    try {
      const blob = await api.blob(`/api/runs/${conflict.run_id}/conflicts/${conflict.id}/photo`);
      if (request !== photoRequest) return;
      photoUrl = URL.createObjectURL(blob);
      parts.image.onload = () => {
        if (request !== photoRequest) return;
        const crop = faceCrop(
          [conflict.digikam_rect, conflict.nextcloud_rect],
          parts.image.naturalWidth, parts.image.naturalHeight,
          parts.canvas.width / parts.canvas.height,
        );
        drawCrop(parts.canvas, parts.image, crop);
        parts.loading.hidden = true;
        parts.canvas.hidden = false;
        placeBox(parts.digikamBox, conflict.digikam_rect, crop);
        placeBox(parts.memoriesBox, conflict.nextcloud_rect, crop);
      };
      parts.image.onerror = () => {
        if (request !== photoRequest) return;
        parts.loading.textContent = 'This photo format cannot be previewed here.';
      };
      parts.image.src = photoUrl;
    } catch (error) {
      if (request !== photoRequest) return;
      parts.loading.textContent = error.message;
    }
  }

  async function resolve(conflict, resolution, applyToRemaining, always, message, buttons) {
    for (const element of buttons) element.disabled = true;
    buttons[resolution === 'digikam' ? 0 : 1].setAttribute('aria-pressed', 'true');
    message.textContent = 'Saving choice…';
    message.className = 'message';
    try {
      if (always) {
        await api.post('/api/settings/sync', { conflict_policy: resolution });
      }
      await api.post(`/api/runs/${conflict.run_id}/conflicts/${conflict.id}`, {
        resolution, apply_to_remaining: applyToRemaining,
      });
      // The inbox spans runs, and "apply to remaining" settles a whole run, so
      // the authoritative next item comes from reloading rather than stepping.
      const inbox = await api.get('/api/attention');
      conflicts = inbox.conflicts;
      refresh();
      if (!conflicts.length) showFinished();
      else show(Math.min(index, conflicts.length - 1));
    } catch (error) {
      message.textContent = error.message;
      message.className = 'message error';
      for (const element of buttons) element.disabled = false;
    }
  }

  return { element, enter: load, leave: releasePhoto };
}
