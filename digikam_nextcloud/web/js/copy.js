// Every sentence the interface says, in one file, so the wording can be read
// and changed without hunting through screens.

import { ago, count, plural, timeOfDay, when } from './format.js';

export const TRIGGERS = {
  manual: 'you',
  decisions: 'your decisions',
  digikam_closed: 'digiKam closed',
  digikam_changed: 'digiKam changed',
  memories_changed: 'Memories changed',
  interval: 'daily check',
  decisions: 'your decisions',
  resume: 'resumed',
};

export function trigger(name) {
  return TRIGGERS[name] || name || 'you';
}

export const PHASES = {
  starting: 'Getting ready',
  waiting: 'Waiting to continue',
  deferred: 'Waiting for digiKam to close',
  loading_memories: 'Reading faces from Memories',
  scanning_digikam: 'Checking digiKam photos',
  scanning_memories: 'Checking Memories photos',
  backing_up_digikam: 'Backing up digiKam',
  starting_apply: 'Getting ready to apply',
  applying: 'Applying changes',
  completed: 'Finished',
  failed: 'Stopped',
  apply_failed: 'Some changes need attention',
};

export function phase(name) {
  return PHASES[name] || 'Working';
}

/** How many faces a finished run actually changed. */
export function changeCount(summary = {}) {
  return (
    Number(summary.assigned || 0) +
    Number(summary.inserted || 0) +
    Number(summary.created_in_digikam || 0) +
    Number(summary.reassigned_in_digikam || 0)
  );
}

/** Changes that write to digiKam, which is what the close-digiKam rule needs. */
export function digikamChangeCount(summary = {}) {
  return (
    Number(summary.created_in_digikam || 0) +
    Number(summary.reassigned_in_digikam || 0)
  );
}

export function outcome(run) {
  const summary = run.summary || {};
  const applied = Number(summary.applied || 0);
  switch (run.status) {
    case 'queued':
      return 'Waiting to start';
    case 'waiting':
      return run.waiting_reason === 'connection'
        ? "Couldn't reach Nextcloud, will retry"
        : 'Waiting to continue';
    case 'deferred':
      return `${plural((run.apply || {}).pending || 0, 'change is', 'changes are')} waiting for digiKam`;
    case 'previewing':
      return 'Checking both libraries';
    case 'applying':
      return 'Applying changes';
    case 'previewed': {
      // What is still open, not what the run found. Those differ the moment
      // you answer the first one.
      const open = run.conflicts_open ?? summary.conflicts ?? 0;
      if (open) return `${plural(open, 'face needs', 'faces need')} a decision`;
      const changes = changeCount(summary) + (run.conflicts_resolved || 0);
      return changes ? `${plural(changes, 'change', 'changes')} ready to apply` : 'No changes';
    }
    case 'applied':
      return `Applied ${plural(applied || changeCount(summary), 'change', 'changes')}`;
    case 'applied_with_issues':
    case 'apply_failed':
      return `${plural(summary.failed || 0, 'change needs', 'changes need')} attention`;
    case 'no_changes':
      return 'No changes';
    case 'failed':
      return 'Could not finish';
    case 'discarded':
      return 'Discarded';
    case 'superseded':
      return 'Replaced by a later sync';
    default:
      return run.status;
  }
}

// ------------------------------------------------------------- home screen

export function headline(status) {
  const attention = status.attention || {};
  switch (status.state) {
    case 'setup':
      return 'Connect your photo libraries';
    case 'syncing':
      return 'Syncing…';
    case 'attention':
      return attention.conflicts
        ? `${plural(attention.conflicts, 'face needs', 'faces need')} a decision`
        : `${plural(attention.failures, 'face', 'faces')} could not be added`;
    case 'waiting_digikam':
      return 'Waiting for digiKam to close';
    case 'ready':
      return `${plural(readyChanges(status), 'change is', 'changes are')} ready to apply`;
    case 'paused':
      return status.automation.paused_until
        ? `Paused until ${timeOfDay(status.automation.paused_until)}`
        : 'Paused';
    case 'off':
      return 'Automatic sync is off';
    default:
      return 'Everything is in sync';
  }
}

function readyChanges(status) {
  return changeCount((status.run || {}).summary || {});
}

export function subline(status) {
  const last = status.last_completed;
  switch (status.state) {
    case 'setup':
      return 'Face Sync needs your digiKam folder and your Nextcloud account.';
    case 'syncing': {
      const run = status.run || {};
      return `Started ${ago(run.started_at)} because ${trigger(run.trigger)} changed something.`;
    }
    case 'attention':
      return 'Everything else keeps syncing around them.';
    case 'waiting_digikam': {
      const run = status.run || {};
      const pending = (run.apply || {}).pending;
      const waiting = pending === undefined || pending === null
        ? digikamChangeCount(run.summary || {})
        : pending;
      return `${plural(waiting, 'change', 'changes')} for digiKam will be applied when you quit digiKam.`;
    }
    case 'ready': {
      const run = status.run || {};
      return `Preview finished ${when(run.finished_at || run.started_at)}.`;
    }
    case 'paused':
      return 'Nothing will change in either library until it resumes.';
    case 'off':
      return last
        ? `Last sync ${when(last.finished_at || last.started_at)}.`
        : 'Face Sync has not synced yet.';
    default:
      return last
        ? `Last sync ${when(last.finished_at || last.started_at)} · ${plural(changeCount(last.summary), 'change', 'changes')}`
        : 'Face Sync has not synced yet.';
  }
}

/** A quieter third line, only where there is something worth adding. */
export function detail(status) {
  if (status.state === 'waiting_digikam') {
    const applied = ((status.run || {}).apply || {}).applied;
    if (applied) return `${plural(applied, 'change', 'changes')} were applied already.`;
    const summary = (status.run || {}).summary || {};
    const memories = Number(summary.assigned || 0) + Number(summary.inserted || 0);
    if (memories) return `${plural(memories, 'change', 'changes')} for Memories are ready.`;
  }
  if (status.state === 'ready' && !status.automation.apply_automatically) {
    return 'Automatic sync is on, but “Apply changes without asking” is off.';
  }
  if (status.state === 'off') {
    return 'Turn it on and Face Sync keeps both libraries in step by itself.';
  }
  return '';
}

export function attentionSummary(attention) {
  const parts = [];
  if (attention.conflicts) parts.push(`${count(attention.conflicts)} with different names`);
  if (attention.failures) parts.push(`${count(attention.failures)} that could not be added`);
  return parts.join(' · ');
}
