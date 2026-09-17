#!/usr/bin/env bash
# Sign, notarise, staple and wrap DigiMem.app in a DMG.
#
# Signing is inner-out and never uses `codesign --deep`, which Apple deprecated
# and which does not reliably reach nested content or apply entitlements to it.
# Every Mach-O in the bundle is signed in its own right, deepest first, because
# a bundle is only as signed as the last thing inside it.
set -euo pipefail

APP=${1:?usage: package.sh <DigiMem.app> <version> <output dir>}
VERSION=${2:?missing version}
OUTDIR=${3:?missing output directory}

HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
ENTITLEMENTS="$HERE/entitlements.plist"
DMG="$OUTDIR/DigiMem-$VERSION.dmg"
VOLUME="DigiMem $VERSION"

mkdir -p "$OUTDIR"

sign_one() {
    codesign --force --timestamp --options runtime \
        --sign "$SIGN_IDENTITY" "$1" >/dev/null
}

if [[ -z ${SIGN_IDENTITY:-} ]]; then
    if [[ ${DIGIMEM_ALLOW_UNSIGNED:-} != "1" ]]; then
        echo "SIGN_IDENTITY is not set. Set DIGIMEM_ALLOW_UNSIGNED=1 to build" \
             "a DMG anyway — it will not open on anyone else's Mac." >&2
        exit 1
    fi
    echo "==> No signing identity; building an unsigned DMG"
else
    echo "==> Signing nested Mach-O binaries, deepest first"
    # Sorting by path depth is what makes this inner-out: a framework's
    # contents are signed before the framework, and everything before the app.
    while IFS= read -r binary; do
        sign_one "$binary"
    done < <(
        find "$APP/Contents" -type f \( -name '*.so' -o -name '*.dylib' \) |
            awk -F/ '{print NF"\t"$0}' | sort -rn | cut -f2-
    )

    echo "==> Signing nested frameworks and executables"
    while IFS= read -r nested; do
        [[ -e $nested ]] && sign_one "$nested"
    done < <(
        find "$APP/Contents" -name '*.framework' -maxdepth 3 -type d
        find "$APP/Contents/MacOS" -type f -perm -u+x
    )

    echo "==> Signing the bundle"
    codesign --force --timestamp --options runtime \
        --entitlements "$ENTITLEMENTS" \
        --sign "$SIGN_IDENTITY" "$APP"

    echo "==> Verifying"
    codesign --verify --deep --strict --verbose=2 "$APP"
fi

echo "==> Building $DMG"
staging=$(mktemp -d)
cp -R "$APP" "$staging/"
ln -s /Applications "$staging/Applications"
rm -f "$DMG"
hdiutil create -volname "$VOLUME" -srcfolder "$staging" \
    -ov -format UDZO "$DMG" >/dev/null
rm -rf "$staging"

if [[ -n ${SIGN_IDENTITY:-} ]]; then
    codesign --force --timestamp --sign "$SIGN_IDENTITY" "$DMG"

    echo "==> Notarising (Apple can take minutes, sometimes hours)"
    xcrun notarytool submit "$DMG" \
        --apple-id "$APPLE_ID" \
        --team-id "$APPLE_TEAM_ID" \
        --password "$APPLE_APP_PASSWORD" \
        --wait

    echo "==> Stapling"
    xcrun stapler staple "$DMG"
    xcrun stapler validate "$DMG"
    # Proves what a first-time user's Mac will actually check.
    spctl --assess --type open --context context:primary-signature -vv "$DMG"
fi

echo "$DMG"
