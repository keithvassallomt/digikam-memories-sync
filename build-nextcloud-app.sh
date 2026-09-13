#!/usr/bin/env bash
set -euo pipefail

root=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
exec "$root/nextcloud-app/digikam_face_sync/build-release.sh" "$root/dist"
