# Installing DigiMem

DigiMem goes on the computer where digiKam lives, because it reads and writes
digiKam's own database. Everything on this page is done once.

Downloads for every platform are on the
[releases page](https://github.com/keithvassallomt/digikam-memories-sync/releases).
Take the newest release and pick the file for your system.

## Before you start

- Close digiKam, or be ready to close it later. DigiMem never writes to
  digiKam's database while digiKam is open, and will wait for you.
- Have your Nextcloud address and username to hand. You will also need an
  **app password**, which takes a minute to create — see
  [Connecting your libraries](#connecting-your-libraries) below.
- The [Nextcloud companion app](install-nextcloud-app.md) should ideally be
  installed already. If it is not, DigiMem will still install and run; it will
  tell you what is missing when you try to connect.

## Linux

### Debian, Ubuntu, Linux Mint

```bash
sudo apt install ./digimem_<version>_all.deb
```

### Fedora, RHEL, openSUSE

```bash
sudo dnf install ./digimem-<version>-1.noarch.rpm
```

### Arch Linux, Manjaro, EndeavourOS

DigiMem is in the AUR as
[`digimem-bin`](https://aur.archlinux.org/packages/digimem-bin), which
repackages the same `.deb`, so there is nothing to download by hand:

```bash
yay -S digimem-bin
```

All three packages use the Python your distribution already has, so they are
small and work on any architecture. They need Python 3.10 or later.

### Any distribution — AppImage

```bash
chmod +x DigiMem-<version>-x86_64.AppImage
./DigiMem-<version>-x86_64.AppImage
```

The AppImage carries its own Python and needs nothing installed. Keep it
somewhere permanent — your home folder is fine — because the application menu
entry points at wherever you put it.

### Adding DigiMem to your application menu

The `.deb` and `.rpm` add a menu entry themselves. With the AppImage, or if the
entry is missing, open DigiMem and use **Settings → Background service → Add to
application menu**. The same screen has **Start DigiMem when I log in**.

Both can also be done from a terminal, if you prefer:

```bash
digimem shortcuts install
digimem autostart enable
```

## macOS

Apple Silicon only — M1 and later. There is no Intel build.

1. Open `DigiMem-<version>-arm64.dmg`.
2. Drag **DigiMem** into your Applications folder.
3. Launch it from Applications or Spotlight.

## Windows

1. Run `DigiMem-<version>-Setup.exe`.
2. Windows SmartScreen will warn you that the publisher is unknown, because the
   installer is not signed. Choose **More info**, then **Run anyway**.
3. The installer offers a desktop shortcut and starting DigiMem when you log
   in. Both are optional, and both can be changed later in DigiMem's Settings.

DigiMem installs for the current user, so Windows does not ask for an
administrator password.

## What happens when you first open DigiMem

DigiMem is two things: a small background service that does the work, and a
window in your browser that shows you what it is doing.

Opening DigiMem starts the service if it is not already running, and opens the
window. The window is served from your own computer, is reachable only from
your own computer, and needs no internet connection of its own.

**Closing the window does not stop DigiMem.** The service keeps running and
keeps syncing. To see the window again, open DigiMem the same way you did the
first time.

## Connecting your libraries

The first time you open DigiMem it shows **Connect your photo libraries**. You
cannot skip this — nothing else works until both libraries are reachable.

### Your digiKam library

DigiMem needs the folder that holds `digikam4.db`. Press **Find it for me** and
it will look in the usual places; if it finds nothing, type the path yourself.
In digiKam you can see it under **Settings → Configure digiKam → Database**.

### Your Nextcloud account

- **Nextcloud address** — the address you use in a browser, for example
  `https://cloud.example.com`.
- **Username** — your Nextcloud username.
- **App password** — *not* your normal password. In Nextcloud, go to your
  profile picture → **Settings → Security**, scroll to **Devices & sessions**,
  type a name such as `DigiMem` and press **Create new app password**. Copy
  the password it shows you and paste it into DigiMem. Nextcloud will not show
  it again.

  An app password can be revoked from that same page later without changing
  your real password. DigiMem stores it in your system keychain, not in a file.
- **Photos folder in Nextcloud** — where your photos live in Nextcloud,
  usually `Photos`.

### Testing and finishing

**Test connection** checks both libraries without saving anything. It is the
quickest way to find a typo.

If Nextcloud answers but something on the server is missing, DigiMem says which
of the two it is:

- *Recognize is not installed in Nextcloud* — the Recognize app needs
  installing and enabling for your account before anything can work.
- *The digiKam Face Sync app needs installing or updating* — the
  [companion app](install-nextcloud-app.md) is missing or too old. There is a
  link to the installation page, and a **Check again** button for when it is
  done.

**Save and continue** saves the settings and moves to the last step, which
offers two choices:

- **Keep both libraries in sync automatically** — DigiMem watches both
  libraries and syncs when something changes, waiting for digiKam to be closed
  before it changes anything locally. Recommended.
- **Start DigiMem when I log in** — without this, syncing only happens while
  DigiMem is running.

**Finish** takes you to the Home screen, and DigiMem is set up. From here on,
see [Using DigiMem](using-digimem.md).

## Updating DigiMem

Install the new version over the old one. Your settings, the history of past
syncs and the remembered names are all kept, and settings files are quietly
brought up to date the first time the new version starts.

On Linux, install the new `.deb` or `.rpm` the same way you installed the first
one, update `digimem-bin` through your AUR helper, or replace the AppImage
file. On macOS, drag the new app over the old one. On Windows, run the new
installer.

If you use the Nextcloud companion app's newer features, update it too when its
version changes. DigiMem tells you on the connection screen when the server
side is too old for something it wants, and simply does without in the
meantime.

## Uninstalling

Remove DigiMem the way you remove any other application: your package manager
on Linux, dragging it to the Bin on macOS, Add or remove programs on Windows.

That leaves your settings, history and digiKam backups in place — see
[where DigiMem keeps things](index.md#where-digimem-keeps-things) — so that
reinstalling picks up where you left off. Delete that folder by hand if you
want DigiMem gone completely. Nothing in it is needed by digiKam or Nextcloud.

Your photos, your digiKam library and your Nextcloud are untouched by
uninstalling. Names DigiMem has already synced stay where they are, in both
libraries.
