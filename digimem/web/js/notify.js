// Browser notifications, and the tab title that works without permission.

import { api } from './api.js';
import * as router from './router.js';

const BASE_TITLE = 'DigiMem';
const listeners = new Set();

export function supported() {
  return 'Notification' in window;
}

export function permission() {
  return supported() ? Notification.permission : 'unavailable';
}

export function onPermissionChange(callback) {
  listeners.add(callback);
  return () => listeners.delete(callback);
}

/** Must be called from a click. Browsers refuse a silent request. */
export async function request() {
  if (!supported()) return 'unavailable';
  let result = Notification.permission;
  if (result === 'default') result = await Notification.requestPermission();
  for (const callback of listeners) callback(result);
  return result;
}

function openTarget(target) {
  window.focus();
  // Notification targets are stored as server paths; the interface uses hashes.
  const path = String(target || '/').replace(/^\/+/, '/');
  router.go(path === '/' ? '/' : path);
}

/** Raise whatever the service handed us, then tell it they were shown. */
export async function raise(items) {
  if (!items || !items.length) return;
  if (permission() !== 'granted') return;
  const shown = [];
  for (const item of items) {
    try {
      const notification = new Notification(item.title, {
        body: item.body,
        tag: item.tag,
        renotify: false,
      });
      notification.onclick = () => {
        openTarget(item.target);
        notification.close();
      };
      shown.push(item.id);
    } catch (error) {
      console.warn('The notification could not be shown', error);
    }
  }
  if (shown.length) {
    try {
      await api.post('/api/notifications/delivered', { ids: shown });
    } catch (error) {
      console.warn('The notification acknowledgement failed', error);
    }
  }
}

/** The count in the tab title. The one signal that needs no permission. */
export function setBadge(total) {
  document.title = total > 0 ? `(${total}) ${BASE_TITLE}` : BASE_TITLE;
}
