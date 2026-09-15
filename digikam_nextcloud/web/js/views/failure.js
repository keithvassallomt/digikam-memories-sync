// Faces the other library rejected. Adjust the box, or keep the face where it is.

import { api } from '../api.js';
import { dragRect, drawCrop, faceCrop, placeBox } from '../crop.js';
import { button, card, h, replace } from '../dom.js';
import { count } from '../format.js';

const CORNERS = ['top-left', 'top-right', 'bottom-left', 'bottom-right'];

function reason(failure) {
  if (String(failure.error).includes('No face found inside the supplied rectangle')) {
    return 'Recognize’s automatic detector did not accept this face. If the box is correct, add it using the box below.';
  }
  return failure.error;
}

function library(name) {
  return name === 'digikam' ? 'digiKam' : 'Memories';
}

export function create({ go, refresh }) {
  const element = h('section', { class: 'screen' });
  const body = h('div', {});
  let failures = [];
  let index = 0;
  let photoUrl = null;
  let photoRequest = 0;
  let crop = null;
  let rect = null;
  let drag = null;

  element.append(body);

  function releasePhoto() {
    photoRequest += 1;
    if (photoUrl) URL.revokeObjectURL(photoUrl);
    photoUrl = null;
    crop = null;
    drag = null;
  }

  async function load() {
    releasePhoto();
    replace(body, h('p', { class: 'muted' }, 'Loading faces…'));
    try {
      const inbox = await api.get('/api/attention');
      failures = inbox.failures;
      index = 0;
      if (!failures.length) showFinished();
      else show(0);
    } catch (error) {
      replace(body, h('p', { class: 'message error' }, error.message));
    }
  }

  function showFinished() {
    releasePhoto();
    replace(body,
      h('h1', {}, 'Every rejected face is decided'),
      h('p', { class: 'lead' }, 'Faces kept in one library are not offered again unless their name or box changes.'),
      card(
        h('p', { class: 'completion-mark', 'aria-hidden': 'true' }, '✓'),
        h('h3', {}, 'Your decisions are saved')),
      h('div', { class: 'actions' },
        button('Back to Home', { onClick: () => go('/') }),
        button('Needs attention', { class: 'button button-primary', onClick: () => go('/attention') })));
  }

  function show(position) {
    index = position;
    const failure = failures[position];
    rect = failure.rect.map(Number);
    const source = library(failure.source);
    const destination = library(failure.destination);

    const loading = h('span', { class: 'photo-message' }, 'Loading photo…');
    const image = h('img', { alt: '', hidden: true });
    const canvas = h('canvas', {
      width: '960', height: '720', role: 'img',
      'aria-label': 'Face that needs a decision', hidden: true,
    });
    const box = h('div', { class: 'face-box digikam-box editable-face-box', hidden: true },
      h('small', {}, `${source}: ${failure.person || 'Unnamed'}`),
      ...CORNERS.map((corner) => h('i', { class: `handle ${corner}`, dataset: { corner } })));
    const frame = h('div', { class: 'photo-frame editable-photo-frame' }, loading, image, canvas, box);

    box.addEventListener('pointerdown', (event) => {
      if (!crop || !rect) return;
      event.preventDefault();
      drag = {
        x: event.clientX, y: event.clientY, rect: [...rect],
        corner: event.target.dataset.corner || 'move',
      };
      box.setPointerCapture(event.pointerId);
    });
    box.addEventListener('pointermove', (event) => {
      if (!drag || !crop) return;
      const bounds = frame.getBoundingClientRect();
      const dx = ((event.clientX - drag.x) * crop.width) / bounds.width;
      const dy = ((event.clientY - drag.y) * crop.height) / bounds.height;
      rect = dragRect(drag.rect, crop, drag.corner, dx, dy);
      placeBox(box, rect, crop);
      retry.disabled = false;
    });
    box.addEventListener('pointerup', () => { drag = null; });

    const message = h('p', { class: 'message', role: 'status' });
    const keepAll = h('input', { type: 'checkbox' });

    const keep = h('button', {
      type: 'button', class: 'name-choice', hidden: !failure.reviewable,
      onClick: () => resolve(failure, 'keep_source', keepAll.checked, message, [keep, retry]),
    }, h('span', { 'aria-hidden': 'true' }, '▣'),
       h('span', {}, h('strong', {}, `Keep only in ${source}`),
         h('small', {}, 'Remember this and do not offer the face again.')));
    const retry = h('button', {
      type: 'button', class: 'name-choice', disabled: !failure.reviewable,
      onClick: () => resolve(failure, 'retry', false, message, [keep, retry]),
    }, h('span', { 'aria-hidden': 'true' }, '☁'),
       h('span', {}, h('strong', {}, `Add to ${destination} using this box`),
         h('small', {}, 'Use the box shown, or adjust it first.')));

    replace(body,
      h('div', { class: 'row' },
        h('div', {},
          h('h1', {}, 'Face needs a decision'),
          h('p', { class: 'lead' }, `${count(failures.length)} still need a decision.`)),
        h('strong', { class: 'position' }, `${position + 1} / ${failures.length}`)),
      h('div', { class: 'review-grid' },
        h('div', {}, frame,
          h('p', { class: 'photo-caption' }, failure.path),
          h('p', { class: 'photo-caption' }, 'If the box is wrong, drag it or resize it before adding the face.')),
        h('div', {},
          card(
            h('h3', {}, failure.person || 'Unnamed face'),
            h('p', { class: 'muted' }, `Found in ${source} · missing from ${destination}`),
            h('p', { class: 'muted' }, reason(failure))),
          keep, retry,
          failure.reviewable
            ? h('label', { class: 'check' }, keepAll,
                h('span', {},
                  h('strong', {}, 'Keep all remaining rejected faces where they are'),
                  h('small', {}, 'They will be ignored in future syncs.')))
            : null)),
      message,
      h('div', { class: 'actions' },
        button('Back', { onClick: () => go('/attention') })));

    loadPhoto(failure, { image, canvas, loading, box });
  }

  async function loadPhoto(failure, parts) {
    const request = ++photoRequest;
    if (photoUrl) URL.revokeObjectURL(photoUrl);
    photoUrl = null;
    try {
      const blob = await api.blob(`/api/runs/${failure.run_id}/failures/${failure.id}/photo`);
      if (request !== photoRequest) return;
      photoUrl = URL.createObjectURL(blob);
      parts.image.onload = () => {
        if (request !== photoRequest) return;
        crop = faceCrop(
          [rect], parts.image.naturalWidth, parts.image.naturalHeight,
          parts.canvas.width / parts.canvas.height,
        );
        drawCrop(parts.canvas, parts.image, crop);
        parts.loading.hidden = true;
        parts.canvas.hidden = false;
        placeBox(parts.box, rect, crop);
      };
      parts.image.onerror = () => {
        if (request !== photoRequest) return;
        parts.loading.textContent = 'This photo could not be previewed.';
      };
      parts.image.src = photoUrl;
    } catch (error) {
      if (request !== photoRequest) return;
      parts.loading.textContent = error.message;
    }
  }

  async function resolve(failure, decision, applyToRemaining, message, buttons) {
    for (const element of buttons) element.disabled = true;
    message.textContent = 'Saving choice…';
    message.className = 'message';
    try {
      await api.post(`/api/runs/${failure.run_id}/failures/${failure.id}`, {
        decision, rect, apply_to_remaining: decision === 'keep_source' && applyToRemaining,
      });
      const inbox = await api.get('/api/attention');
      failures = inbox.failures;
      refresh();
      if (!failures.length) showFinished();
      else show(Math.min(index, failures.length - 1));
    } catch (error) {
      message.textContent = error.message;
      message.className = 'message error';
      for (const element of buttons) element.disabled = false;
    }
  }

  return { element, enter: load, leave: releasePhoto };
}
