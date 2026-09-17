// One poll of /api/status feeds every screen. Views subscribe rather than
// fetching the same thing over and over.

import { api, query } from './api.js';

const VISIBLE_INTERVAL = 2000;
const HIDDEN_INTERVAL = 30000;

const subscribers = new Set();
let latest = null;
let timer = null;
let failures = 0;
let lastError = null;

export function subscribe(callback) {
  subscribers.add(callback);
  if (latest) callback(latest, lastError);
  return () => subscribers.delete(callback);
}

export function status() {
  return latest;
}

function isWatched() {
  // "Watched" means the user can actually see it, which is what decides
  // whether a notification is raised at all.
  return document.visibilityState === 'visible' && document.hasFocus();
}

function permission() {
  return 'Notification' in window ? Notification.permission : 'unavailable';
}

export async function refresh() {
  try {
    latest = await api.get(
      `/api/status${query({ visible: isWatched(), permission: permission() })}`,
    );
    failures = 0;
    lastError = null;
  } catch (error) {
    failures += 1;
    // One blip while the service restarts should not blank the screen.
    if (failures < 3) return latest;
    lastError = error;
  }
  for (const callback of subscribers) {
    try {
      callback(latest, lastError);
    } catch (problem) {
      console.error('A screen failed to handle a status update', problem);
    }
  }
  return latest;
}

function schedule() {
  clearTimeout(timer);
  timer = setTimeout(async () => {
    await refresh();
    schedule();
  }, document.visibilityState === 'visible' ? VISIBLE_INTERVAL : HIDDEN_INTERVAL);
}

export function start() {
  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'visible') refresh();
    schedule();
  });
  window.addEventListener('focus', refresh);
  window.addEventListener('blur', refresh);
  refresh().then(schedule);
}
