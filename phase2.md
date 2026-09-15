# Phase 2: automatic operation and robustness

Design document. Phase 1 delivered a working, manual, two-way face sync.
Phase 2 turns it into a background service that keeps both libraries in sync
without being watched, and that survives the things a desktop does to a
long-running process: digiKam being opened, laptops sleeping, and networks
dropping.

Audience: Keith, and whoever (or whatever) implements the milestones in
section 16. Everything here is meant to be buildable directly.

---

## 0. Decisions already taken

| Question | Decision | Why |
|---|---|---|
| digiKam open during an automatic run | Apply the Memories half immediately. Defer the digiKam half until digiKam closes. | Memories writes never need digiKam closed. digiKam does not notice external writes while running, so a copy-and-merge of `digikam4.db` would add risk without adding value. |
| Unambiguous changes in automatic mode | Applied automatically. Conflicts and rejected faces always wait for a person. | "Automatic" has to mean automatic. A "Review before applying" toggle disables it. |
| Service lifecycle | Starts at login as a user service. The launcher only opens the UI. | Nothing syncs if nothing is running. |
| Prototypes | ASCII wireframes in this document. Clickable HTML prototype built later from the brief in section 4.9. | Flow first, look second. |

Assumptions that were not questioned and are treated as settled:

- Automatic runs always use the **All faces** scope. One-person runs remain a manual shortcut.
- Triggers are **change-driven with a quiet period**, plus a fallback interval. There is no clock schedule to configure.
- The companion Nextcloud app gains two small read-only endpoints (section 9.3).
- Process detection becomes cross-platform via `psutil`. The service **never** closes digiKam itself. Only the manual flow may offer that, on an explicit click, as today.
- Single profile. The `profiles` table stays as is.
- Deletion propagation stays out of scope.

---

## 1. Goals and non-goals

**Goals**

1. Both libraries converge on their own. A rename in either library reaches the other without anyone opening Face Sync.
2. Face Sync only interrupts a person for a real decision: two names disagree, or a face was rejected.
3. An interrupted run resumes where it stopped. Never repeats a completed change. Never applies a stale one.
4. Everything the service does is visible afterwards: activity, logs, why it is waiting.
5. Setup happens once. Launching the app lands on a home screen, not a wizard.

**Non-goals for phase 2**

- Propagating face deletions.
- Rectangle-only drift (same name, slightly different box). Detected, not synced.
- Multiple digiKam libraries or Nextcloud accounts.
- Battery or metered-network awareness. Noted as a later gate.
- Copy-and-reconcile of the digiKam database.

---

## 2. What phase 2 builds on

Phase 1 already provides the hard parts of safe writing, and phase 2 reuses them unchanged:

- **Frozen plan.** Apply executes the action list saved by the preview, never a fresh comparison.
- **Per-action journal** in `run_actions`. Completed actions are idempotent, so resuming is "run whatever is still pending".
- **Verify before write.** Every action re-reads its target and refuses with a stale error if the name, rectangle or file ID moved.
- **Backup before the first digiKam write**, via the SQLite backup API.
- **Failure queue** with photo review, adjusted-rectangle retry, and remembered one-sided faces.

Gaps that phase 2 must close:

| Gap | Effect today | Phase 2 answer |
|---|---|---|
| `service` mode is a stub | Nothing runs unattended | Section 5 |
| Random port, per-process token | Nothing can find a running instance | `service.json` discovery, section 5.2 |
| `face_links` ledger written but never read | Every rename becomes a conflict | Change attribution, section 8 |
| Preview has no checkpoint (`session=None`) | A preview lost to sleep or crash restarts from zero | Section 7.2 |
| One open conflict blocks the whole Apply | One undecided face stops all automatic progress | Split plan, section 6.4 |
| digiKam detection Linux-only | macOS and Windows cannot gate writes | `psutil`, section 14 |
| No log destination but stderr | Nothing to look at after the fact | Section 11 |

Measured on Keith's library for sizing: WAL journal mode, 19 MB, 23,662 images, 13,713 face regions. Fingerprints and ledger writes at this size are sub-second. Design for 100k images, 500k regions without changing approach.

---

## 3. Product behaviour

### 3.1 Principles

- **One sentence tells you the state.** The home screen always has a single headline: in sync, syncing, waiting for X, paused, needs you, or broken.
- **Waiting is not failing.** digiKam open, Recognize busy, laptop offline: these are shown calmly as "waiting for…", with the reason and what will happen next. No red.
- **Interrupt only for decisions.** Notifications fire for faces needing a decision and for connection problems that persisted. Completed syncs are silent unless the user opts in. When Face Sync is already open and in front of the user, nothing is raised at all: the screen updates instead (section 12.1).
- **Never write to digiKam behind its back.** digiKam-side writes happen only while digiKam is closed, and stop the instant it starts.
- **Manual is a shortcut, not a different mode.** "Sync now" runs the same job the service would run, skipping the quiet period.

### 3.2 First run

Unchanged Connect screen (digiKam folder, Nextcloud address, username, app password, photos folder, checks for Recognize and the companion app). One new final step:

```
┌──────────────────────────────────────────────────────────────────┐
│  You're connected                                                 │
│  digiKam  /home/keith/Photos  ✓      Nextcloud  nc.vassallo.cloud ✓│
│                                                                   │
│  [x] Keep both libraries in sync automatically                    │
│      Face Sync checks for changes in the background, waits until  │
│      digiKam is closed before changing it, and only asks you when │
│      two names disagree.                                          │
│                                                                   │
│  [x] Start Face Sync when I log in                                │
│                                                                   │
│                                              [ Finish ]           │
└──────────────────────────────────────────────────────────────────┘
```

Both boxes default to on. Finish saves settings, installs the autostart entry if ticked, and lands on Home. If automatic sync is on, the first full sync is queued immediately and Home shows it running.

### 3.3 Home

Home is the only screen most users see. It is described in section 4.2 with wireframes for each state.

Headline states, in priority order (highest wins):

| State | Headline | Sub-line | Actions |
|---|---|---|---|
| Setup needed | Connect your libraries | Settings are missing or failed verification | Open setup |
| Needs attention | 12 faces need a decision | Sync continues around them | Review |
| Syncing | Syncing… | Phase, progress bar, live counts | View details |
| Waiting: digiKam | Waiting for digiKam to close | 31 digiKam changes ready. Memories changes applied at 14:02 | Sync now, Pause |
| Waiting: Recognize | Waiting for Recognize to finish | Nextcloud is still recognising faces | Sync now, Pause |
| Waiting: connection | Can't reach Nextcloud | Retrying in 4 min. Last success 09:12 | Try now, Pause |
| Paused | Paused until 18:00 | Nothing will change until then | Resume, Sync now |
| Off | Automatic sync is off | Last sync 3 days ago | Turn on, Sync now |
| Ready to apply (review mode) | 12 changes are ready to apply | Preview finished 09:12 | Review, Apply |
| In sync | Everything is in sync | Last sync today 09:12 · 3 changes · next check in 4 min | Sync now, Pause |

### 3.4 Sync now

A split button.

- **Sync everything now.** Queues a manual full job. Jumps the queue. Skips the quiet period. Respects gates, but for the digiKam gate the manual flow may offer "Close digiKam" as it does today.
- **Sync one person…** Opens the existing person picker, then runs a person-scoped job.
- **Preview only.** Runs a job with `auto_apply=false` regardless of settings. Ends on the run detail screen with the Apply button.

If a job is already running, the button reads "View progress".

### 3.5 Pause and resume

Pause menu: **Pause for 1 hour · Pause until tomorrow · Pause until I resume.** Pause stops new jobs from starting and stops the current job at its next safe point (end of the current batch or action). Pending work stays pending. Resume continues it. Pausing never discards a plan.

### 3.6 Needs attention

A single inbox across all runs. Two kinds of items, both reusing phase 1 screens:

- **Different names.** Conflict review, one face at a time, as today.
- **Couldn't be added.** Failure review with rectangle adjustment, as today.

Resolving a batch of conflicts produces a small follow-up run containing only those actions. In automatic mode with auto-apply it applies immediately (Memories half) or defers (digiKam half). In review mode it lands as "ready to apply".

### 3.7 Activity

Reverse-chronological run list. Each row: time, what started it, outcome, change count. Clicking opens run detail, which is phase 1's Preview, Apply and result screens folded into one page with a timeline at the top.

### 3.8 Logs

In-app log viewer with level filter, run filter, text search, and "Copy for bug report". Points at the log folder on disk.

### 3.9 Settings

Grouped, always editable, and verified on save with the same checks as first run. A sync that starts while settings are being edited uses the last saved settings.

### 3.10 Shortcuts

"Add to application menu" in Settings installs a `.desktop` file on Linux, a minimal `Face Sync.app` in `~/Applications` on macOS, and a Start Menu shortcut on Windows. All run `face-sync ui`, which opens the browser at the running service, starting the service first if needed.

---

## 4. UI design

### 4.1 Shell and navigation

The five-step rail is replaced by a section rail. Setup is not a section. It is a full-screen flow that appears only when settings are missing or invalid.

```
┌────────────────────────────────────────────────────────────────────────┐
│ ◎ Face Sync   digiKam ↔ Memories                     ● Connected      │
├─────────────┬──────────────────────────────────────────────────────────┤
│ ▸ Home      │                                                          │
│   Activity  │                                                          │
│   Needs     │              (section content)                           │
│   attention │                                                          │
│   Logs      │                                                          │
│   Settings  │                                                          │
│             │                                                          │
│ ● Running   │                                                          │
│   in the    │                                                          │
│   background│                                                          │
└─────────────┴──────────────────────────────────────────────────────────┘
```

The rail badge on Needs attention shows the open count. The footer of the rail states whether the background service is running, and links to Settings if it is not.

### 4.2 Home

**In sync**

```
┌──────────────────────────────────────────────────────────────────────┐
│  Everything is in sync                                                │
│  Last sync today 09:12 · 3 changes · Next check in 4 min              │
│                                                                       │
│  [ Sync now ▾ ]   [ Pause ▾ ]                                         │
│                                                                       │
│  ┌─ Libraries ─────────────────────────────────────────────────────┐ │
│  │ ▣ digiKam    /home/keith/Photos                    closed   ✓   │ │
│  │ ☁ Nextcloud  nc.vassallo.cloud · keith                      ✓   │ │
│  │ ◌ Recognize  idle                                               │ │
│  └─────────────────────────────────────────────────────────────────┘ │
│                                                                       │
│  Recent activity                                        All activity →│
│  09:12 today     Applied 3 changes            digiKam closed    auto  │
│  22:40 yesterday No changes                   daily check       auto  │
│  18:03 yesterday Applied 41 changes           Memories changed  auto  │
│  17:10 yesterday 12 conflicts need a decision  you              manual│
└──────────────────────────────────────────────────────────────────────┘
```

**Syncing**

```
│  Syncing…                                                             │
│  Checking digiKam photos    ▰▰▰▰▰▰▰▱▱▱▱▱  61%   1,412 of 2,310        │
│  1,388 matched · 9 changes · 0 conflicts                              │
│                                                                       │
│  [ View details ]   [ Pause ▾ ]                                       │
```

**Waiting for digiKam**

```
│  Waiting for digiKam to close                                         │
│  31 changes for digiKam are ready and will be applied when you quit   │
│  digiKam. 12 changes for Memories were applied at 14:02.              │
│                                                                       │
│  [ Sync now ▾ ]   [ Pause ▾ ]                                         │
│                                                                       │
│  ┌─ Libraries ─────────────────────────────────────────────────────┐ │
│  │ ▣ digiKam    /home/keith/Photos                    open     ◐   │ │
```

**Needs attention** (card appears above Libraries whenever the count is above zero, in any state)

```
│  ┌─ Needs attention ───────────────────────────────────────────────┐ │
│  │ 12 faces have different names in each library      [ Review ]   │ │
│  │  3 faces could not be added                        [ Review ]   │ │
│  └─────────────────────────────────────────────────────────────────┘ │
```

**Can't reach Nextcloud**

```
│  Can't reach Nextcloud                                                │
│  Retrying in 4 minutes. Last successful sync today 09:12.             │
│  nc.vassallo.cloud did not respond (timed out).                       │
│                                                                       │
│  [ Try now ]   [ Pause ▾ ]   [ Check settings ]                       │
```

**Paused**

```
│  Paused until 18:00                                                   │
│  Nothing will change in either library until then.                    │
│                                                                       │
│  [ Resume ]   [ Sync now ▾ ]                                          │
```

**Ready to apply** (review mode only)

```
│  12 changes are ready to apply                                        │
│  Preview finished 09:12 · 9 for Memories · 3 for digiKam              │
│                                                                       │
│  [ Review changes ]   [ Apply ]                                       │
```

### 4.3 Sync now and Pause menus

```
[ Sync now ▾ ]                      [ Pause ▾ ]
 ├ Sync everything now               ├ Pause for 1 hour
 ├ Sync one person…                  ├ Pause until tomorrow
 └ Preview only (don't apply)        └ Pause until I resume
```

### 4.4 Activity and run detail

```
│  Activity                                         [ All ▾ ] [ Auto ▾ ]│
│  Today                                                                │
│  14:02  Waiting for digiKam · 12 applied, 31 waiting   Memories changed│
│  09:12  Applied 3 changes                              digiKam closed │
│  Yesterday                                                            │
│  22:40  No changes                                     daily check    │
│  18:03  Applied 41 changes                             Memories changed│
│  17:10  12 conflicts · 29 applied                      you            │
```

Run detail:

```
│  ← Activity                                                           │
│  Sync · today 14:02 · started because Memories changed                │
│                                                                       │
│  ● Checked 2,140 photos in both libraries          14:02 – 14:04      │
│  ● Applied 12 changes to Memories                  14:04              │
│  ◐ 31 changes for digiKam waiting for digiKam to close                │
│                                                                       │
│  ┌ 1,988 ┐ ┌ 12 ┐ ┌ 31 ┐ ┌ 0 ┐                                         │
│  │correct│ │Mem.│ │dK  │ │dec.│   (phase 1 stat tiles)                │
│                                                                       │
│  What the preview found                (phase 1 results list)         │
│  Backup: created before the first digiKam change                      │
│  Log                                                    [ Open Logs ] │
│  14:04:31  Applied 12 Memories changes                                │
│  14:04:31  digiKam is running; 31 changes deferred                    │
```

In review mode the run detail ends with phase 1's Apply card, including the "Close digiKam" step.

### 4.5 Needs attention

```
│  Needs attention                                                      │
│                                                                       │
│  Different names                                          12 faces    │
│  The same face has a different name in digiKam and Memories.          │
│  Newest from today 14:02.                               [ Review ]    │
│                                                                       │
│  Couldn't be added                                        3 faces     │
│  Memories rejected these faces. Adjust the box or keep them in        │
│  digiKam only.                                           [ Review ]    │
```

Review opens the phase 1 conflict or failure screens unchanged, except that the position counter counts across all runs and the final screen returns here.

### 4.6 Logs

```
│  Logs           Level [ Info ▾ ]  Run [ All ▾ ]  Search [          ]  │
│                                                          [ Follow ]   │
│  14:04:31  INFO   run 88   digiKam is running; 31 changes deferred    │
│  14:04:31  INFO   run 88   Applied 12 Memories changes                │
│  14:02:55  INFO   run 88   Scanning digiKam 2,140 of 2,310            │
│  14:02:11  INFO   run 88   Loaded 13,713 Memories faces               │
│  14:02:10  INFO   coord    Starting run 88: Memories changed           │
│  13:52:10  DEBUG  coord    Memories fingerprint changed; quiet period  │
│                                                                       │
│  [ Copy for bug report ]      Files: ~/.config/digikam-memories-sync/logs│
```

### 4.7 Settings

```
│  Settings                                                             │
│                                                                       │
│  Libraries                                                            │
│    digiKam folder      /home/keith/Photos                  [ Change ] │
│    Nextcloud           nc.vassallo.cloud · keith           [ Change ] │
│    Photos folder       Photos                                         │
│                                             [ Test connection ]       │
│                                                                       │
│  Automatic sync                                       (●) On          │
│    [x] Apply changes without asking                                   │
│        Faces with two different names always wait for you.            │
│    Wait  [ 10 ] minutes after the last change before syncing          │
│    Check for changes every  [ 5 minutes ▾ ]                           │
│    Sync at least every      [ day ▾ ]                                 │
│                                                                       │
│  Notifications                                                        │
│    [x] Faces need a decision      [x] Connection problems             │
│    [ ] Every completed sync                                           │
│    Notify me in this browser            not asked yet   [ Enable ]    │
│                                                                       │
│  Background service                                                   │
│    (●) Start Face Sync when I log in            ● running since 08:01 │
│    [ Add to application menu ]                                        │
│                                                                       │
│  Storage                                                              │
│    Backups   keep the last [ 10 ]   187 MB           [ Open folder ]  │
│    Logs      keep [ 30 ] days                        [ Open folder ]  │
│                                                                       │
│  Advanced                                                             │
│    [ Rebuild face ledger ]   [ Forget faces kept in one library ]     │
```

"Change" on a library re-opens the matching setup card inline, with the same Test and Save behaviour as first run.

### 4.8 Copy rules

- Headlines are states, not verbs: "Waiting for digiKam to close", not "Deferring digiKam writes".
- Always say what happens next: "will be applied when you quit digiKam".
- Numbers first: "12 faces need a decision".
- Never show internal words: run, job, gate, ledger, fingerprint, plan.

### 4.9 Prototype

Built: `docs/prototypes/face-sync-phase2.html`. Nine screens, the eight Home states, both run-detail variants, and the two review screens carried over from phase 1.

It keeps the phase 1 prototype's conventions: `#face-sync-prototypes` scope, `--fs-*` tokens, `fs-` class names, segmented switcher, `data-screen` sections, no framework and no external assets.

Two deliberate differences from `docs/prototypes/face-sync-ui.html`:

- It is a standalone document with a doctype rather than a fragment, so it opens directly in a browser.
- Icons are Unicode glyphs (`◎ ▣ ☁ ◌`) as in the shipped `digikam_nextcloud/web/index.html`, not the `data-lucide` placeholders the phase 1 prototype used. Those render blank without the Lucide script, which the no-external-assets rule forbids.

It also adds a light/dark/auto switch above the window, because `light-dark()` otherwise follows the operating system and both themes need reviewing.

What the prototype fakes rather than models: photos are CSS gradients, all counts are fixed, and no state persists. It exists to settle the flow and the wording, not the rendering.

---

## 5. Process model

### 5.1 Commands

| Command | Role |
|---|---|
| `face-sync service` | The long-running process. Owns the HTTP server, the coordinator, the pollers, the job runner, logging and notifications. Exactly one per config directory. |
| `face-sync ui` | Launcher used by shortcuts. Finds the running service via `service.json`. If none, starts `face-sync service` detached, waits for health, then opens the browser. |
| `face-sync run` | Unchanged CLI. Shares the state database read-only for reporting and refuses to write while a service holds the lock. |
| `face-sync autostart enable/disable` | Same as the Settings toggle, for scripting. |
| `face-sync shortcuts install` | Same as the Settings button. |

Module layout: `service.py` (lifecycle, lock, `service.json`), `launcher.py`, `coordinator.py` (gates and job state machine), `triggers.py` (fingerprints and pollers), `ledger.py` (change attribution), `log_store.py`, `notify.py`, `autostart.py`, `shortcuts.py`. `AppService` keeps its role as the UI-facing facade and grows the new endpoints.

### 5.2 Discovery and singleton

- `service.lock` in the config directory, held with `fcntl.flock` on POSIX and `msvcrt.locking` on Windows. A second `service` exits with code 3 and prints the running URL.
- `service.json` (mode 0600): `{ "pid", "port", "token", "started_at", "version" }`. Written after bind, removed on clean shutdown. Stale files are detected by the lock, not by pid.
- Port: try the configured port (default 47818), fall back to an ephemeral one. Always bind `127.0.0.1` only.
- Token: generated per service start, stored in `service.json` so `face-sync ui` can call `/api/health`. Injected into `index.html` as today.

### 5.3 Autostart

| Platform | Mechanism | Path |
|---|---|---|
| Linux | systemd user unit, `WantedBy=default.target`, `Restart=on-failure`, `RestartSec=10`; falls back to `~/.config/autostart/*.desktop` when systemd user sessions are absent | `~/.config/systemd/user/face-sync.service` |
| macOS | launchd agent, `RunAtLoad`, `KeepAlive` with `SuccessfulExit=false` | `~/Library/LaunchAgents/com.keithvassallo.face-sync.plist` |
| Windows | `HKCU\Software\Microsoft\Windows\CurrentVersion\Run` entry running `pythonw -m digikam_nextcloud service` | registry |

The unit runs the same interpreter and entry point that installed it (`sys.executable -m digikam_nextcloud service`), so virtualenv installs work. Enabling starts the service immediately; disabling stops it after asking the running instance to finish its current action.

### 5.4 Shutdown

`SIGTERM` (or the Windows console event) sets a stop flag. The job runner finishes the current action or batch, checkpoints, marks the run `waiting` with reason `service_stopped`, and exits. On next start, recovery re-queues it (section 7.3).

---

## 6. Coordinator

The coordinator is a single thread with a tick every 10 seconds. It owns the job queue, evaluates gates, and starts or resumes at most one job at a time. It is written as a pure state machine over an injected clock and injected probes so it can be tested without a real digiKam, network or filesystem.

### 6.1 Gates

| Gate | What it blocks | Probe | Recheck cadence |
|---|---|---|---|
| Paused | starting any job; continuing past the next safe point | `service_state.paused_until` | on tick |
| Automation off | starting automatic jobs (manual still runs) | settings | on tick |
| Connection | anything that touches Nextcloud | cached result of the last probe or request failure | backoff, section 7.4 |
| Recognize busy | starting a job; Memories writes mid-apply | companion `status.recognize_busy` | 5 min |
| digiKam running | digiKam writes only | process probe | 10 s while digiKam work is pending, else 60 s |
| Job running | starting another job | in-memory | event |

Gates are evaluated per action target, not per job. A job whose remaining work is all digiKam-side and blocked by the digiKam gate sits in `deferred`, and the Memories half is not held back.

### 6.2 Triggers

| Trigger | Source | Notes |
|---|---|---|
| `digikam_closed` | process probe saw digiKam disappear, settle period elapsed, fingerprint changed | the common case for digiKam-side edits |
| `digikam_changed` | `digikam4.db` or `-wal` mtime changed while digiKam is not running, fingerprint changed | external tools, restores |
| `memories_changed` | companion `changes` fingerprint differs from the last synced one | polled every `check_interval` |
| `interval` | `fallback_interval` elapsed since the last completed job | safety net |
| `manual` | Sync now | skips the quiet period |
| `decisions` | user resolved conflicts or failures | follow-up run with only those actions |
| `resume` | service start found unfinished work | section 7.3 |

### 6.3 Quiet period and coalescing

A change trigger does not start a job. It sets `pending_trigger_at = now + quiet_period` (default 10 min) and records the reason. Further changes push it out again, but never past `first_change_at + 60 min`, so a library that is edited all afternoon still syncs hourly. When `pending_trigger_at` passes and gates allow, one job starts with all accumulated reasons.

While a job is running, new triggers are recorded and evaluated after it finishes, so a change made during a sync is picked up by the next one rather than lost.

### 6.4 Job state machine

```
 trigger ─▶ queued ─▶ (gates) ─▶ previewing ─▶ previewed
                        ▲            │ checkpoint every batch
                        │            │
                    waiting ◀────────┘ systemic failure: backoff
                  (reason,
                  next_attempt)

 previewed ─┬─ no proposed changes, no conflicts ──▶ no_changes
            ├─ auto_apply off ────────────────────▶ previewed  (Home: ready to apply)
            └─ auto_apply on ─────────────────────▶ applying
                                                       │
            Memories actions ◀── gate: Recognize idle ─┤
            digiKam actions  ◀── gate: digiKam closed ─┤
                                                       │ digiKam running
                                                       ▼
                                                    deferred ── digiKam closes + settle ──▶ applying
                                                       │
                              applied · applied_with_issues · failed

 conflicts found ─▶ open conflicts (deduplicated across runs) ─▶ Needs attention
 user decides    ─▶ new run, trigger=decisions ─▶ applying …
```

Run statuses become: `queued, waiting, previewing, previewed, applying, deferred, applied, applied_with_issues, no_changes, failed, discarded, superseded`. `apply_failed` is renamed `applied_with_issues` because in automatic mode it is the normal outcome of a few rejected faces, not a failure of the run.

**Plan split.** `build_apply_plan` no longer refuses when conflicts are open. It returns the unambiguous actions and leaves conflicts aside. Conflict resolutions are applied by a `decisions` run. The rule "resolve everything before applying" was right for a manual wizard and wrong for a service.

**Superseded.** When a new full job completes, older `previewed` runs that were never applied become `superseded`. Their conflicts are re-evaluated by the new run (section 8.4), not carried blindly.

### 6.5 Manual and automatic interplay

- A manual job while an automatic job runs: attach to the running job and show its progress. Do not queue a second one.
- An automatic trigger while a manual review is pending (`previewed`, review mode): allowed. The new run supersedes the old preview and Home shows the newer numbers. The user never applies stale numbers.
- Manual runs may prompt to close digiKam. Automatic runs never do.

---

## 7. Robustness

### 7.1 digiKam is open

**Before digiKam writes start.** The digiKam gate is checked. If digiKam is running, all digiKam-target actions stay `pending` and the run enters `deferred`. Home says so. The Memories half proceeds.

**While digiKam writes are in progress.** The apply loop checks the process probe before every digiKam action (a `/proc` scan is about a millisecond; `psutil` similar). The instant digiKam appears, the current transaction completes atomically, no further digiKam action starts, and the run moves to `deferred`. A user launching digiKam mid-apply loses nothing and sees nothing odd: the writes that landed are complete rows that digiKam reads normally at startup.

**When digiKam closes.** The probe sees the process disappear. Wait a settle period (15 s), then confirm the database is not locked by opening a write transaction and rolling it back. Resume pending digiKam actions. Each one re-verifies its target as today, so edits the user made in digiKam meanwhile turn individual actions into review items rather than overwrites.

**Staleness.** Deferred actions are not discarded after a timeout. Verification makes them safe at any age. After a deferred run completes, the coordinator computes fingerprints so any edits made during the wait trigger a follow-up sync through the normal path.

**Flatpak, Snap, AppImage.** The process name is still `digikam` inside the sandbox and visible in `/proc`. Match `comm` or the executable basename, as now, and additionally any `cmdline` containing `/digikam` to cover wrappers. Document the limitation that a digiKam running on another machine against a shared database cannot be seen; the write-lock probe is the fallback there.

### 7.2 Sleep, suspend, and the preview checkpoint

**Detecting a sleep.** The coordinator tick compares wall clock to monotonic clock. A jump above 60 s means the machine slept. Any in-flight HTTP request will fail on its own with a timeout. On wake the coordinator marks the connection gate unknown, re-probes before continuing, and logs "resumed after sleep".

**Preview checkpoint.** The preview has three phases:

1. Load named Memories faces (paginated HTTP, `after` cursor).
2. Scan digiKam images in id-ordered batches, matching forward.
3. Scan Memories faces grouped by path, matching backward.

A checkpoint row (`run_checkpoints`) records the phase, the cursor (last digiKam image id for phase 2, last path index for phase 3), and the report counters. Actions and conflicts found so far are appended per batch to `preview_actions` and `conflicts` rather than held in memory until the end. Resume re-does phase 1 (cheap, 14 requests for this library), then continues phase 2 or 3 from the cursor. `iter_image_batches` already accepts `skip_image_ids`; add a `start_after_image_id` so resume is O(1) rather than a set of ids.

A resumed preview computed its halves at different moments. That is acceptable because Apply verifies every target before writing.

**Apply checkpoint.** Already exists: `run_actions` with pending, applied, failed, ignored.

### 7.3 Service restart and crash

Today `recover_interrupted_previews` marks `previewing` as failed and `recover_interrupted_applies` marks `applying` as failed. Phase 2 replaces both with `recover_unfinished_runs`:

| Found | Becomes | Then |
|---|---|---|
| `previewing` with checkpoint | `waiting` (reason `resume`) | re-queued, resumes from cursor |
| `previewing` without checkpoint | `waiting` (reason `resume`) | re-queued, starts over |
| `applying` or `deferred` | `waiting` (reason `resume`) | re-queued, pending actions resume |
| `queued` | unchanged | evaluated normally |

Manual runs recover the same way and additionally post a quiet notification so the user knows why Home says "syncing" after a reboot.

### 7.4 Connection failures

Two classes, already distinguished by `is_systemic_apply_failure`:

- **Per-face** failures (stale target, rejected face) go to the review queue and the run continues.
- **Systemic** failures (timeout, 401, 5xx, DNS) stop the run at its checkpoint and move it to `waiting` with reason `connection` and a backoff schedule: 1, 2, 5, 15, 30, then 60 minutes repeating. The next attempt resumes, it does not restart.

Notifications: after the first failure, nothing. After 6 hours of continuous failure, one `connection.action_required` notification. Not one per retry. A 401 is different: it means the app password was revoked, and it notifies immediately and stops retrying until settings change.

Preview and apply both pre-flight with `connection_requirements()` as today, so a wake-up on a captive-portal network fails fast instead of half-way.

---

## 8. Change attribution (the ledger)

Auto-apply is only safe if a rename on one side is recognised as "that side changed", not "the two sides disagree". Today every disagreement is a conflict. Phase 2 makes `face_links` authoritative.

### 8.1 Ledger content

Per paired face: digiKam image and tag id, Nextcloud file and detection id, the names on both sides at the last successful sync, both rectangles, and `last_synced_at`. Table exists. Two additions: `synced_name` (the single agreed name, since both sides agree after a sync) and an index on `(nextcloud_file_id, nextcloud_detection_id)`.

### 8.2 Attribution rule

For a matched pair with digiKam name `A` and Memories name `B`, where the ledger's last agreed name is `S`:

| Condition | Meaning | Action |
|---|---|---|
| `A == B` | in sync | refresh the ledger row |
| `A == S`, `B != S` | Memories changed | `reassign_digikam` to `B`, automatic |
| `B == S`, `A != S` | digiKam changed | `assign_memories` to `A`, automatic |
| `A != S`, `B != S`, `A != B` | both changed | conflict |
| no ledger row | unknown history | conflict, as today |

Name comparison uses `person_names_match`, as everywhere else. Rectangles are matched by IoU exactly as now; the ledger does not change pairing, only the decision once paired.

A person renamed in Memories (cluster title change) shows up as many faces where `B` changed. Each becomes an automatic `reassign_digikam`. The digiKam writer creates the new person tag on first use, as it does today. Person-level rename detection is a later optimisation, not a correctness need.

### 8.3 Keeping the ledger complete

Today only applied actions write links. The "already correct" pairs never do, so after phase 1 the ledger covers only faces Face Sync touched. Phase 2:

- Every preview writes or refreshes a ledger row for every matched pair that agrees (`skip` actions carry both ids already). Batched insert, one transaction per scan batch.
- Every applied action writes its row as today.
- **Baseline on upgrade.** The first phase 2 run, and the "Rebuild face ledger" button, run a preview whose only side effect is ledger rows for agreeing pairs. Disagreeing pairs at that moment still need one human decision each. After that the ledger carries the history.

### 8.4 Conflicts across runs

Conflicts get an identity key: `(digikam_image_id, digikam_tag_id, nc_file_id, nc_detection_id)`, with `(path, digikam_rect, nextcloud_rect)` as fallback when ids are missing. On each preview:

- A conflict with the same key as an open one is not inserted again. The open one is refreshed with current names.
- An open conflict whose faces now agree, or whose faces no longer exist, is closed with resolution `resolved_externally`.
- A resolved conflict whose decision was applied is closed normally.

Needs attention therefore shows a stable list, not a growing one.

---

## 9. Change detection

### 9.1 digiKam fingerprint

Read-only, computed with the existing `mode=ro` connection:

```
regions  = sha256 over rows of (imageid, tagid, value)
           FROM ImageTagProperties WHERE property='tagRegion' ORDER BY rowid
people   = sha256 over rows of (t.id, t.name, tp.value)
           FROM Tags t JOIN TagProperties tp ON tp.tagid=t.id AND tp.property='person'
           ORDER BY t.id
count    = COUNT(*) of tagRegion rows
```

Fingerprint is `regions:people:count`. About 40 ms for 13,713 rows. Under a second at 500k. Computed only when the cheap pre-checks say something might have changed: `digikam4.db` or `digikam4.db-wal` mtime moved, or digiKam just exited. Never computed while digiKam is running, since the result would be discarded anyway.

The fingerprint at the end of each completed job is stored as `last_synced_digikam_fingerprint`.

### 9.2 Memories fingerprint

Polled from the companion app every `check_interval` (default 5 min). One request, one small JSON body. The service compares it with `last_synced_memories_fingerprint`.

### 9.3 Companion app additions (version 0.5.0)

Two routes, both `NoAdminRequired`, both read-only, both under the existing `api/v1` prefix:

**`GET /api/v1/changes`**

```json
{
  "detections": { "count": 13713, "max_id": 918273, "checksum": "a1b2…" },
  "clusters":   { "count": 212,   "titles_hash": "c3d4…" }
}
```

`checksum` is a hash over `(id, cluster_id)` for the user's detections in named clusters, computed with one aggregate query. Any assignment, unassignment or new detection changes it. `titles_hash` catches renames.

**`GET /api/v1/status`**

```json
{ "recognize_busy": true, "jobs": ["ClassifyFacesJob"], "since": "2026-09-15T02:10:00Z" }
```

Derived from `oc_jobs` rows whose class starts with `OCA\Recognize\BackgroundJobs\` and whose `reserved_at` is non-zero and younger than 12 hours (Nextcloud clears stale reservations at that age). Busy blocks job start and Memories writes. It does not block digiKam writes.

The capabilities document adds `changeFingerprint: true` and `recognizeStatus: true`. When absent the client degrades: Memories changes are picked up by the fallback interval only, and the Recognize gate is treated as idle. Home notes "Update the Face Sync app in Nextcloud for faster change detection".

### 9.4 Fallback interval

Default once per day, measured from the last completed job. Covers anything the fingerprints miss. In Settings as "Sync at least every: 6 hours · day · week".

---

## 10. Data model

### 10.1 `settings.json` version 2

```json
{
  "version": 2,
  "digikam_library": "/home/keith/Photos",
  "digikam_db": "/home/keith/Photos/digikam4.db",
  "nextcloud_url": "https://nc.vassallo.cloud",
  "nc_user": "keith",
  "nc_photos_path": "Photos",
  "automation": {
    "enabled": true,
    "apply_automatically": true,
    "quiet_period_minutes": 10,
    "check_interval_minutes": 5,
    "fallback_interval_hours": 24
  },
  "notifications": {
    "decisions": true,
    "connection": true,
    "completed": false
  },
  "service": { "autostart": true, "port": 47818 },
  "retention": { "backups_keep": 10, "log_days": 30 }
}
```

Version 1 files are migrated in place on first load, with the automation block absent (off) so an upgrade never starts syncing without the user seeing Home once.

### 10.2 State database additions

```sql
ALTER TABLE runs ADD COLUMN trigger TEXT NOT NULL DEFAULT 'manual';
ALTER TABLE runs ADD COLUMN auto_apply INTEGER NOT NULL DEFAULT 0;
ALTER TABLE runs ADD COLUMN waiting_reason TEXT;
ALTER TABLE runs ADD COLUMN next_attempt_at TEXT;
ALTER TABLE runs ADD COLUMN attempts INTEGER NOT NULL DEFAULT 0;

CREATE TABLE run_checkpoints (
  run_id INTEGER PRIMARY KEY REFERENCES runs(id) ON DELETE CASCADE,
  phase TEXT NOT NULL,
  cursor_json TEXT NOT NULL DEFAULT '{}',
  counters_json TEXT NOT NULL DEFAULT '{}',
  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE preview_actions (
  id INTEGER PRIMARY KEY,
  run_id INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
  action_json TEXT NOT NULL
);
CREATE INDEX preview_actions_run ON preview_actions(run_id);

ALTER TABLE conflicts ADD COLUMN identity_key TEXT;
CREATE INDEX conflicts_open_identity ON conflicts(identity_key) WHERE status = 'open';

ALTER TABLE face_links ADD COLUMN synced_name TEXT;
CREATE INDEX face_links_remote ON face_links(nextcloud_file_id, nextcloud_detection_id);

CREATE TABLE service_state (
  key TEXT PRIMARY KEY,
  value_json TEXT NOT NULL,
  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
-- keys: paused_until, pending_trigger_at, pending_trigger_reasons,
--       last_synced_digikam_fingerprint, last_synced_memories_fingerprint,
--       last_memories_check_at, recognize_busy_since, last_completed_at,
--       connection_failed_since, ledger_baselined_at

CREATE TABLE log_entries (
  id INTEGER PRIMARY KEY,
  ts TEXT NOT NULL,
  level TEXT NOT NULL,
  logger TEXT NOT NULL,
  run_id INTEGER,
  message TEXT NOT NULL
);
CREATE INDEX log_entries_ts ON log_entries(ts);
CREATE INDEX log_entries_run ON log_entries(run_id);
```

`run_results.result_json` continues to hold the frozen preview for Apply. It is now assembled from `preview_actions` when the preview finishes, so a resumed preview produces the same shape as an uninterrupted one.

### 10.3 API additions

All under the existing token check.

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/status` | Home aggregate: headline state, sub-line facts, gates, current run summary, next check, service info, attention counts. Also returns any notifications the service has marked for browser delivery. The request carries `visible` and `permission` so the service can choose a channel (section 12.1). One call, polled every 2 s while the tab is visible and every 30 s while it is hidden. |
| POST | `/api/sync` | `{ scope, person?, preview_only? }`. Returns the run id, or the running run if one exists. |
| POST | `/api/automation/pause` | `{ until: ISO or "indefinite" }` |
| POST | `/api/automation/resume` | |
| GET | `/api/runs?limit=&before=` | Activity list |
| GET | `/api/runs/{id}` | Run detail, extended with timeline and log tail |
| GET | `/api/attention` | Open conflicts and failures across runs |
| GET | `/api/logs?level=&run_id=&q=&after_id=&limit=` | Log page, incremental |
| GET, POST | `/api/settings` | Extended settings; POST verifies libraries as today |
| POST | `/api/service/autostart` | `{ enabled }` |
| POST | `/api/shortcuts/install` | |
| POST | `/api/notifications/delivered` | `{ ids }`. The page acknowledges the browser notifications it raised so they are never raised twice. |
| POST | `/api/ledger/rebuild` | Queues a baseline run |

Existing run, conflict and failure endpoints stay as they are.

---

## 11. Logging

- Root logger gets two handlers in service mode: a `RotatingFileHandler` at `<config>/logs/face-sync.log` (5 files × 5 MB) and a `SQLiteLogHandler` writing `log_entries` through a queue so logging never blocks the job thread.
- A `contextvars.ContextVar` holds the current run id. The job runner sets it, the handler stamps it, so the Logs page can filter by run and the run detail can show its own tail.
- Retention: rows older than `retention.log_days` are deleted nightly. `DEBUG` rows older than 3 days are deleted regardless.
- CLI mode keeps the stderr handler and adds nothing.
- Log lines are written for the user, not the developer, at INFO: what started, what was found, what was applied, what is being waited on and why. DEBUG carries the rest.

---

## 12. Notifications

Every event is written to the `notifications` table before anything else is attempted, so the in-app inbox is always the authoritative record. Delivery beyond the inbox uses two channels, and a given event uses exactly one of them.

Existing event types stay. New: `sync.deferred` (inbox only), `automation.paused` (inbox only), `service.started` (inbox only). Delivery is opt-in per category as in Settings.

### 12.1 Choosing the channel

The **browser channel** is a `Notification` raised by the open UI page. The **operating-system channel** is raised by the service process. The service picks one when the event is created:

| Situation | Channel | Why |
|---|---|---|
| UI open and focused | none | the user is looking at Face Sync, and the attention card and rail badge appear in front of them |
| UI open, not focused, permission granted | browser | one implementation on every platform, and its click behaviour is reliable |
| UI open, permission refused or unavailable | operating system | the page cannot notify |
| UI closed | operating system | the only channel there is |

The service can tell these apart because the UI already polls `/api/status` every two seconds. The poll carries the page's visibility and its notification permission. A client seen within the last ten seconds counts as open.

Without this rule the two channels would both fire and the user would be told twice.

### 12.2 Browser channel

`http://127.0.0.1` is a potentially trustworthy origin, so the `Notification` API is available to the local page with no TLS certificate. No service worker is involved: the page raises the notification itself, and a page-owned notification only survives while the page is open, which is exactly the case this channel covers.

- **Permission** must be requested from a user gesture, so it is never requested on load. Settings has a "Notify me in this browser" row with an Enable button, the first-run final step offers it, and the attention card shows an inline "Turn these on" link while permission is still unasked.
- **Grouping** uses `tag: "<run id>-<category>"`, so a repeated event for the same run replaces its predecessor instead of stacking.
- **Clicking** focuses the window and navigates to the notification's stored target, which is the run, conflict or failure screen it refers to.
- **Acknowledgement**: the page posts the ids it raised to `/api/notifications/delivered`, so a row is never raised twice across polls or page reloads.
- **Zero-permission fallback**: whenever anything is waiting, the page prefixes its document title, as in `(12) Face Sync`. This needs no permission at all and is the only signal some users will ever accept.

One cosmetic cost is worth knowing before building this. Browsers label a notification with its origin, so this one reads `127.0.0.1:47818` rather than `Face Sync`. Nothing can change that from inside the page. The operating-system channel does show the application name, so the better-looking notification is the one used when the UI is closed. The prototype shows the origin line so the real appearance is not a surprise.

### 12.3 Operating-system channel

| Platform | Delivery |
|---|---|
| Linux | `notify-send` with app name and icon |
| macOS | `osascript -e 'display notification'` |
| Windows | PowerShell toast via `Windows.UI.Notifications`, falling back to a balloon |

Click-to-open is **best effort** on this channel and deliberately not relied upon. A `notify-send` callback needs a blocking action listener, `osascript` offers no callback at all, and a Windows toast needs a registered application identity. Since the browser channel covers the case where the user is at the machine with Face Sync already open, the operating-system channel can stay a plain one-way message. A user who clicks a dead notification and opens Face Sync manually still lands on the inbox with everything waiting.

This is the reason section 16 can deliver useful notifications in M2 and leave the three-platform work to M6.

### 12.4 What they say

| Event | Title | Body |
|---|---|---|
| `conflicts.created` | 12 faces need a decision | The same face has a different name in each library. |
| `apply.completed` with rejects | 3 faces could not be added | Memories rejected them. Adjust the box or keep them in digiKam. |
| `connection.action_required` | Can't reach Nextcloud | Face Sync keeps retrying. Last sync today 09:12. |
| `connection.action_required` on 401 | Nextcloud rejected the app password | Face Sync has stopped until you update it in Settings. |
| `run.completed` (opt in) | Sync finished | 41 face changes applied. |

Counts are current at the moment of delivery, not at the moment the event was recorded, so a notification that arrives after several runs does not understate the queue.

---

## 13. Retention

- Backups: keep the newest `retention.backups_keep` (default 10). Delete older ones after a successful apply, never during. Always keep the backup of any run that is `applied_with_issues` or `deferred`.
- Logs: section 11.
- Runs: keep forever. They are small. `preview_actions` rows for runs older than 90 days are deleted; the summary remains.

---

## 14. Platform matrix

| Concern | Linux | macOS | Windows |
|---|---|---|---|
| digiKam process probe | `/proc` scan (existing) plus `psutil` | `psutil` | `psutil` |
| Manual "Close digiKam" | SIGTERM (existing) | `psutil.terminate()` | `psutil.terminate()` |
| Autostart | systemd user unit | launchd agent | Run key |
| Shortcut | `.desktop` in `~/.local/share/applications` | `Face Sync.app` bundle in `~/Applications` | Start Menu `.lnk` |
| Notifications | `notify-send` | `osascript` | PowerShell toast |
| Config directory | `~/.config/digikam-memories-sync` | `~/Library/Application Support/…` | `%APPDATA%\…` |

`psutil` becomes a hard dependency of the `desktop` extra. `watchdog` is removed from the extras: polling an mtime once a minute is enough and has no platform quirks. `keyring` stays optional with the existing file fallback.

---

## 15. Testing strategy

- **Coordinator** is tested as a pure state machine with a fake clock and fake probes: quiet period coalescing, the 60-minute cap, backoff schedule, pause at a safe point, manual jump-the-queue, supersede rules.
- **Deferral**: fake process probe flips to "running" between two digiKam actions; assert the transaction boundary, the `deferred` status, and resumption after the settle period.
- **Resume**: kill the preview after phase 2 batch N and assert phase 1 is redone, phase 2 continues at N+1, and the final `result_json` equals the uninterrupted one. Same for apply, already covered by phase 1 tests.
- **Sleep**: monotonic jump injected; assert the connection gate is re-probed before the next HTTP call.
- **Ledger**: table-driven tests for the five attribution rows, including `person_names_match` near-misses, and the baseline run writing rows for agreeing pairs only.
- **Conflict identity**: same face across two runs yields one open conflict; externally fixed face closes it.
- **Fingerprints**: digiKam fingerprint changes on rect edit, on rename, on new region; unchanged on unrelated metadata edits. Companion checksum tested in PHPUnit with a seeded table.
- **Service**: lock exclusivity, `service.json` lifecycle, launcher start-then-open with a fake browser.
- **Smoke**: one end-to-end run against the test fixtures through `face-sync service --once`, a flag that runs one coordinator cycle and exits, used by CI.

---

## 16. Milestones

Each milestone is shippable on its own and leaves phase 1 behaviour intact.

**M1. Service foundation.** `service.py`, lock, `service.json`, `face-sync ui` launcher, autostart install for the three platforms, shortcuts, file and SQLite logging, Logs page. No change to sync behaviour.
Done when: a fresh login starts the service; the desktop shortcut opens the UI; the Logs page shows a manual run.

**M2. Home, Settings, Activity.** New shell and rail. Setup only when needed. Sync now runs the existing job. Run detail folds the phase 1 screens. Settings page with the version 2 schema and migration. Pause and resume flags exist but only gate manual runs. Browser notifications and the title badge, including the channel choice in `/api/status`, since both are page code plus one status field.
Done when: a returning user lands on Home in the "in sync" or "ready to apply" state and can reach every phase 1 screen from Activity, and a conflict raised while the window is in the background produces one notification that opens the right screen.

**M3. Ledger and conflicts.** Attribution rule in the engine, ledger writes for agreeing pairs, baseline run, conflict identity and cross-run deduplication, Needs attention inbox, plan split, `decisions` follow-up runs.
Done when: renaming a person in Memories produces automatic digiKam reassignments and zero conflicts on the next manual run; the same conflict never appears twice in Needs attention.

**M4. Resumable runs and the coordinator.** Preview checkpoints and `preview_actions`, `recover_unfinished_runs`, coordinator thread with gates, digiKam deferral including mid-apply detection, backoff, sleep detection.
Done when: killing the service mid-preview and mid-apply both resume correctly; opening digiKam mid-apply defers cleanly and closing it completes the run.

**M5. Triggers.** digiKam fingerprint and mtime pre-check, companion `changes` and `status` endpoints and client, Recognize gate, quiet period, fallback interval, automation toggle wired end to end, first-run automation step.
Done when: a face renamed in digiKam is reflected in Memories within quiet period + one check interval of closing digiKam, with no clicks.

**M6. Polish.** Operating-system notifications per platform for the UI-closed case, retention jobs, run and log tail in run detail, `--once` smoke flag, README and architecture doc updates, HTML prototype replaced by screenshots.

---

## 17. Risks and open points

- **Ledger bootstrap.** The first phase 2 run after upgrade will surface every current disagreement as a conflict, once. This is expected and should be said on Home the first time: "Face Sync needs one round of decisions before it can tell which library changed."
- **Fingerprint checksum collisions** on the Memories side are theoretically possible. The fallback interval bounds the damage to one day.
- **Recognize busy detection** depends on `oc_jobs.reserved_at` semantics, which are stable but undocumented. If Nextcloud changes them the gate degrades to "idle", which is the phase 1 behaviour.
- **digiKam on another machine** writing to a shared database is invisible to the process probe. The write-lock probe catches an active transaction only. Documented, not solved.
- **Preview cost per trigger.** A full preview on this library takes minutes. Person-scoped automatic runs, driven by which person's faces changed, would be a phase 3 optimisation once the fingerprints record per-person deltas.
- **Windows service semantics.** A Run-key process dies at logoff, so a sync in progress stops and resumes at next login. Acceptable; a real Windows service is out of scope.
