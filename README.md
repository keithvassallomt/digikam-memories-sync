# digiKam ↔ Memories Face Sync

This project reconciles face names and rectangles between a local digiKam
library and Nextcloud Memories/Recognize.

The guided desktop UI previews and applies changes in both directions. It
provides photo-based conflict review, creates a consistent digiKam database
backup before local writes, and records each operation so an interrupted Apply
can resume without repeating completed changes.

## Repository layout

```text
digikam_nextcloud/             Python sync engine, service and HTTP client
  web/                         Browser interface: index.html, css/, js/
  web/js/views/                One module per screen
tests/                         Python tests
nextcloud-app/
  digikam_face_sync/           Separately installable Nextcloud app
docs/
  architecture.md              What the parts are and how they fit
  install-nextcloud-app.md     Installing the companion app
  prototypes/                  UI prototypes, phase 1 and phase 2
phase2.md                      Phase 2 design: automatic operation
backlog.md                     What is next, and what was deferred
sync_faces.py                  Current command-line entry point
config.example.yaml            Development configuration example
```

The Nextcloud app is separate from Recognize, so a Recognize update does not
overwrite it. Build its installable archive with:

```bash
./build-nextcloud-app.sh
```

The HTTP client checks the server during connection:

1. Recognize must be installed and enabled. A missing Recognize DAV collection
   is an error.
2. The digiKam Face Sync app must expose its face-import capability. When it is
   missing, the UI will offer the configured installation page. The temporary
   page is `https://keithvassallo.com`.

## Development

```bash
python -m pip install -e .
python -m unittest discover -s tests -v
```

## Automatic operation

Face Sync can keep both libraries in step without being asked. Turn it on in
Settings, or at the end of first-run setup.

It watches digiKam's database file and polls Nextcloud every few minutes. A
change starts a wait rather than a sync, so twenty renames produce one sync
afterwards instead of twenty while you work. The wait is capped an hour from
the first change, so a busy library still syncs hourly.

It waits for Recognize to finish its own work, and holds digiKam's half of a
sync until you quit digiKam. Opening digiKam part way through stops the writes
within a second and the rest waits.

Face Sync remembers the name both libraries last agreed on for each face, so a
rename in either one is applied to the other rather than queued as a question.
Only a face renamed on both sides, or one with no history, waits for you.

## Commands

```bash
face-sync                          # open the interface, starting the service if needed
face-sync ui --foreground          # run the service in this terminal instead
face-sync service                  # run the background service
face-sync run --config config.yaml # one-off command-line sync
face-sync autostart enable         # start Face Sync at login
face-sync shortcuts install        # add it to the application menu
face-sync service --once           # start up, do one pass, exit (build check)
```

One service owns a configuration directory. It holds `service.lock` so a second
copy cannot start, and publishes its port and session token in `service.json`
so the launcher and shortcuts can find it. Starting a second service prints the
address of the running one and exits with status 3.

The service logs to `logs/face-sync.log` under the configuration directory and
to the state database, so the interface can show logs without reading files.

## The interface

The browser interface is plain ES modules, no framework and no build step. The
shell in `index.html` holds the rail; `js/main.js` routes between screens by
hash and mounts one view at a time. Each screen is a module under `js/views/`
that returns `{ element, enter, leave, update }`.

Shared pieces sit beside them: `api.js` is the only place that talks to the
service, `store.js` polls the status once for every screen, `copy.js` holds
every sentence the interface says, and `crop.js` carries the face-box geometry
the two review screens share.

Settings are versioned. A version 1 file is upgraded in place on first load,
with automatic sync left off so upgrading never starts changing libraries on
its own.

Command-line runs are previews unless `--apply` is supplied. The command line
has no ledger, so it treats every disagreement as a conflict, which is the
behaviour it has always had.

## The Nextcloud companion app

`nextcloud-app/digikam_face_sync` is installed separately from Recognize, so a
Recognize update cannot overwrite it. Build its archive with
`./build-nextcloud-app.sh`, and see `docs/install-nextcloud-app.md` for
installing it.

Version 0.5.0 adds two read-only endpoints Face Sync polls between syncs: a
fingerprint of which person each named face belongs to, and whether Recognize
is busy. An older version simply leaves change detection off, and Face Sync
falls back to its daily check.

The interface implements first-run setup, All/One-person selection, a read-only
two-way preview, conflict decisions and an explicit Apply step. Close digiKam
before applying local changes; the UI checks this on Linux and always requires
confirmation. If digiKam remains open without a visible window, the Apply page
can ask it to close and waits until it releases the database. Preview runs,
notifications, conflict decisions, operation
results and face links are persisted in the application state database.
If a model rejects an individual face, the result screen shows a zoomed review
with its source name and rectangle. The user can adjust the rectangle for a
targeted retry or keep the face in its source library; remembered one-sided
faces are suppressed in later previews unless their source box changes.

Backups are stored under the application's configuration directory in
`backups/`. On Linux this is normally
`~/.config/digikam-memories-sync/backups/`.
