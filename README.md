# digiKam ↔ Memories Face Sync

This project reconciles face names and rectangles between a local digiKam
library and Nextcloud Memories/Recognize.

The guided desktop UI previews and applies changes in both directions. It
provides photo-based conflict review, creates a consistent digiKam database
backup before local writes, and records each operation so an interrupted Apply
can resume without repeating completed changes.

## Repository layout

```text
digikam_nextcloud/             Python sync engine and HTTP client
tests/                         Python tests
nextcloud-app/
  digikam_face_sync/           Separately installable Nextcloud app
docs/
  architecture.md              Product and service architecture
  prototypes/face-sync-ui.html Guided UI prototype
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
face-sync                         # guided local interface
face-sync run --config config.yaml # current command-line sync
```

Command-line runs are previews unless `--apply` is supplied. The desktop
release will not be marked usable until synchronization works in both
directions.

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
