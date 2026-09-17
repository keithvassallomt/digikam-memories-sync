// Hash routing. Every screen has an address so a notification can open it and
// the back button behaves.

const routes = [];
let onChange = () => {};
let current = null;

export function define(pattern, name) {
  // "/runs/:id" becomes a matcher that yields { id }.
  const names = [];
  const expression = new RegExp(
    `^${pattern.replace(/:[^/]+/g, (part) => {
      names.push(part.slice(1));
      return '([^/]+)';
    })}$`,
  );
  routes.push({ expression, names, name });
}

export function parse(hash) {
  const path = `/${String(hash || '').replace(/^#\/?/, '').split('?')[0]}`.replace(/\/+$/, '') || '/';
  for (const route of routes) {
    const found = path.match(route.expression);
    if (found) {
      const parameters = {};
      route.names.forEach((key, index) => {
        parameters[key] = found[index + 1];
      });
      return { name: route.name, parameters, path };
    }
  }
  return null;
}

export function go(path, { replace = false } = {}) {
  const target = `#${path.startsWith('/') ? path : `/${path}`}`;
  if (window.location.hash === target) {
    handle();
    return;
  }
  if (replace) window.history.replaceState(null, '', target);
  else window.location.hash = target;
  if (replace) handle();
}

export function currentRoute() {
  return current;
}

function handle() {
  current = parse(window.location.hash) || parse('#/');
  onChange(current);
}

export function start(callback) {
  onChange = callback;
  window.addEventListener('hashchange', handle);
  handle();
}
