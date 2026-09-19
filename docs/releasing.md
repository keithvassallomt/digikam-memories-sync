# Releasing DigiMem

This page is for whoever cuts a release. It is not needed to use DigiMem or to
work on it.

A release is one tag. Pushing `vX.Y.Z` builds every installer, signs and
notarises the macOS one, and publishes them all with notes rendered from the
changelog. Nothing is built on a developer machine, and nothing about a release
depends on which machine cut it.

## Cutting one

The changelog decides whether a release may happen at all, so it comes first.
Open `CHANGELOG.md` and turn the `## [Unreleased]` heading into the version
being released:

```markdown
## [Unreleased]

## [0.3.0] - 2026-10-01
```

Then add the two link definitions at the foot, following the ones already
there. `just release` refuses a version with no entry, or an entry with no
body, because a release nobody can read the notes for is worse than no release.

Then:

```bash
just release
```

It shows the current version, asks for the new one, checks the changelog, asks
separately about the Nextcloud companion app, runs the tests, commits, tags and
pushes. It does not wait for the build: notarisation alone can take a while.

The companion app has its own version on purpose. Its number is a compatibility
promise to a Nextcloud server, not a marketing one, so it moves when the app
changes and stays put when it does not.

## What the tag builds

| Artefact | Built on | Notes |
|---|---|---|
| `.deb`, `.rpm` | `ubuntu-latest` | No bundled Python. Architecture-independent, so one of each covers x86_64 and arm64. |
| `.AppImage` | `ubuntu:22.04` container | Bundles Python, so the glibc it is built against is the floor it imposes: 2.35. |
| `.dmg` | `macos-14` | Signed with the Developer ID, notarised by Apple and stapled. Apple Silicon only. |
| `.exe` | `windows-latest` | Unsigned, deliberately. SmartScreen warns; the release notes say so. |
| companion `.tar.gz` | `ubuntu-latest` | Reproducible — see below. |

The frozen builds are **onedir, never onefile**. A onefile bundle unpacks its
libraries to a temporary directory and loads them from there, so the binaries
actually loaded are not the ones that were signed, and macOS refuses them under
the hardened runtime. The app then signs, notarises, and dies on launch.

Only a tag publishes. A `workflow_dispatch` run builds and notarises everything
and stops, which is the way to rehearse the whole thing without creating a
release that has to be deleted afterwards.

## What CI needs

Four repository secrets, all for macOS:

| Secret | What it is |
|---|---|
| `MACOS_CERTIFICATE_P12` | The Developer ID certificate and its private key, exported as `.p12` and base64-encoded. |
| `MACOS_CERTIFICATE_PASSWORD` | The password set when exporting it. Must not be empty. |
| `APPLE_ID` | The Apple account used for notarisation. |
| `APPLE_APP_PASSWORD` | An app-specific password from appleid.apple.com, not the account password. |

The signing identity and Team ID are **not** secrets and sit in the workflow as
plain values: both are readable in any signed binary.

To replace the certificate, export it from Keychain Access under **My
Certificates** — it must have its private key under the disclosure triangle,
or it will import into CI and fail at signing — then:

```bash
base64 -w0 DeveloperID.p12 | gh secret set MACOS_CERTIFICATE_P12
```

## Publishing to the AUR

Arch is the one distribution that needs a step after the release, because the
AUR holds a recipe rather than a file. The recipe, `digimem-bin`, repackages
the published `.deb`: the launcher, the desktop entry and the icon are then the
same files every other Linux user gets, and nothing about the Arch package has
to be kept in step by hand. The `.deb` carries no interpreter and nothing
compiled, so the result is architecture-independent too.

Do it after the GitHub release exists, because the recipe carries the sha256 of
the asset it points at, and the asset has to be published before it can be
checksummed.

```bash
just aur           # build it and leave it somewhere to install, publishing nothing
just aur-publish   # render, validate, and push to the AUR
```

Both need an Arch host with `base-devel`, and `gh` to find the release asset.
Both build the package before publishing anything, because a broken PKGBUILD on
the AUR is public. The build runs with `--nodeps` so that it does not depend on
what is installed on the machine doing it; the dependency names are checked
against the repositories separately, which is the part that actually matters.

`just aur-publish` also needs an AUR account with your SSH key, and says what
to set up if it cannot connect. It signs the AUR commit with the same key this
project signs with, carried in from the project tree — a temporary checkout
sees only global git config, where a `signingkey` that resolves only under an
`includeIf` would abort the commit.

Edit `packaging/aur/PKGBUILD.in`, never a generated `PKGBUILD`: the version and
the checksum are filled in from the release each time.

## Publishing the companion app to the App Store

The desktop release is independent of this. Do it after the GitHub release
exists, because the store needs a public URL for the archive and a signature of
**the file at that URL**.

The signing key lives outside the repository, at
`~/.nextcloud/certificates/digikam_face_sync.key`, with its certificate beside
it. The key is the only proof of authorship of this app: losing it means
requesting a new certificate, and it must never be committed.

Download what was actually published, rather than signing a local build:

```bash
curl -fsSL -o /tmp/app.tar.gz \
  https://github.com/keithvassallomt/digikam-memories-sync/releases/download/vX.Y.Z/digikam_face_sync-A.B.C.tar.gz

openssl dgst -sha512 -sign ~/.nextcloud/certificates/digikam_face_sync.key /tmp/app.tar.gz \
  | openssl base64
```

Then fill in the form at
<https://apps.nextcloud.com/developer/apps/releases/new> with the download URL
and that signature, leaving the nightly box unticked. The certificate field is
only for registering an app the first time.

Check it before uploading, because the store validates on upload and refusing
there is a slow way to find out:

```bash
curl -fsSL https://apps.nextcloud.com/schema/apps/info.xsd -o /tmp/info.xsd
xmllint --noout --schema /tmp/info.xsd \
  nextcloud-app/digikam_face_sync/appinfo/info.xml
```

`<bugs>` is mandatory there, which is not obvious until an upload is rejected.

A published release does not reach every Nextcloud instantly. Servers read a
catalogue that is generated separately from the listing page, so the app can be
live on the store and not yet offered in anyone's **Apps** screen. Early
absence is not a failed upload.

## Checking a release

The companion app archive is reproducible: the same commit always produces the
same bytes, whether built from a full clone or a single checked-out commit.
That is what lets anyone confirm a published release matches its source.

```bash
git checkout vX.Y.Z
./build-nextcloud-app.sh
sha256sum dist/digikam_face_sync-*.tar.gz
```

That must equal the sha256 of the published asset. If it does not, either the
build has stopped being deterministic or the published file is not what the tag
says it is — and both are worth stopping for.

CI checks this on every push by building once in the shallow checkout it starts
with, deepening the history, and building again. Building twice in one checkout
is not the same test, and passed once while the archive was still impossible to
reproduce from the tag.
