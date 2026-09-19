# DigiMem documentation

DigiMem keeps the faces in your digiKam library and the faces in Nextcloud
Memories in step with each other. Name someone in digiKam and the name appears
in Memories; name someone in Memories and it appears in digiKam. Face boxes
travel the same way, so a face one library has found and the other has not gets
added rather than lost.

It works in both directions, on its own, in the background. When the two
libraries genuinely disagree — the same face with a different name in each —
DigiMem stops and asks you, and keeps syncing everything else while it waits.

## What you need

- **digiKam** on your computer, with the face tags you already use.
- **A Nextcloud server** with the **Memories** and **Recognize** apps installed
  and enabled, and Recognize having finished looking through your photos.
- **The digiKam Face Sync companion app** on that Nextcloud server. It is a
  small add-on that lets DigiMem send face boxes to Recognize. Installing it
  needs Nextcloud administrator access.
- The same photos in both places. DigiMem matches photos between the two
  libraries by their content, so they do not have to be in the same folders,
  but a photo that only exists in one of them has nothing to sync with.

DigiMem runs on Linux, macOS (Apple Silicon) and Windows.

## The guides

1. **[Installing DigiMem](install-digimem.md)** — putting DigiMem on your
   computer and connecting it to your two libraries.
2. **[Installing the Nextcloud companion app](install-nextcloud-app.md)** — the
   server side, which you or your Nextcloud administrator does once.
3. **[Using DigiMem](using-digimem.md)** — every screen, every button, and what
   to do when DigiMem asks you something.

## Setting up for the first time

The order that causes the least confusion:

1. Install the companion app on Nextcloud, or ask whoever runs your Nextcloud
   to do it. See [Installing the Nextcloud companion app](install-nextcloud-app.md).
2. Install DigiMem on the computer where digiKam lives. See
   [Installing DigiMem](install-digimem.md).
3. Open DigiMem and work through the setup screen. It checks both libraries
   before it lets you finish, and tells you plainly if the server side is not
   ready yet.

You can do it in the other order. DigiMem will simply tell you the companion
app is missing, and offer a link to the installation page, until it is there.

## Where DigiMem keeps things

Everything DigiMem remembers lives in one folder on your computer: your
settings, the history of every sync, the names the two libraries have agreed
on, the logs, and the backups of digiKam's database.

| Platform | Folder |
|---|---|
| Linux | `~/.config/digimem/` |
| macOS | `~/Library/Application Support/digimem/` |
| Windows | `%APPDATA%\digimem\` |

Inside it, `backups/` holds copies of digiKam's database taken before DigiMem
changes anything locally, and `logs/` holds the log files. Your Nextcloud app
password is not kept there — it goes into your system keychain.

DigiMem never uploads anything about you anywhere. It talks to your digiKam
database on disk and to your own Nextcloud server, and nothing else.

## If something goes wrong

The **Logs** screen shows everything the background service has done, and has a
**Copy for bug report** button that puts it on your clipboard. That, plus what
you were doing at the time, is what makes a problem easy to fix.

Problems and suggestions go to
[GitHub Issues](https://github.com/keithvassallomt/digikam-memories-sync/issues).

## For developers

[architecture.md](architecture.md) describes what the parts are and how they
fit together, and [releasing.md](releasing.md) is for whoever cuts a release.
Neither is needed to use DigiMem.
