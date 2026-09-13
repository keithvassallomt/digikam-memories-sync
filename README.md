# digiKam ↔ Memories Face Sync

This project reconciles face names and rectangles between a local digiKam
library and Nextcloud Memories/Recognize.

The first public release will support changes in both directions. The guided
desktop UI now previews digiKam → Memories and Memories → digiKam changes. The
conflict review, safe Apply flow and sync ledger remain before the first usable
release.

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

The current interface implements first-run setup, All/One-person selection and
a read-only two-way preview. Preview runs and notifications are persisted in
the application state database. The conflict review and Apply flow are still
disabled.
