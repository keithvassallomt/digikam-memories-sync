// First run, and the same form whenever a library needs changing.

import { api } from '../api.js';
import { button, card, h, replace } from '../dom.js';

export function create({ go, refresh }) {
  const element = h('section', { class: 'screen' });
  const body = h('div', {});
  element.append(body);

  function field(label, input, { hint = null, wide = false } = {}) {
    return h('label', { class: `field ${wide ? 'wide' : ''}` }, label, input,
      hint ? h('small', {}, hint) : null);
  }

  function showForm(settings) {
    const library = h('input', {
      class: 'input', value: settings.digikam_library || '',
      placeholder: '/home/you/Photos', autocomplete: 'off',
    });
    const url = h('input', {
      class: 'input', type: 'url', value: settings.nextcloud_url || '',
      placeholder: 'https://cloud.example.com',
    });
    const user = h('input', {
      class: 'input', value: settings.nc_user || '', autocomplete: 'username',
    });
    const password = h('input', {
      class: 'input', type: 'password', autocomplete: 'current-password',
    });
    const photos = h('input', { class: 'input', value: settings.nc_photos_path || 'Photos' });
    const found = h('div', { class: 'inline-result', role: 'status' });
    const message = h('p', { class: 'message', role: 'status' });
    const install = h('div', {});

    const payload = () => ({
      digikam_library: library.value.trim(),
      nextcloud_url: url.value.trim(),
      nc_user: user.value.trim(),
      password: password.value,
      nc_photos_path: photos.value.trim(),
    });

    function showRequirements(result) {
      replace(install);
      if (result.ready) return;
      if (!result.recognize_installed) {
        install.appendChild(card(
          h('h3', {}, 'Recognize is not installed in Nextcloud'),
          h('p', { class: 'muted' },
            'Face Sync needs the Recognize app enabled for your account before it can read faces.')));
        return;
      }
      install.appendChild(card(
        h('h3', {}, 'The Face Sync app in Nextcloud needs installing or updating'),
        h('p', { class: 'muted' },
          'Recognize is working, but the companion app is missing a feature Face Sync needs.'),
        h('div', { class: 'row-tight' },
          h('a', {
            class: 'button', href: result.install_url, target: '_blank', rel: 'noopener',
          }, 'Open the installation page'),
          button('Check again', { onClick: test }))));
    }

    async function test() {
      message.textContent = 'Checking…';
      message.className = 'message';
      try {
        const result = await api.post('/api/connection/test', payload());
        showRequirements(result);
        message.textContent = result.ready
          ? 'Both libraries are reachable.'
          : 'Nextcloud answered, but something still needs installing.';
        message.className = `message ${result.ready ? 'success' : ''}`;
      } catch (error) {
        replace(install);
        message.textContent = error.message;
        message.className = 'message error';
      }
    }

    async function save() {
      message.textContent = 'Saving…';
      message.className = 'message';
      try {
        const result = await api.post('/api/settings', payload());
        if (!result.ready) {
          showRequirements(result);
          message.textContent = 'Nextcloud answered, but something still needs installing.';
          message.className = 'message';
          return;
        }
        await refresh();
        showFinish(result.settings || settings);
      } catch (error) {
        message.textContent = error.message;
        message.className = 'message error';
      }
    }

    replace(body,
      h('h1', {}, 'Connect your photo libraries'),
      h('p', { class: 'lead' }, 'You only need to do this once. Face Sync saves these settings on this computer.'),
      card(
        h('div', { class: 'row-start' },
          h('span', { class: 'iconbox', 'aria-hidden': 'true' }, '▣'),
          h('div', {}, h('h3', {}, 'digiKam library'),
            h('div', { class: 'muted' }, 'The folder holding digikam4.db.'))),
        h('div', { class: 'field-action' }, library,
          button('Find it for me', {
            onClick: async () => {
              try {
                const { databases } = await api.get('/api/digikam/discover');
                if (!databases.length) {
                  found.textContent = 'No digiKam database was found automatically.';
                  return;
                }
                library.value = databases[0].replace(/\/digikam4\.db$/, '');
                found.textContent = `Found ${databases[0]}`;
              } catch (error) {
                found.textContent = error.message;
              }
            },
          })),
        found),
      card(
        h('div', { class: 'row-start' },
          h('span', { class: 'iconbox', 'aria-hidden': 'true' }, '☁'),
          h('div', {}, h('h3', {}, 'Nextcloud'),
            h('div', { class: 'muted' }, 'Use an app password. Your normal password is not needed.'))),
        h('div', { class: 'fields' },
          field('Nextcloud address', url, { wide: true }),
          field('Username', user),
          field('App password', password, {
            hint: settings.has_password ? 'Leave blank to keep the saved password.' : null,
          }),
          field('Photos folder in Nextcloud', photos, { wide: true }))),
      install, message,
      h('div', { class: 'actions' },
        button('Test connection', { onClick: test }),
        button('Save and continue', { class: 'button button-primary', onClick: save })));
  }

  function showFinish(settings) {
    const automatic = h('input', { type: 'checkbox', checked: true });
    const atLogin = h('input', { type: 'checkbox', checked: true });
    const message = h('p', { class: 'message', role: 'status' });

    replace(body,
      h('h1', {}, "You're connected"),
      h('p', { class: 'lead' }, 'Last step. Face Sync can keep both libraries in step on its own.'),
      card(
        h('div', { class: 'lib-row' },
          h('span', { 'aria-hidden': 'true' }, '▣'), h('span', {}, 'digiKam'),
          h('span', { class: 'lib-detail' }, settings.digikam_library || ''),
          h('span', { class: 'pill is-good' }, 'found')),
        h('div', { class: 'lib-row' },
          h('span', { 'aria-hidden': 'true' }, '☁'), h('span', {}, 'Nextcloud'),
          h('span', { class: 'lib-detail' },
            `${settings.nextcloud_url || ''} · ${settings.nc_user || ''}`),
          h('span', { class: 'pill is-good' }, 'connected'))),
      card(
        h('label', { class: 'check' }, automatic,
          h('span', {},
            h('strong', {}, 'Keep both libraries in sync automatically'),
            h('small', {},
              'Face Sync checks for changes in the background, waits until digiKam is closed before changing it, and only asks you when two names disagree.'))),
        h('label', { class: 'check' }, atLogin,
          h('span', {},
            h('strong', {}, 'Start Face Sync when I log in'),
            h('small', {}, 'Without this, syncing only happens while Face Sync is running.')))),
      message,
      h('div', { class: 'actions' },
        button('Finish', {
          class: 'button button-primary',
          onClick: async () => {
            message.textContent = 'Saving…';
            message.className = 'message';
            try {
              await api.post('/api/settings/automation', { enabled: automatic.checked });
              if (atLogin.checked) {
                await api.post('/api/service/autostart', { enabled: true });
              }
              await refresh();
              go('/');
            } catch (error) {
              message.textContent = error.message;
              message.className = 'message error';
            }
          },
        })));
  }

  return {
    element,
    async enter() {
      replace(body, h('p', { class: 'muted' }, 'Loading settings…'));
      try {
        showForm(await api.get('/api/settings'));
      } catch (error) {
        replace(body, h('p', { class: 'message error' }, error.message));
      }
    },
  };
}
