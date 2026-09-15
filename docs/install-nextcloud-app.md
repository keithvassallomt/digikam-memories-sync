# Installing the Face Sync app in Nextcloud

For version 0.5.0, which adds change detection. Written for a Nextcloud running
in Docker inside an LXC container, reached by SSH to the container.

Nothing here touches your photos or the Recognize database. The two new
endpoints only read.

## 1. Build the archive

On the machine with this repository:

```bash
cd ~/LocalCode/keithvassallomt/digikam-memories-sync
./build-nextcloud-app.sh
```

It prints the path it wrote, which will be
`dist/digikam_face_sync-0.5.0.tar.gz`. The `dist/` directory is ignored by git,
so the archive is always built rather than committed.

## 2. Copy it to the LXC container

```bash
scp dist/digikam_face_sync-0.5.0.tar.gz YOUR_LXC:/tmp/
ssh YOUR_LXC
```

## 3. Find the Docker container and the apps directory

Do not assume either. Ask:

```bash
docker ps --format '{{.Names}}\t{{.Image}}' | grep -i nextcloud
```

Take the container name from the first column and use it below as `$NC`.

```bash
NC=nextcloud-app-1          # whatever the line above showed

# Where the existing 0.4.0 copy lives tells you the right directory.
docker exec "$NC" sh -c 'ls -d /var/www/html/*apps*/digikam_face_sync 2>/dev/null'
```

That prints something like `/var/www/html/custom_apps/digikam_face_sync`. The
parent of that path is your apps directory. Use it below as `$APPS`.

```bash
APPS=/var/www/html/custom_apps
```

If it prints nothing, the app is not installed yet. Use
`/var/www/html/custom_apps` if that directory exists, and `/var/www/html/apps`
otherwise.

## 4. Replace the app

```bash
cd /tmp
tar -xzf digikam_face_sync-0.5.0.tar.gz          # makes ./digikam_face_sync

# Keep the old one until the new one is proven.
docker exec "$NC" sh -c "mv $APPS/digikam_face_sync $APPS/digikam_face_sync.0.4.0" 2>/dev/null || true

docker cp digikam_face_sync "$NC:$APPS/digikam_face_sync"
docker exec "$NC" chown -R www-data:www-data "$APPS/digikam_face_sync"
```

## 5. Tell Nextcloud about it

```bash
docker exec -u www-data "$NC" php occ app:enable digikam_face_sync
docker exec -u www-data "$NC" php occ app:list | grep -A1 digikam
```

The version shown should be 0.5.0. Enabling an app that is already enabled is
safe and is how Nextcloud picks up the new version. This app has no database
migrations, so there is nothing else to run.

## 6. Check it from your workstation

```bash
NCURL=https://nc.vassallo.cloud
AUTH='keith:YOUR_APP_PASSWORD'

curl -s -u "$AUTH" -H 'OCS-APIRequest: true' -H 'Accept: application/json' \
  "$NCURL/index.php/apps/digikam_face_sync/api/v1/face-import"
```

Look for `"apiVersion":5`, `"changeFingerprint":true` and
`"recognizeStatus":true`. If those are false, Nextcloud is still serving the
old copy: check step 4 landed in the right directory.

Then the two new endpoints themselves:

```bash
curl -s -u "$AUTH" -H 'OCS-APIRequest: true' -H 'Accept: application/json' \
  "$NCURL/index.php/apps/digikam_face_sync/api/v1/changes"

curl -s -u "$AUTH" -H 'OCS-APIRequest: true' -H 'Accept: application/json' \
  "$NCURL/index.php/apps/digikam_face_sync/api/v1/status"
```

The first returns detection and cluster counts with two hashes. Rename a person
in Memories and call it again: the hashes must change. The second returns
`recognize_busy` and, when something is running, the job names.

## 7. Clean up

Once the checks pass:

```bash
docker exec "$NC" sh -c "rm -rf $APPS/digikam_face_sync.0.4.0"
rm -rf /tmp/digikam_face_sync /tmp/digikam_face_sync-0.5.0.tar.gz
```

## Rolling back

```bash
docker exec "$NC" sh -c "rm -rf $APPS/digikam_face_sync && mv $APPS/digikam_face_sync.0.4.0 $APPS/digikam_face_sync"
docker exec -u www-data "$NC" php occ app:enable digikam_face_sync
```

Face Sync detects the older app and turns change detection off by itself,
falling back to the daily check. Nothing breaks.

## Then, on the desktop side

```bash
cd ~/LocalCode/keithvassallomt/digikam-memories-sync
git checkout phase-2
python -m pip install -e .

# Stop any running service first, then:
face-sync service
```

In the interface, open Settings and turn Automatic sync on. Face Sync checks
Nextcloud every 5 minutes and your digiKam library whenever its file changes,
waits 10 minutes after the last change, and syncs. It waits for Recognize to
finish, and holds digiKam changes until you quit digiKam.

To see it working, watch the Logs page, or:

```bash
tail -f ~/.config/digikam-memories-sync/logs/face-sync.log
```

## If something looks wrong

Nextcloud's own log, filtered to this app:

```bash
docker exec -u www-data "$NC" php occ log:watch 2>/dev/null \
  || docker exec "$NC" tail -f /var/www/html/data/nextcloud.log
```
