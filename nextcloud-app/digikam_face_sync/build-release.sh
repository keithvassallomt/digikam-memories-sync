#!/usr/bin/env bash
set -euo pipefail

app_root=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
app_id=$(basename "$app_root")
version=$(python -c "import xml.etree.ElementTree as ET; print(ET.parse('$app_root/appinfo/info.xml').getroot().findtext('version'))")
output_dir=${1:-"$(dirname "$app_root")/dist"}
archive="$output_dir/${app_id}-${version}.tar.gz"

mkdir -p "$output_dir"
tar --create --gzip --file "$archive" \
	--directory="$(dirname "$app_root")" \
	--exclude="$app_id/.git" \
	--exclude="$app_id/vendor" \
	--exclude="$app_id/dist" \
	"$app_id"

echo "$archive"
