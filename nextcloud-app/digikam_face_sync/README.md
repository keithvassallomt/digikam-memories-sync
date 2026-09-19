# digiKam Face Sync for Nextcloud

This is a separate Nextcloud app. It lives beside Recognize in Nextcloud's app
directory. Updating Recognize does not remove or overwrite it.

The app adds these authenticated endpoints:

```text
GET  /index.php/apps/digikam_face_sync/api/v1/face-import
POST /index.php/apps/digikam_face_sync/api/v1/face-import
POST /index.php/apps/digikam_face_sync/api/v1/face-assign
GET  /index.php/apps/digikam_face_sync/api/v1/people
GET  /index.php/apps/digikam_face_sync/api/v1/faces
```

It supports Nextcloud 33 to 35 and Recognize 12.x and 13.x. The app talks to
Recognize rather than to Nextcloud, and Recognize's tables have not moved
across Nextcloud releases, which is what makes that range safe. A Recognize update
needs no reinstall of this app. After a future Recognize major update, this app
remains installed but pauses imports until its compatibility is checked and its
version support is updated.

The face-list endpoints return only the signed-in user's accessible files and
use cursor pagination. The POST request verifies file access, rejects overlapping conflicting faces,
generates a real descriptor with Recognize's locally installed model, and then
writes the detection into Recognize's existing tables. Retrying an identical
request returns the existing detection. A face explicitly confirmed in the
desktop review screen can bypass Recognize's automatic detector; the app still
uses Recognize's landmark alignment and recognition model to create its descriptor.

The assignment endpoint verifies that both the image and detection belong to
the signed-in user before assigning an existing detection to a named person.
It also handles unclustered detections, which have no WebDAV source folder.

## Install

Build the archive from inside this repository:

```bash
./build-release.sh
```

The parent workspace also provides `./build-nextcloud-app.sh` as a shortcut.

Extract the resulting archive into a configured Nextcloud app directory so its
folder is named `digikam_face_sync`, then enable it from the Nextcloud root:

```bash
php occ app:enable digikam_face_sync
```

Recognize must already be installed, enabled, and working. The app has no
database migration and installs no additional model or Node packages.

Check it with an app password:

```bash
curl --user 'USER:APP_PASSWORD' \
  -H 'OCS-APIRequest: true' \
  https://cloud.example.com/index.php/apps/digikam_face_sync/api/v1/face-import
```

A healthy response contains `"createFaceDetection":true`.

## Update behaviour

- Updating Recognize leaves this app and its settings in place.
- Updating Nextcloud or Recognize beyond the versions listed above may disable
  imports, but does not remove the app or any existing face data.
- Updating this app means replacing only the `digikam_face_sync` folder and
  enabling it again if Nextcloud disabled it during the update.
