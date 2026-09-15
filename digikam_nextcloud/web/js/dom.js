// Minimal element building. No framework, no template strings in markup, so
// nothing user-supplied can be parsed as HTML.

export function h(tag, props = {}, ...children) {
  const element = document.createElement(tag);
  for (const [key, value] of Object.entries(props || {})) {
    if (value === null || value === undefined || value === false) continue;
    if (key === 'class') element.className = value;
    else if (key === 'dataset') Object.assign(element.dataset, value);
    else if (key === 'style') Object.assign(element.style, value);
    else if (key.startsWith('on') && typeof value === 'function') {
      element.addEventListener(key.slice(2).toLowerCase(), value);
    } else if (key === 'hidden' || key === 'disabled' || key === 'checked') {
      element[key] = Boolean(value);
    } else if (value === true) element.setAttribute(key, '');
    else element.setAttribute(key, String(value));
  }
  append(element, children);
  return element;
}

export function append(parent, children) {
  for (const child of children.flat(Infinity)) {
    if (child === null || child === undefined || child === false) continue;
    parent.appendChild(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return parent;
}

export function clear(element) {
  while (element.firstChild) element.removeChild(element.firstChild);
  return element;
}

export function replace(element, ...children) {
  return append(clear(element), children);
}

/** A labelled statistic tile. */
export function stat(number, label) {
  return h('div', { class: 'stat' },
    h('div', { class: 'stat-number' }, number),
    h('div', { class: 'stat-label' }, label));
}

/** A row of `term — value`, used by the result and settings lists. */
export function row(left, right, className = 'list-row') {
  return h('li', { class: className }, h('span', {}, left), h('strong', {}, right));
}

export function card(...children) {
  return h('section', { class: 'card' }, ...children);
}

export function button(label, props = {}) {
  return h('button', { type: 'button', class: 'button', ...props }, label);
}
