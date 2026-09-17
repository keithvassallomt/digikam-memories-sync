// Everything that is set once and then verified before each sync.

import { api } from '../api.js';
import { button, card, h, replace } from '../dom.js';
import { when } from '../format.js';
import * as notify from '../notify.js';

export function create({ go, refresh }) {
  const element = h('section', { class: 'screen' });
  const body = h('div', {});
  const message = h('p', { class: 'message', role: 'status' });
  element.append(h('h1', {}, 'Settings'),
    h('p', { class: 'lead' }, 'Set these once. DigiMem checks them before every sync.'),
    message, body);

  function say(text, kind = 'success') {
    message.textContent = text;
    message.className = `message ${kind}`;
  }

  async function send(path, payload, description) {
    try {
      await api.post(path, payload);
      say(`${description} saved.`);
      await refresh();
      return true;
    } catch (error) {
      say(error.message, 'error');
      return false;
    }
  }

  function settingRow(title, description, control) {
    return h('div', { class: 'setting-row' },
      h('div', {}, h('strong', {}, title),
        description ? h('div', { class: 'muted' }, description) : null),
      control);
  }

  function toggle(checked, onChange) {
    const input = h('input', {
      type: 'checkbox', checked,
      onChange: (event) => onChange(event.currentTarget.checked),
    });
    return h('label', { class: 'switch-label' }, input);
  }

  function numberField(value, unit, onCommit) {
    const input = h('input', {
      class: 'input input-number', type: 'number', min: '1', value: String(value),
      onChange: (event) => onCommit(Number(event.currentTarget.value)),
    });
    return h('div', { class: 'row-tight' }, input, unit ? h('span', { class: 'muted' }, unit) : null);
  }

  function wordChoice(options, value, onChange) {
    return h('select', {
      class: 'select',
      onChange: (event) => onChange(event.currentTarget.value),
    }, ...options.map(([word, label]) =>
      h('option', { value: word, selected: value === word }, label)));
  }

  function choice(options, value, onChange) {
    return h('select', {
      class: 'select',
      onChange: (event) => onChange(Number(event.currentTarget.value)),
    }, ...options.map(([amount, label]) =>
      h('option', { value: String(amount), selected: Number(value) === amount }, label)));
  }

  function librariesCard(settings) {
    return card(
      h('h3', {}, 'Libraries'),
      settingRow('digiKam folder', settings.digikam_library || 'Not set',
        button('Change', { class: 'button button-small', onClick: () => go('/setup') })),
      settingRow('Nextcloud',
        settings.nextcloud_url ? `${settings.nextcloud_url} · ${settings.nc_user}` : 'Not set',
        button('Change', { class: 'button button-small', onClick: () => go('/setup') })),
      settingRow('Photos folder in Nextcloud', settings.nc_photos_path || 'Photos',
        button('Change', { class: 'button button-small', onClick: () => go('/setup') })));
  }

  function syncCard(settings) {
    const sync = settings.sync || {};
    return card(
      h('h3', {}, 'What a sync does'),
      settingRow('When the two libraries disagree',
        'DigiMem settles what it can from what both libraries last agreed on. '
        + 'This is for the rest. Trusting one renames faces in the other without asking.',
        wordChoice(
          [['ask', 'Ask me'], ['digikam', 'Trust digiKam'], ['memories', 'Trust Memories']],
          sync.conflict_policy || 'ask',
          (conflict_policy) => send('/api/settings/sync', { conflict_policy }, 'Disagreements'))),
      settingRow('Create face boxes in Memories',
        'Off means names only: faces Memories already found get named, and none are added.',
        toggle(sync.create_in_memories !== false, (create_in_memories) =>
          send('/api/settings/sync', { create_in_memories }, 'Memories face boxes'))),
      settingRow('Create face boxes in digiKam',
        'Off means digiKam keeps the faces it already has.',
        toggle(sync.create_in_digikam !== false, (create_in_digikam) =>
          send('/api/settings/sync', { create_in_digikam }, 'digiKam face boxes'))));
  }

  function automationCard(settings) {
    const automation = settings.automation || {};
    return card(
      h('div', { class: 'row' },
        h('h3', {}, 'Automatic sync'),
        toggle(automation.enabled, (enabled) =>
          send('/api/settings/automation', { enabled }, 'Automatic sync'))),
      settingRow('Apply changes without asking',
        'Faces with two different names always wait for you.',
        toggle(automation.apply_automatically, (apply_automatically) =>
          send('/api/settings/automation', { apply_automatically }, 'Automatic apply'))),
      settingRow('Wait after the last change',
        'Stops a burst of edits becoming many small syncs.',
        numberField(automation.quiet_period_minutes, 'minutes', (quiet_period_minutes) =>
          send('/api/settings/automation', { quiet_period_minutes }, 'Quiet period'))),
      settingRow('Check for changes every',
        'How often DigiMem asks Nextcloud whether anything moved.',
        choice([[5, '5 minutes'], [15, '15 minutes'], [60, '1 hour']],
          automation.check_interval_minutes,
          (check_interval_minutes) =>
            send('/api/settings/automation', { check_interval_minutes }, 'Check interval'))),
      settingRow('Sync at least every',
        'A safety net for changes that slip past detection.',
        choice([[6, '6 hours'], [24, 'day'], [168, 'week']],
          automation.fallback_interval_hours,
          (fallback_interval_hours) =>
            send('/api/settings/automation', { fallback_interval_hours }, 'Fallback interval'))));
  }

  function notificationsCard(settings) {
    const wanted = settings.notifications || {};
    const state = notify.permission();
    const description = {
      granted: 'On. Used while DigiMem is open but behind another window.',
      denied: 'Refused by the browser. DigiMem will use desktop notifications instead.',
      default: 'Not asked yet. Used while DigiMem is open but behind another window.',
      unavailable: 'This browser cannot show notifications.',
    }[state];
    return card(
      h('h3', {}, 'Notifications'),
      settingRow('Faces need a decision', null,
        toggle(wanted.decisions, (decisions) =>
          send('/api/settings/notifications', { decisions }, 'Notification setting'))),
      settingRow('Connection problems', null,
        toggle(wanted.connection, (connection) =>
          send('/api/settings/notifications', { connection }, 'Notification setting'))),
      settingRow('Every completed sync', 'Off by default. Most syncs change nothing.',
        toggle(wanted.completed, (completed) =>
          send('/api/settings/notifications', { completed }, 'Notification setting'))),
      settingRow('Notify me in this browser', description,
        state === 'default'
          ? button('Enable', {
              class: 'button button-small',
              onClick: async () => { await notify.request(); await load(); },
            })
          : h('span', { class: `pill ${state === 'granted' ? 'is-good' : 'is-off'}` }, state)));
  }

  function serviceCard(service) {
    const login = service.autostart || {};
    const shortcut = service.shortcut || {};
    return card(
      h('h3', {}, 'Background service'),
      settingRow('Start DigiMem when I log in',
        service.started_at ? `Running since ${when(service.started_at)}` : 'Running',
        toggle(login.enabled, async (enabled) => {
          if (await send('/api/service/autostart', { enabled }, 'Start at login')) load();
        })),
      settingRow('Add to application menu',
        shortcut.installed ? shortcut.path : 'Creates a shortcut that opens this window.',
        shortcut.installed
          ? h('span', { class: 'pill is-good' }, 'installed')
          : button('Add', {
              class: 'button button-small',
              onClick: async () => {
                if (await send('/api/shortcuts/install', {}, 'Shortcut')) load();
              },
            })));
  }

  function storageCard(settings) {
    const retention = settings.retention || {};
    return card(
      h('h3', {}, 'Storage'),
      settingRow('Backups to keep', settings.config_dir ? `${settings.config_dir}/backups` : null,
        numberField(retention.backups_keep, null, (backups_keep) =>
          send('/api/settings/retention', { backups_keep }, 'Backup retention'))),
      settingRow('Keep logs for', settings.config_dir ? `${settings.config_dir}/logs` : null,
        numberField(retention.log_days, 'days', (log_days) =>
          send('/api/settings/retention', { log_days }, 'Log retention'))));
  }

  function advancedCard(ledger) {
    return card(
      h('h3', {}, 'Advanced'),
      settingRow('Rebuild the face ledger',
        `DigiMem remembers ${ledger.remembered.toLocaleString()} agreed names. `
        + 'Rebuilding forgets them and learns again from a full run, which means '
        + 'one round of decisions for anything that disagrees.',
        button('Rebuild', {
          class: 'button button-small',
          onClick: async () => {
            if (!window.confirm('Forget every remembered name and run a full check?')) return;
            if (await send('/api/ledger/rebuild', {}, 'Ledger rebuild')) go('/');
          },
        })));
  }

  async function load() {
    replace(body, h('p', { class: 'muted' }, 'Loading settings…'));
    try {
      const [settings, service, ledger] = await Promise.all([
        api.get('/api/settings'),
        api.get('/api/service'),
        api.get('/api/ledger'),
      ]);
      replace(body,
        librariesCard(settings),
        syncCard(settings),
        automationCard(settings),
        notificationsCard(settings),
        serviceCard(service),
        storageCard(settings),
        advancedCard(ledger));
    } catch (error) {
      replace(body, h('p', { class: 'message error' }, error.message));
    }
  }

  return { element, enter: load };
}
