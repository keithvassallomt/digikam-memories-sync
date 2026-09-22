# Changelog

All notable changes to DigiMem are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.3.0] - 2026-09-22

### Added

- DigiMem is on the AUR as `digimem-bin`, so Arch, Manjaro and EndeavourOS
  install and update it the way they install everything else. It repackages
  the published `.deb` rather than building from source, so the launcher, the
  menu entry and the icon are the files every other Linux user gets.

- A desktop notification carries an Open button that goes straight to the
  screen it is about, so a message about changes needing attention is one
  press away from them. All three desktops do it, each the only way it can:
  Linux waits on `notify-send --action`, Windows hands the address to the
  shell through a toast's protocol activation, which needs no registered COM
  server, and macOS posts from inside DigiMem.app through a helper that holds
  a run loop, because a notification there belongs to an application and its
  answer arrives on a loop a background service has not got. A macOS DigiMem
  run from a checkout rather than the bundle still shows the message, with
  nothing to press.

- Settings can restart the background service, so picking up a new version no
  longer means finding the right thing to kill. Whoever is in charge does it:
  under a systemd user unit or a launchd agent DigiMem asks the supervisor,
  because a job that replaces itself behind its supervisor's back is one the
  supervisor no longer knows about, would not bring back after a crash, and
  would report as stopped while it ran. With no supervisor DigiMem starts the
  replacement itself, once it has given up the lock and the port. A sync in
  progress has to finish first, and a service run in a terminal does not offer
  a restart at all.

### Fixed

- Applying no longer refuses a new face box because Recognize found the same
  face first. On photos added shortly before a sync, Recognize often detects
  its own faces between the preview and the apply, leaving an unnamed
  detection under a box digiKam had asked for. DigiMem now names that
  detection, which is what a preview taken a moment later would have proposed,
  instead of reporting that a different Memories face was in the way. A face
  genuinely carrying another name is still refused, and now says whose. The
  same goes the other way: a detection Recognize replaced after the preview is
  named rather than reported as changed.

- Naming a face in digiKam takes away the unnamed box digiKam's own detector
  had left under it, which is what digiKam does when somebody confirms a face.
  Before, the photo kept both, so one face showed twice: once with the name
  just written and once as a stranger. A face marked Ignored is a decision and
  stays, and so does a suggestion digiKam made for a real person.

## [0.2.0] - 2026-09-19

### Changed

- The Nextcloud companion app (0.6.1) accepts Recognize 13 as well as 12.
  Nothing it depends on moved between the two: the face tables are untouched,
  face-api stays on 1.7.x so the model format and API are unchanged, and the
  Node binary setting is where it was. An upper bound remains, because an
  untested major is what the check is for.

### Fixed

- When the companion app has turned a feature off, DigiMem says which one and
  repeats the app's own reason, instead of reporting that the app needs
  installing or updating. On a server running Recognize 13 that advice was
  wrong in both halves: the app was current, and reinstalling it changed
  nothing.

- The Nextcloud companion app archive builds reproducibly: two clones of the
  same commit now produce the same bytes. Previously the file order came from
  however the directory happened to be read and the timestamps from whenever
  the files were checked out, so nobody could check that a published release
  matched its source.

## [0.1.0] - 2026-09-19

First release.

### Added

#### Two-way synchronization

- Face names and rectangles are kept in step between a local digiKam library
  and Nextcloud Memories/Recognize, in both directions.
- Creating face boxes in Memories and creating them in digiKam are separate
  settings, so either half of a first sync can be turned off. With Memories
  off, a sync is names only: faces Recognize already found get their digiKam
  name, and no new boxes are drawn.
- A face ledger remembers the name both libraries last agreed on, so a rename
  in either one is applied to the other rather than queued as a question. Only
  a face renamed on both sides, or one with no history, waits for a decision.

#### Automatic operation

- A background service watches digiKam's database and polls Nextcloud. A change
  starts a wait rather than a sync, so twenty renames produce one sync
  afterwards instead of twenty while you work. The wait is capped an hour from
  the first change, so a busy library still syncs hourly.
- Syncs wait for Recognize to finish its own work, and hold digiKam's half
  until digiKam is closed. Opening digiKam part way through stops the writes
  within a second and the rest waits.
- Both libraries are fingerprinted per person, so a change belonging to one
  person is synced by looking at that person rather than at everything. The
  common case — one person named or corrected — goes from minutes to seconds.
  A change that cannot be pinned on exactly one person, and the daily sweep,
  still look at everyone.

#### Reviewing disagreements

- Conflicts are reviewed against the photo, with the face zoomed and every box
  drawn and labelled.
- HEIC and other formats the browser cannot draw are previewed through
  Nextcloud.
- *Ask me* is the default. *Trust digiKam* and *Trust Memories* rename the
  other library to match without asking, which suits a library with no history
  where most disagreements are simply one side being right. The same choice is
  offered as "Always use this library from now on" while reviewing.
- A face rejected by the model is shown with its source name and rectangle. The
  rectangle can be adjusted for a targeted retry, or the face kept in its
  source library; remembered one-sided faces are suppressed in later previews
  unless their source box changes.

#### The library check

- Before every sync, digiKam is checked for face boxes that cannot sync cleanly
  however often it is tried: a person tagged more than once in one photo, and a
  box drawn inside another person's. Left alone, these are proposed on every
  run and refused on every run, for ever.
- The check takes about 140 ms over 13,700 faces and never holds a sync up.
- Findings arrive in Needs attention with the photo and every box drawn on it.
  Boxes can be removed individually, ticked and removed or kept in a batch, or
  all removed at once for a photo the person is not in. Keeping a box is
  remembered and never asked again.

#### Writing safely

- Apply executes the list the preview saved, never a fresh comparison. A run's
  plan freezes once its journal exists; a decision made after that is carried
  by a follow-up run.
- Every action re-reads its target and refuses if the name, rectangle or file
  id has moved since the preview.
- A consistent copy of `digikam4.db` is made immediately before the first
  digiKam write that actually happens, using SQLite's backup API so committed
  write-ahead content is included. Backups live under the configuration
  directory in `backups/`.
- Each operation is recorded, so an interrupted Apply resumes without repeating
  completed changes, and continues past individual face failures rather than
  stopping.

#### Interface

- A browser interface of plain ES modules — no framework, no build step —
  covering first-run setup, a home screen, preview and conflict review, an
  explicit Apply step, a Needs attention inbox, settings and logs.
- Closing the browser does not stop anything. Notifications are raised in the
  page where possible, and through the desktop when no window is open.
- Settings files are versioned and upgraded in place on first load, with
  automatic sync left off so upgrading never starts changing libraries on its
  own.

#### Platform integration

- Start at login and application-menu shortcuts, with icons, on Linux, macOS
  and Windows.
- Whether digiKam is open is read from `/proc` on Linux and through `psutil`
  elsewhere; macOS and Windows need the `desktop` extra to notice it at all.
- Credentials are stored in the system keyring.
- Nextcloud can be reached over HTTP, over an SSH tunnel, or by connecting
  directly to its database — MariaDB/MySQL, PostgreSQL or SQLite, through the
  `database` extra.

#### Command line

- `digimem run` performs a one-off sync, a preview unless `--apply` is given.
  The command line has no ledger, so it treats every disagreement as a
  conflict.
- `digimem service --once` starts up, does a single pass and exits, which is
  what a build check runs.
- One service owns a configuration directory, held by an exclusive lock and
  published in `service.json`. Starting a second prints the address of the
  running one and exits with status 3.

#### Nextcloud companion app

- `digikam_face_sync` 0.6.0, available from the Nextcloud App Store and
  installed separately from Recognize so a Recognize update cannot overwrite it.
  It creates detections Recognize is missing, reads named faces, and reports a
  change fingerprint and whether Recognize is busy.
- Where the companion app is older than 0.5.0, change detection is left off and
  DigiMem falls back to its daily check.
- Requires Nextcloud 33 to 35 and PHP 8.2 or later.

### Requirements

- Python 3.10 or later.
- digiKam with an SQLite database.
- Nextcloud 33 to 35 with Recognize installed and enabled, and the digiKam Face
  companion app.

[Unreleased]: https://github.com/keithvassallomt/digikam-memories-sync/compare/v0.3.0...HEAD
[0.3.0]: https://github.com/keithvassallomt/digikam-memories-sync/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/keithvassallomt/digikam-memories-sync/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/keithvassallomt/digikam-memories-sync/releases/tag/v0.1.0
