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
    # A first release, or a re-run after a failed one, changes no version files
    # because they already say what is being released. There is nothing to
    # commit then, and git treats that as an error. The tag is the point.
    if git diff --cached --quiet; then
        echo "  the version files already say $version, so there is nothing to commit"
    else
        git commit -m "Release v$version"
    fi
    git tag -a "v$version" -m "DigiMem v$version"
    git push origin main "v$version"

    say "Pushed v$version. CI is building the artefacts now."
    echo "Notarisation can take a while, so this does not wait."
    echo
    echo "  Watch:   gh run watch --exit-status"
    echo "  Release: https://github.com/keithvassallomt/digikam-memories-sync/releases/tag/v$version"

# Build the AUR package and leave it somewhere to install. Publishes nothing.
aur output_dir="~/Downloads":
    #!/usr/bin/env bash
    set -euo pipefail
    cd "$(git rev-parse --show-toplevel)"

    output="{{ output_dir }}"
    output="${output/#\~/$HOME}"
    version=$(python3 -c 'import digimem; print(digimem.__version__)')

    staging=$(mktemp -d)
    trap 'rm -rf "$staging"' EXIT

    just _aur-stage "$staging"

    echo "==> Building it, which is what validates the PKGBUILD"
    (cd "$staging" && makepkg -f --noconfirm --nocheck --nodeps)

    dest="$output/digimem-bin-$version"
    mkdir -p "$dest"
    cp "$staging/PKGBUILD" "$staging/.SRCINFO" "$staging"/*.pkg.tar.zst "$dest/"

    echo
    echo "digimem-bin $version is in $dest:"
    ls -1 "$dest"
    echo
    echo "  Install it:  sudo pacman -U $dest/*.pkg.tar.zst"
    echo "  Publish it:  just aur-publish"

# Push the current version to the AUR. Needs an AUR account with your SSH key.
aur-publish:
    #!/usr/bin/env bash
    set -euo pipefail
    cd "$(git rev-parse --show-toplevel)"

    version=$(python3 -c 'import digimem; print(digimem.__version__)')
    repo="ssh://aur@aur.archlinux.org/digimem-bin.git"

    staging=$(mktemp -d)
    checkout=$(mktemp -d)
    trap 'rm -rf "$staging" "$checkout"' EXIT

    # Resolve the signing identity here, in the project tree, where a
    # per-directory includeIf still applies. The AUR checkout below is a
    # temporary directory that sees only global config, so what it would
    # resolve there may not be the key you actually sign with -- or may not
    # resolve at all, which aborts the commit rather than skipping the
    # signature.
    sign_format=$(git config --get gpg.format || echo "")
    sign_key=$(git config --get user.signingkey || echo "")
    sign_on=$(git config --get commit.gpgsign || echo "false")

    just _aur-stage "$staging"

    # A broken PKGBUILD on the AUR is public, so prove it builds first.
    echo "==> Validating with makepkg"
    (cd "$staging" && makepkg -f --noconfirm --nocheck --nodeps >/dev/null)

    echo "==> Cloning $repo"
    if ! git clone --quiet "$repo" "$checkout" 2>/dev/null; then
        echo >&2
        echo "Could not reach the AUR over SSH. One-time setup:" >&2
        echo "  1. Create an account at https://aur.archlinux.org/register" >&2
        echo "  2. Add ~/.ssh/id_ed25519.pub under My Account -> SSH Public Key" >&2
        echo "  3. Put this in ~/.ssh/config:" >&2
        echo "       Host aur.archlinux.org" >&2
        echo "         User aur" >&2
        echo "         IdentityFile ~/.ssh/id_ed25519" >&2
        exit 1
    fi

    cp "$staging/PKGBUILD" "$staging/.SRCINFO" "$checkout/"
    cd "$checkout"
    # Stage first, then ask what changed. On the very first push the AUR
    # repository is empty: both files are untracked and there is no HEAD to
    # diff against, so asking git diff would report nothing and skip the push.
    git add PKGBUILD .SRCINFO
    if [[ -z "$(git status --porcelain)" ]]; then
        echo "==> The AUR is already at $version. Nothing to push."
        exit 0
    fi

    if [[ "$sign_on" == "true" && -n "$sign_key" ]]; then
        git -c commit.gpgsign=true \
            -c gpg.format="$sign_format" \
            -c user.signingkey="$sign_key" \
            commit --quiet -m "digimem-bin $version"
    else
        git commit --quiet -m "digimem-bin $version"
    fi
    # The AUR's branch is master. A clone of an empty repository takes its
    # branch name from init.defaultBranch locally, which may well be main, so
    # name the remote ref rather than trusting what the local branch is called.
    git push --quiet origin HEAD:refs/heads/master

    echo
    echo "==> Published digimem-bin $version"
    echo "    https://aur.archlinux.org/packages/digimem-bin"

# Internal: render PKGBUILD and .SRCINFO for the current version into a directory.
_aur-stage staging:
    #!/usr/bin/env bash
    set -euo pipefail
    cd "$(git rev-parse --show-toplevel)"
    staging="{{ staging }}"

    [[ "$(uname -s)" == "Linux" ]] && command -v makepkg >/dev/null \
        || { echo "The AUR recipes need an Arch host with base-devel." >&2; exit 1; }
    command -v gh >/dev/null \
        || { echo "The GitHub CLI (gh) is needed to find the release asset." >&2; exit 1; }

    version=$(python3 -c 'import digimem; print(digimem.__version__)')
    tag="v$version"
    deb="digimem_${version}_all.deb"
    url="https://github.com/keithvassallomt/digikam-memories-sync/releases/download/$tag/$deb"

    # The PKGBUILD points at the release asset and carries its checksum, so the
    # asset has to exist first. Stopping here beats emitting a broken PKGBUILD.
    echo "==> Looking for $deb on $tag"
    if ! gh release view "$tag" --json assets --jq '.assets[].name' 2>/dev/null | grep -qx "$deb"; then
        echo "$deb is not attached to release $tag." >&2
        echo "Cut the release first and let CI finish uploading." >&2
        exit 1
    fi

    echo "==> Downloading it to checksum it"
    curl -sSfL -o "$staging/$deb" "$url"
    sha256=$(sha256sum "$staging/$deb" | cut -d' ' -f1)
    echo "    sha256: $sha256"

    sed -e "s/@PKGVER@/$version/g" -e "s/@SHA256@/$sha256/g" \
        packaging/aur/PKGBUILD.in > "$staging/PKGBUILD"

    # makepkg reuses the deb just downloaded rather than fetching it again.
    (cd "$staging" && makepkg --printsrcinfo > .SRCINFO)

    # The build runs with --nodeps, so that validating the recipe does not turn
    # on what happens to be installed on this machine. That skips the one check
    # worth keeping, so make it here instead: a dependency named for another
    # distribution stays invisible until somebody tries to install the package.
    echo "==> Checking the dependency names against the repositories"
    missing=()
    while read -r name; do
        pacman -Si "$name" >/dev/null 2>&1 || missing+=("$name")
    done < <(awk -F' = ' '/^\t(opt)?depends = /{split($2, d, ":"); print d[1]}' "$staging/.SRCINFO")
    if (( ${#missing[@]} > 0 )); then
        echo "Not in any repository: ${missing[*]}" >&2
        exit 1
    fi

    echo "==> Rendered PKGBUILD and .SRCINFO for $version"
