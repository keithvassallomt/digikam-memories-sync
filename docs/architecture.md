# Application architecture

DigiMem keeps face names and rectangles in step between a local digiKam
library and Nextcloud Memories/Recognize, in both directions, on its own.

The phase 2 design document, `phase2.md`, carries the reasoning behind
automatic operation. This file describes what the parts are and how they fit.

## Repository layout

```text
digimem/                       Python sync engine, service and HTTP client
  web/                         Browser interface: index.html, css/, js/
  web/js/views/                One module per screen
tests/                         Python tests
nextcloud-app/
  digikam_face_sync/           Separately installable Nextcloud app
docs/                          User documentation, and this file
phase2.md                      Phase 2 design: automatic operation
backlog.md                     What is next, and what was deferred
release_template.md            Release notes around the changelog entry
sync_faces.py                  Current command-line entry point
config.example.yaml            Development configuration example
```

## Components

- `digimem`: the engine. Matching, the digiKam reader and writer, and
  the Nextcloud client. Independent of any interface.
- `service.py`: the long-running process. One per configuration directory, held
  by an exclusive lock and published in `service.json`.
- `coordinator.py`: decides what may run and when. Gates, triggers, resuming
  interrupted work, housekeeping.
- `triggers.py`: notices that a library changed and waits for the changes to
  stop before asking for a sync.
- `ledger.py`: remembers the name both libraries last agreed on for each face,
  which is what lets a rename be applied rather than questioned.
- `checkpoint.py`: writes a long run's findings and position down as it goes.
- The browser interface under `web/`: plain ES modules, one per screen.
- `nextcloud-app/digikam_face_sync`: the companion Nextcloud app. Creates
  detections Recognize is missing, reads named faces, and reports a change
  fingerprint and whether Recognize is busy.
- The state database: runs, proposed changes, conflicts, the face ledger,
  notifications, logs and service state.

## Modes

| Command | What it is |
|---|---|
| `digimem service` | The background process |
| `digimem ui` | Opens the interface, starting the service if needed |
| `digimem run` | One-off command-line sync |
| `digimem autostart` | Start at login, on all three platforms |
| `digimem shortcuts` | Application-menu entry |

Closing the browser does not stop anything. `digimem service --once` starts
up, does a single pass of work and exits, which is what a build check runs.

## The service

One service owns a configuration directory. It holds `service.lock` so a second
copy cannot start, and publishes its port and session token in `service.json`
so the launcher and shortcuts can find it. Starting a second service prints the
address of the running one and exits with status 3.

It logs to `logs/digimem.log` under the configuration directory and to the
state database, so the interface can show logs without reading files.

## The interface

Plain ES modules, no framework and no build step. The shell in `index.html`
holds the rail; `js/main.js` routes between screens by hash and mounts one view
at a time. Each screen is a module under `js/views/` that returns
`{ element, enter, leave, update }`.

Shared pieces sit beside them: `api.js` is the only place that talks to the
service, `store.js` polls the status once for every screen, `copy.js` holds
every sentence the interface says, and `crop.js` carries the face-box geometry
the review screens share.

Settings are versioned. A version 1 file is upgraded in place on first load,
with automatic sync left off, so upgrading never starts changing libraries on
its own.

Command-line runs are previews unless `--apply` is supplied. The command line
has no ledger, so it treats every disagreement as a conflict, which is the
behaviour it has always had.

## How a sync happens

1. **Something changes.** digiKam is fingerprinted locally behind a
   modification-time check; Memories is polled through the companion app. A
   change starts a wait rather than a sync, and further changes push the wait
   out, capped an hour from the first change.
2. **The gates are checked.** Paused, automation off, Recognize busy, a job
   already running, a connection backoff still counting down.
3. **A preview runs.** Both libraries are read and compared. Nothing is
   written. Each batch records what it found and how far it read, in one
   transaction, so an interruption costs only the remaining work.
4. **Disagreements are attributed.** The ledger says which library changed, so
   a rename on one side becomes a change to the other. Only a face that moved
   on both sides, or one with no history, becomes a question for a person.
5. **The changes are applied**, if automatic apply is on. Memories first.
   digiKam's half waits until digiKam is closed.
6. **Anything left is put in the inbox**: names that disagree, and faces
   Recognize rejected.

## Writing safely

- **Frozen plan.** Apply executes the list the preview saved, never a fresh
  comparison. A run's plan freezes once its journal exists; a decision made
  after that is carried by a follow-up run.
- **Verify before write.** Every action re-reads its target and refuses if the
  name, rectangle or file id has moved since the preview.
- **Backup first.** A consistent SQLite copy is made immediately before the
  first digiKam write that actually happens, using the backup API so committed
  write-ahead content is included.
- **Never behind digiKam's back.** digiKam-side writes happen only while
  digiKam is closed. Its opening stops them within a second. Each write also
  checks for a database lock, which catches a digiKam running elsewhere.
- **Idempotent journal.** Completed actions are recorded, so an interrupted
  apply resumes with what is still pending and repeats nothing.

## Notifications

Every event is recorded in the inbox first. Delivery beyond that uses one
channel and only one:

| Situation | Channel |
|---|---|
| Window open and focused | none; the screen updates instead |
| Window open, not focused, permission granted | the page raises it |
| Window open, permission refused | the operating system |
| Window closed | the operating system |

The tab title carries the count regardless, which needs no permission.

## Deliberate limits

- Deletions are not propagated. A missing face is reported, never removed.
- Rectangle drift, where both sides agree on the name but not the box, is
  detected and not synced.
- One digiKam library and one Nextcloud account.
- Clicking an operating-system notification does not open a particular screen.
  No platform offers that without more machinery than it is worth, and the
  browser channel covers the case where it matters.

## Working on it

```bash
python -m pip install -e '.[desktop]'
python -m unittest discover -s tests -v
```

The `desktop` extra carries `psutil`, which is what lets macOS and Windows tell
whether digiKam is open. Without it those platforms cannot tell, and an
automatic run will not wait for digiKam. Linux reads `/proc` and needs nothing.

`just` lists the other recipes: running the service in the foreground,
regenerating the icons, and cutting a release.

Build the companion app's installable archive with `./build-nextcloud-app.sh`,
which writes it into `dist/`.
