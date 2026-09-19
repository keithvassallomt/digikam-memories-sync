#!/usr/bin/env bash
set -euo pipefail

app_root=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
app_id=$(basename "$app_root")
version=$(python -c "import xml.etree.ElementTree as ET; print(ET.parse('$app_root/appinfo/info.xml').getroot().findtext('version'))")
output_dir=${1:-"$(dirname "$app_root")/dist"}
archive="$output_dir/${app_id}-${version}.tar.gz"

mkdir -p "$output_dir"

# Build the archive deterministically, so the same source always produces the
# same bytes. Without this, file order comes from however the directory happens
# to be read and timestamps come from whenever the files were checked out, so
# two clones of one commit produce two different archives and nobody can verify
# that a published release matches its source.
#
# The timestamp is the commit being built, falling back to the epoch outside a
# git checkout. Deliberately not "the last commit to touch the app": limiting
# the log by path needs history to simplify against, and CI checks out a single
# commit, so the same tag gave one timestamp on a full clone and another on a
# shallow one. HEAD is the same in both.
epoch=${SOURCE_DATE_EPOCH:-}
if [ -z "$epoch" ]; then
	epoch=$(git -C "$app_root" log -1 --format=%ct 2>/dev/null || true)
fi
epoch=${epoch:-0}

tar --create --gzip --file "$archive" \
	--directory="$(dirname "$app_root")" \
	--sort=name \
	--format=gnu \
	--mtime="@$epoch" \
	--owner=0 --group=0 --numeric-owner \
	--exclude="$app_id/.git" \
	--exclude="$app_id/.gitignore" \
	--exclude="$app_id/vendor" \
	--exclude="$app_id/dist" \
	--exclude="$app_id/build-release.sh" \
	"$app_id"

echo "$archive"
