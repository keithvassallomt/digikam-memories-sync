// The only place that knows how to talk to the local service.

const token = document.querySelector('meta[name="digimem-token"]').content;

async function request(path, options = {}) {
  const response = await fetch(path, {
    ...options,
    headers: {
      'Content-Type': 'application/json',
      'X-DigiMem-Token': token,
      ...(options.headers || {}),
    },
  });
  let body = {};
  try {
    body = await response.json();
  } catch (_) {
    body = {};
  }
  if (!response.ok) {
    const error = new Error(body.error || 'Something went wrong.');
    error.code = body.code;
    error.status = response.status;
    throw error;
  }
  return body;
}

export const api = {
  get: (path) => request(path),
  post: (path, body = {}) => request(path, { method: 'POST', body: JSON.stringify(body) }),

  async blob(path) {
    const response = await fetch(path, { headers: { 'X-DigiMem-Token': token } });
    if (!response.ok) {
      let message = 'The photo could not be loaded.';
      try {
        message = (await response.json()).error || message;
      } catch (_) {
        // Non-JSON failures keep the plain wording.
      }
      throw new Error(message);
    }
    return response.blob();
  },
};

export function query(parameters) {
  const pairs = Object.entries(parameters).filter(
    ([, value]) => value !== null && value !== undefined && value !== '',
  );
  return pairs.length ? `?${new URLSearchParams(pairs)}` : '';
}
