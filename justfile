set shell := ["bash", "-uc"]

config := env("XDG_CONFIG_HOME", env("HOME") / ".config") / "digimem"
service := config / "service.json"

# List what there is to run.
default:
    @just --list

# Run the service in the foreground. Ctrl-C stops it.
dev *args:
    @just stop
    python3 -m digimem service {{ args }}

# Stop a service running in the background, and wait for it to let go.
stop:
    #!/usr/bin/env bash
    # The lock outlives the signal by a moment, so starting again too quickly
    # is refused with "DigiMem is already running". Waiting here is what makes
    # `just dev` safe to run twice in a row.
    set -uo pipefail
    [[ -f "{{ service }}" ]] || exit 0
    pid=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["pid"])' "{{ service }}" 2>/dev/null) || exit 0
    kill -0 "$pid" 2>/dev/null || exit 0
    echo "stopping DigiMem ($pid)…"
    kill "$pid"
    for _ in {1..40}; do
        kill -0 "$pid" 2>/dev/null || exit 0
        sleep 0.5
    done
    echo "it did not stop" >&2
    exit 1

# Regenerate every icon from assets/logo.png. Needs Pillow.
icons:
    python3 assets/make-icons.py

# Run the test suite.
test:
    python3 -m unittest discover -s tests

# Cut a release: bump the version, tag it, and let CI build and publish.
release:
    #!/usr/bin/env bash
    set -euo pipefail

    cd "$(git rev-parse --show-toplevel)"

    say() { printf '\n\033[1m%s\033[0m\n' "$*"; }
    die() { printf '\n\033[31m%s\033[0m\n' "$*" >&2; exit 1; }

    # ---------------------------------------------------- refuse a bad start
    [[ -z "$(git status --porcelain)" ]] \
        || die "Working tree is not clean. Commit or stash first."
    branch=$(git rev-parse --abbrev-ref HEAD)
    [[ "$branch" == "main" ]] || die "On '$branch'. Releases are cut from main."
    command -v gh >/dev/null || die "The GitHub CLI (gh) is needed to watch the build."

    current=$(python3 -c 'import digimem; print(digimem.__version__)')
    nc_current=$(python3 -c "import xml.etree.ElementTree as ET; print(ET.parse('nextcloud-app/digikam_face_sync/appinfo/info.xml').getroot().findtext('version'))")

    say "DigiMem is at $current   (Nextcloud app: $nc_current)"
    read -rp "New DigiMem version: " version
    [[ "$version" =~ ^[0-9]+\.[0-9]+\.[0-9]+([-+][0-9A-Za-z.-]+)?$ ]] \
        || die "'$version' is not a semantic version like 1.2.3."
    # The tag is the guard, not the version in the source. A first release is
    # cut from a tree that already carries the number it is being released as,
    # and refusing that would make the first release impossible.
    git rev-parse -q --verify "refs/tags/v$version" >/dev/null \
        && die "Tag v$version already exists."

    # ------------------------------------------ the changelog decides the rest
    say "Checking CHANGELOG.md"
    python3 scripts/changelog.py check "$version" \
        || die "Write the entry first. A release with no notes is worse than no release."

    # The companion app versions on its own schedule: its number is a
    # compatibility promise to the server, not a marketing one.
    nc_version="$nc_current"
    read -rp "Nextcloud app version [$nc_current, Enter to leave it]: " answer
    if [[ -n "$answer" && "$answer" != "$nc_current" ]]; then
        [[ "$answer" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || die "'$answer' is not a version."
        nc_version="$answer"
    fi

    say "About to release"
    printf '  DigiMem        %s  ->  %s\n' "$current" "$version"
    printf '  Nextcloud app  %s  ->  %s\n' "$nc_current" "$nc_version"
    printf '  Tag            v%s\n' "$version"
    read -rp $'\nGo ahead? [y/N] ' ok
    [[ "$ok" =~ ^[Yy]$ ]] || die "Nothing changed."

    # -------------------------------------------------------------- do it
    say "Setting the version"
    python3 scripts/set_version.py "$version" --nextcloud "$nc_version"

    say "Running the tests"
    python3 -m unittest discover -s tests 2>&1 | tail -3

    say "Committing and tagging"
    git add digimem/__init__.py nextcloud-app/digikam_face_sync/appinfo/info.xml
    git commit -m "Release v$version"
    git tag -a "v$version" -m "DigiMem v$version"
    git push origin main "v$version"

    say "Pushed v$version. CI is building the artefacts now."
    echo "Notarisation can take a while, so this does not wait."
    echo
    echo "  Watch:   gh run watch --exit-status"
    echo "  Release: https://github.com/keithvassallomt/digikam-memories-sync/releases/tag/v$version"
