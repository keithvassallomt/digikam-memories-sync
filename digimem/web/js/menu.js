// A button that opens a short list of choices. Closes on pick, on Escape and
// on a click anywhere else.

import { h } from './dom.js';

let openPanel = null;

document.addEventListener('click', () => closeAll());
document.addEventListener('keydown', (event) => {
  if (event.key === 'Escape') closeAll();
});

export function closeAll() {
  if (!openPanel) return;
  openPanel.panel.classList.remove('is-open');
  openPanel.trigger.setAttribute('aria-expanded', 'false');
  openPanel = null;
}

/**
 * @param {string} label shown on the button
 * @param {Array<{label: string, description?: string, onChoose: Function, separated?: boolean}>} items
 */
export function menu(label, items, { primary = false } = {}) {
  const panel = h('div', { class: 'menu', role: 'menu' });
  for (const item of items) {
    if (item.separated) panel.appendChild(h('hr'));
    panel.appendChild(
      h('button', {
        type: 'button',
        role: 'menuitem',
        onClick: (event) => {
          event.stopPropagation();
          closeAll();
          item.onChoose();
        },
      },
        h('span', { class: 'menu-label' }, item.label),
        item.description ? h('span', { class: 'menu-description' }, item.description) : null),
    );
  }

  const trigger = h('button', {
    type: 'button',
    class: `button ${primary ? 'button-primary' : ''}`,
    'aria-haspopup': 'menu',
    'aria-expanded': 'false',
    onClick: (event) => {
      event.stopPropagation();
      const wasOpen = panel.classList.contains('is-open');
      closeAll();
      if (wasOpen) return;
      panel.classList.add('is-open');
      trigger.setAttribute('aria-expanded', 'true');
      openPanel = { panel, trigger };
    },
  }, label, h('span', { class: 'menu-caret', 'aria-hidden': 'true' }, '▾'));

  return h('div', {
    class: 'menu-wrap',
    onClick: (event) => event.stopPropagation(),
  }, trigger, panel);
}
