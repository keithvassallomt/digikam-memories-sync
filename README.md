# digiKam ↔ Memories Face Sync

This project reconciles face names and rectangles between a local digiKam
library and Nextcloud Memories/Recognize.

The first public release will support changes in both directions. The existing
digiKam → Memories engine and the Nextcloud face-import endpoint are working;
Memories → digiKam synchronization and the guided desktop UI are the remaining
major slices.

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
python sync_faces.py --config config.yaml
```

Command-line runs are previews unless `--apply` is supplied. The desktop
release will not be marked usable until synchronization works in both
directions.
