# DigiMem v{{VERSION}}

{{CHANGELOG}}

## 📦 Installation

DigiMem needs two things installed: the desktop application, on the machine
where digiKam lives, and the companion app on your Nextcloud server. Neither
works without the other.

### Linux

**Debian, Ubuntu, Linux Mint**

```bash
sudo apt install ./digimem_{{VERSION}}_all.deb
```

**Fedora, RHEL, openSUSE**

```bash
sudo dnf install ./digimem-{{VERSION}}-1.noarch.rpm
```

Both packages use your distribution's own Python rather than bundling one, so
they are small and work on any architecture. They need Python 3.10 or later.

**Any distribution — AppImage**

```bash
chmod +x DigiMem-{{VERSION}}-x86_64.AppImage
./DigiMem-{{VERSION}}-x86_64.AppImage
```

The AppImage carries its own Python and needs nothing installed. Then add
DigiMem to your application menu and, if you want it, to your login items:

```bash
digimem shortcuts install
digimem autostart enable
```

### macOS

1. Open `DigiMem-{{VERSION}}-arm64.dmg` and drag **DigiMem** to Applications.
2. Launch it from Applications or Spotlight.

Apple Silicon only (M1 and later). There is no Intel build.

### Windows

1. Run `DigiMem-{{VERSION}}-Setup.exe`.
2. Windows SmartScreen will warn you that the publisher is unknown, because the
   installer is not code-signed. Choose **More info**, then **Run anyway**.
3. The installer offers a desktop shortcut and a login item; both are optional
   and can be changed later from Settings.

DigiMem installs for the current user by default, so no administrator prompt
appears.

### Nextcloud — the companion app

Required on every platform. `digikam_face_sync` is installed separately from
Recognize, so a Recognize update cannot overwrite it.

1. Download `digikam_face_sync-{{NEXTCLOUD_VERSION}}.tar.gz` from this release.
2. Extract it into your Nextcloud `apps/` (or `custom_apps/`) directory.
3. Enable it:
   ```bash
   sudo -u www-data php occ app:enable digikam_face_sync
   ```

It needs Nextcloud 33 to 35 and PHP 8.2 or later, with Recognize installed and
enabled. Full instructions, including permissions and troubleshooting, are in
[docs/install-nextcloud-app.md](docs/install-nextcloud-app.md).

## ⬆️ Upgrading

Install the new package over the old one; settings, the face ledger and the
operation history are kept. Settings files are upgraded in place on first
launch, with automatic sync left off, so an upgrade never starts changing your
libraries on its own.

Upgrade the Nextcloud companion app whenever its version changes — DigiMem
tells you on the connection screen if the server side is too old for a feature
it wants.

## 🔒 Privacy

All data stays on your machine. No telemetry or data collection.

## 📚 Resources

- **Getting started**: [README](README.md)
- **User guide**: [docs/index.md](docs/index.md)
- **How the parts fit together**: [docs/architecture.md](docs/architecture.md)
- **Installing the Nextcloud app**: [docs/install-nextcloud-app.md](docs/install-nextcloud-app.md)
- **Automatic operation, in detail**: [phase2.md](phase2.md)
- **Full history**: [CHANGELOG.md](CHANGELOG.md)
- **Issues**: [GitHub Issues](https://github.com/keithvassallomt/digikam-memories-sync/issues)
