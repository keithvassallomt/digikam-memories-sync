// "Which of these is wrong?" Face boxes digiKam has that cannot sync cleanly.

import { api } from '../api.js';
import { drawCrop, faceCrop, placeBox } from '../crop.js';
import { button, card, h, replace } from '../dom.js';
import { count } from '../format.js';

const HEADING = {
  person_twice: (issue) => `${issue.person} is tagged twice in this photo`,
  nested_box: (issue) => `${issue.person}'s box is inside ${issue.other_person}'s`,
};

const EXPLANATION = {
  person_twice: 'A person has one face in a photograph, so one of these is very '
    + 'likely a mistake. Remove the wrong one, or keep both if the photo really '
    + 'does show them twice.',
  nested_box: 'Faces do not sit inside one another, so one of these boxes is '
    + 'almost certainly drawn in the wrong place.',
};

export function create({ go, refresh }) {
  const element = h('section', { class: 'screen' });
  const body = h('div', {});
  let issues = [];
  let index = 0;
  let photoUrl = null;
  let photoRequest = 0;

  element.append(body);

  function releasePhoto() {
    photoRequest += 1;
    if (photoUrl) URL.revokeObjectURL(photoUrl);
    photoUrl = null;
  }

  async function load() {
    releasePhoto();
    replace(body, h('p', { class: 'muted' }, 'Checking the library…'));
    try {
      issues = (await api.get('/api/library/issues')).issues;
      index = 0;
      if (!issues.length) showFinished();
      else show(0);
    } catch (error) {
      replace(body, h('p', { class: 'message error' }, error.message));
    }
  }

  function showFinished() {
    releasePhoto();
    replace(body, card(
      h('p', { class: 'completion-mark', 'aria-hidden': 'true' }, '✓'),
      h('h3', {}, 'Nothing looks wrong'),
      h('p', { class: 'muted' },
        'Every named face in digiKam sits on its own, where a face should be.'),
      h('div', { class: 'actions' },
        button('Back to Home', { onClick: () => go('/') }))));
  }

  function show(position) {
    index = position;
    const issue = issues[position];
    const loading = h('span', { class: 'photo-message' }, 'Loading photo…');
    const image = h('img', { alt: '', hidden: true });
    const canvas = h('canvas', { class: 'photo-canvas', width: 720, height: 540, hidden: true });

    // One box per candidate, numbered, so the buttons below can name them.
    const marks = issue.boxes.map((box, n) => h(
      'span',
      { class: `face-box ${n === 0 ? 'digikam-box' : 'memories-box'}`, hidden: true },
      h('small', {}, `${n + 1}. ${box.person}`),
    ));
    const frame = h('div', { class: 'photo-frame' }, loading, image, canvas, ...marks);

    const message = h('p', { class: 'message', role: 'status' });
    const choices = issue.boxes.map((box, n) => h('button', {
      type: 'button', class: 'name-choice',
      onClick: () => resolve(issue, { decision: 'remove', person: box.person, rect: box.rect },
        message, buttons),
    }, h('span', { 'aria-hidden': 'true' }, String(n + 1)),
       h('span', {}, h('strong', {}, `Remove box ${n + 1}`),
         h('small', {}, `${box.person} · ${box.pixels.replace(/<\/?rect ?|\/>/g, '').trim()}`))));
    const keep = button('Keep both, this is fine', {
      onClick: () => resolve(issue, { decision: 'dismiss' }, message, buttons),
    });
    const buttons = [...choices, keep];

    replace(body,
      h('div', { class: 'row' },
        h('div', {},
          h('h1', {}, 'Which of these is wrong?'),
          h('p', { class: 'lead' },
            `${count(issues.length)} left · ${HEADING[issue.kind]?.(issue) || issue.kind}`)),
        h('strong', { class: 'position' }, `${position + 1} / ${issues.length}`)),
      h('div', { class: 'review-grid' },
        h('div', {}, frame,
          h('p', { class: 'photo-caption' }, issue.path)),
        h('div', {},
          h('p', { class: 'muted' }, EXPLANATION[issue.kind] || ''),
          ...choices,
          h('div', { class: 'actions' }, keep))),
      message,
      h('p', { class: 'note' },
        'Removing a box changes digiKam, so it has to be closed. '
        + 'digikam4.db is backed up first.'),
      h('div', { class: 'actions' },
        button('Skip for now', {
          onClick: () => show(position + 1 < issues.length ? position + 1 : 0),
        }),
        button('Back', { onClick: () => go('/attention') })));

    loadPhoto(issue, { image, canvas, loading, marks });
  }

  async function loadPhoto(issue, parts) {
    const request = ++photoRequest;
    if (photoUrl) URL.revokeObjectURL(photoUrl);
    photoUrl = null;
    try {
      const blob = await api.blob(`/api/library/issues/${issue.id}/photo`);
      if (request !== photoRequest) return;
      photoUrl = URL.createObjectURL(blob);
      parts.image.onload = () => {
        if (request !== photoRequest) return;
        const crop = faceCrop(
          issue.boxes.map((box) => box.rect),
          parts.image.naturalWidth, parts.image.naturalHeight,
          parts.canvas.width / parts.canvas.height,
        );
        drawCrop(parts.canvas, parts.image, crop);
        parts.loading.hidden = true;
        parts.canvas.hidden = false;
        issue.boxes.forEach((box, n) => placeBox(parts.marks[n], box.rect, crop));
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

  async function resolve(issue, payload, message, buttons) {
    for (const element of buttons) element.disabled = true;
    message.textContent = payload.decision === 'remove' ? 'Removing…' : 'Saving…';
    message.className = 'message';
    try {
      // Removing one box can settle a second issue about the same photo, so
      // the list comes back from the server rather than being stepped along.
      const result = await api.post(`/api/library/issues/${issue.id}`, payload);
      issues = result.issues;
      refresh();
      if (!issues.length) showFinished();
      else show(Math.min(index, issues.length - 1));
    } catch (error) {
      message.textContent = error.message;
      message.className = 'message error';
      for (const element of buttons) element.disabled = false;
    }
  }

  return { element, enter: load, leave: releasePhoto };
}
