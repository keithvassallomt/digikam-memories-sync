# Installing the Nextcloud companion app

DigiMem needs one small app on your Nextcloud server, called **digiKam Face
Sync**. This page is for whoever administers that server. If that is not you,
send them this page; it is the only thing they need to do.

## What it is, and why it is needed

Recognize finds faces in your photos and can be told who they are. What it
cannot do on its own is accept a face box that came from somewhere else —
which is exactly what syncing from digiKam means.

The companion app adds that missing piece. It lets DigiMem, using your own
account and an app password, hand Recognize a face box from digiKam, and read
back the faces Recognize already knows about. It also answers two cheap
questions DigiMem asks between syncs: *has anything changed since last time?*
and *is Recognize busy right now?* — so DigiMem can stay quiet while Recognize
works, and skip syncs where nothing has happened.

It is installed separately from Recognize on purpose, so that updating
Recognize cannot overwrite it.

Nothing it does touches your photo files. It reads your photos and Recognize's
face data, and adds the faces DigiMem sends it. It does not delete anyone's
photos, and it does not change anything for users who are not using DigiMem.

## What you need

- Administrator access to the Nextcloud server. Installing from the App Store
  needs nothing more than that; installing by hand also needs a way to put
  files in the apps directory.
- **Recognize** installed and enabled, and finished analysing the photo library.
  DigiMem cannot do anything useful until Recognize knows about the faces.
- **Memories** installed, since that is the interface the faces are seen in.
- PHP 8.2 or later.
- A Nextcloud version this build of the app supports. Each release says which
  versions it is for. Nextcloud refuses to enable an app outside that range and
  says so plainly, and the App Store simply does not offer it — either way you
  find out before anything is installed.

## Installing it

There are two ways. The first is a few clicks and needs no shell access, so
start there.

### Option 1: from the Nextcloud App Store

The app is published at
[apps.nextcloud.com/apps/digikam_face_sync](https://apps.nextcloud.com/apps/digikam_face_sync).
Nextcloud can fetch and enable it for you, which means no files to copy, no
ownership to set, and updates offered to you the same way any other app's are.

In the Nextcloud web interface, go to your profile picture → **Apps**, search
for *digiKam Face Sync*, and press **Download and enable**.

Or, from a terminal on the server:

```bash
sudo -u www-data php occ app:install digikam_face_sync
```

If searching turns up nothing, it is almost always that your Nextcloud version
falls outside the range this release supports — the listing says which versions
it is for. Nextcloud hides apps it cannot run rather than offering them and
failing afterwards. Use option 2 if you need a version that is not offered, or
wait for a release that covers yours.

### Option 2: by hand

Use this if the server has no access to the App Store, if you need a particular
version, or if you would rather build it yourself.

#### Get the archive

Download `digikam_face_sync-<version>.tar.gz` from the
[releases page](https://github.com/keithvassallomt/digikam-memories-sync/releases).
It is listed alongside the DigiMem downloads for the same release.

If you would rather build it from source, run `./build-nextcloud-app.sh` in a
clone of the repository. It writes the same archive into `dist/`.

#### Put it on the server

Nextcloud loads apps from an apps directory. Most installations have one or
both of these:

```
/var/www/html/apps
/var/www/html/custom_apps
```

If `custom_apps` exists, use it: that is the one meant for apps that did not
come with Nextcloud. Your own paths may differ — a snap or a distribution
package puts Nextcloud elsewhere — and your Nextcloud administration page under
**Settings → Administration → Overview** or your `config.php` will tell you
where.

Extract the archive so that a folder called `digikam_face_sync` ends up inside
the apps directory:

```bash
tar -xzf digikam_face_sync-<version>.tar.gz
sudo mv digikam_face_sync /var/www/html/custom_apps/
```

Then make sure the files belong to the user your web server runs as, or
Nextcloud will not be able to read them. On most Linux installations that user
is `www-data`:

```bash
sudo chown -R www-data:www-data /var/www/html/custom_apps/digikam_face_sync
```

**If Nextcloud runs in a container**, do the same thing from outside it: copy
the folder in with `docker cp` (or your platform's equivalent), then set the
ownership with a command run inside the container. Everything below works the
same way, with `occ` commands run inside the container.

#### Turn it on

In the Nextcloud web interface, go to your profile picture → **Apps**, then
**Disabled apps**. *digiKam Face Sync* will be listed there. Press **Enable**.

Or, from a terminal on the server:

```bash
sudo -u www-data php occ app:enable digikam_face_sync
```

Either way is fine. If the app does not appear in the list at all, Nextcloud
cannot see the files: check that the folder landed in a directory Nextcloud
actually uses, and that the ownership is right.

The app has no database tables of its own, so there is nothing else to run.

## Check it from DigiMem

The honest test is the one that matters: open DigiMem on your computer, go to
**Settings → Libraries → Nextcloud → Change** (or the setup screen, on a fresh
install) and press **Test connection**.

- *Both libraries are reachable* — done.
- *Recognize is not installed in Nextcloud* — Recognize is missing, or not
  enabled for the account DigiMem is signing in with.
- *The digiKam Face Sync app needs installing or updating* — Nextcloud is
  answering, but it is not serving this app, or it is serving an older copy
  than DigiMem needs. See below.

## Updating it

**Installed from the App Store**, there is nothing special to do. Nextcloud
offers the update under **Apps** like any other, or from a terminal:

```bash
sudo -u www-data php occ app:update digikam_face_sync
```

**Installed by hand**, replace the folder with the new version and enable it
again:

```bash
sudo rm -rf /var/www/html/custom_apps/digikam_face_sync
sudo tar -xzf digikam_face_sync-<version>.tar.gz -C /var/www/html/custom_apps/
sudo chown -R www-data:www-data /var/www/html/custom_apps/digikam_face_sync
sudo -u www-data php occ app:enable digikam_face_sync
```

Enabling an app that is already enabled is safe, and is how Nextcloud notices
the new version.

If you would rather keep the old copy until the new one is proven, rename it
instead of deleting it, and rename it back to roll the change back.

DigiMem copes with an older companion app by turning off the features that need
a newer one — it falls back to checking for changes on a timer rather than
being told about them. Nothing breaks, and nothing is lost.

## Removing it

**Installed from the App Store**, press **Remove** under **Apps**, or:

```bash
sudo -u www-data php occ app:remove digikam_face_sync
```

**Installed by hand**, disable it and delete the folder:

```bash
sudo -u www-data php occ app:disable digikam_face_sync
sudo rm -rf /var/www/html/custom_apps/digikam_face_sync
```

Faces DigiMem has already added stay in Recognize; they are ordinary Recognize
faces. DigiMem itself will report that it can no longer reach the server side
and will stop syncing until the app comes back.

## If something is not working

**The app is not in the Apps list.** Searching the App Store and finding
nothing usually means your Nextcloud version is outside the range this release
supports; Nextcloud hides apps it cannot run. After a hand install it means
Nextcloud is not looking at the directory you put it in, or cannot read it —
check `config.php` for the app directories Nextcloud is configured with, and
check ownership.

**Nextcloud refuses to enable it**, mentioning versions. This build of the app
does not support your Nextcloud version. Use the release that matches, or
upgrade Nextcloud.

**DigiMem still says the app is missing** after you enabled it. Nextcloud is
probably still serving a cached older copy: enable it again, and check that the
folder you replaced is the one Nextcloud is actually using — a second copy in
another apps directory will quietly win.

**Faces are being rejected** when DigiMem applies changes. That is usually
Recognize itself declining a face box rather than a problem with this app, and
DigiMem has a screen for working through them — see
[Using DigiMem](using-digimem.md#faces-that-could-not-be-added).

**For anything else**, Nextcloud's own log is the place to look:
**Settings → Administration → Logging**, or the log file named in your
`config.php`.
