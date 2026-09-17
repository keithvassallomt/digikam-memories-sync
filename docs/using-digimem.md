# Using DigiMem

DigiMem is two things working together: a **background service** that does the
syncing, and a **window** that shows you what it is doing and asks you the
questions only you can answer.

This page walks through every screen. If you are setting DigiMem up for the
first time, start with [Installing DigiMem](install-digimem.md) instead.

## Opening and closing DigiMem

Open DigiMem from your application menu, the Start menu, Spotlight, or by
running the AppImage. The window opens in your browser, served from your own
computer.

**Closing the window does not stop DigiMem.** The background service carries
on: it keeps watching both libraries, keeps syncing, and keeps a note of
anything it needs to ask you. Opening DigiMem again simply shows you the window
once more. This is deliberate — syncing that stopped whenever you closed a
window would not be much use.

If you want DigiMem to stop doing anything at all for a while, use **Pause** on
the Home screen rather than closing the window.

## The window

### The top bar

The bar along the top always shows what DigiMem is doing, whichever screen you
are on:

| It says | What that means |
|---|---|
| **In sync** | Nothing to do. Both libraries agree. |
| **Syncing** | A sync is running now. |
| **Changes ready to apply** | A sync has finished looking and is waiting for you to approve what it found. |
| **Needs your attention** | Something needs a decision from you. |
| **Waiting for digiKam** | Changes are ready, but digiKam is open and DigiMem will not write to it while it is. |
| **Paused** | You have paused it. Nothing will change in either library. |
| **Automatic sync is off** | DigiMem only syncs when you ask it to. |
| **Setup needed** | The libraries are not connected yet. |
| **DigiMem is not responding** | The background service has stopped. Open DigiMem again to restart it. |

### The sidebar

- **Home** — what is happening now, and the buttons to do something about it.
- **Activity** — every sync DigiMem has ever run.
- **Needs attention** — everything waiting on a decision from you. The number
  beside it is how many faces are waiting.
- **Logs** — what the service has been doing, in detail.
- **Settings** — everything that is set once.

At the bottom it says when the background service started, which is a quick way
to tell whether it has restarted recently.

## Home

Home answers one question — *what is DigiMem doing?* — in the first line, and
gives you the buttons that make sense for that answer.

### The headline

The big line changes with the situation: *Everything is in sync*, *Syncing…*,
*3 faces need a decision*, *Waiting for digiKam to close*, and so on. The line
underneath adds the detail: when the last sync was and how much it changed, why
this one started, or what is being waited for.

### The buttons

What you get depends on what is happening:

- **Sync now** — a menu, described below. Always available when DigiMem is idle.
- **Pause** — a menu: **Pause for 1 hour**, **Pause until tomorrow**, or
  **Pause until I resume**. While paused, nothing changes in either library.
- **Resume** — when paused.
- **Review** — when faces are waiting for a decision.
- **Review changes** / **Apply** — when a sync has finished and its changes are
  waiting for your approval.
- **View details** — while a sync is running, to watch it in full.
- **Turn on automatic sync** — when automatic sync is off.
- **Open setup** — when the libraries are not connected yet.

### The Sync now menu

- **Sync everything now** — checks both libraries in full. This is the thorough
  one, and on a large library it takes minutes.
- **Sync one person…** — pick a name and sync only that person. Much faster,
  and usually what you want after correcting one person's name.
- **Preview only (don't apply)** — check both libraries and show what *would*
  change, without applying anything. Useful the first time, when you would
  rather see what DigiMem intends before it does it.

### While a sync is running

A progress card appears, showing which stage it has reached — reading faces
from Memories, checking digiKam photos, applying changes — how far through it
is, and three live counts: how many photos were **matched** between the two
libraries, how many **changes** have been found, and how many **conflicts**
will need you.

You can close the window and come back. The sync continues either way.

### Needs attention, on Home

When something is waiting, Home shows it directly with a **Review** button, so
you do not have to go looking. Everything else keeps syncing in the meantime —
a face waiting for a decision never holds up the rest.

This is also where DigiMem offers to turn on notifications, so it can tell you
when something needs you while the window is behind another one.

### Libraries

A small panel showing your digiKam folder and your Nextcloud address, each with
a status: digiKam is **open** or **closed**, Nextcloud is **connected** or
**not set**. The digiKam one matters more than it looks: DigiMem will not write
to digiKam while it is open.

### Recent activity

The last few syncs, newest first. Each row shows the time, what the sync did,
and a tag saying what started it — *you*, *digiKam changed*, *Memories
changed*, *digiKam closed*, *your decisions*, *daily check* or *resumed*.
Clicking a row opens that sync in full. **All activity →** opens the whole
list.

## Activity

Every sync DigiMem has run, newest first, grouped by day. The same three
columns as on Home: when it started, what it did, and what started it.

**Show older** loads more; when there is no more, it says so.

Click any row to open it.

## A single sync

The sync screen is the whole story of one sync: what it found, what it wants to
change, and the button that applies it.

### The timeline

A short list of what has happened so far — the libraries being checked, the
decisions you made, changes applied, and anything still waiting for you. The
marks tell you which is which: done, in progress, or not started.

### The four numbers

- **Already correct** — faces both libraries agreed on. Usually the biggest
  number, and the point of the whole exercise.
- **Changes for Memories** — faces that will be named or added in Nextcloud.
- **Changes for digiKam** — faces that will be named or added locally. These are
  the ones that need digiKam closed.
- **Need your decision** — faces with a different name in each library.

### What the sync found

A longer breakdown, for when you want it: how many photos were checked in
digiKam, how many were matched to photos in Nextcloud, how many were not found
there at all, and then the changes split by kind — naming faces Memories
already found, creating face boxes in Memories, creating them in digiKam,
renaming faces in digiKam, and faces skipped because you previously chose to
keep them in one library.

Photos not found in Nextcloud are not an error. They are photos that exist in
digiKam and not in your Nextcloud photos folder, and there is nothing to sync
them with.

### Applying the changes

Nothing is written to either library until you press **Apply**.

Before it applies anything locally, DigiMem copies digiKam's database, so there
is always a way back. The card above the button says so, and the copy's
location is shown once it has been made.

If the sync includes changes for digiKam, DigiMem insists that digiKam is
closed first, because writing to its database underneath it would corrupt it:

- Tick **I have closed digiKam** to confirm.
- If digiKam is still open, **Close digiKam for me** asks it to quit and waits
  until it has let go of the database.

**Apply *n* changes** starts the work. **Discard this preview** throws the
whole thing away instead — it asks first, because checking both libraries takes
minutes and any decisions you have already made go with it. Nothing has been
changed in either library at that point, so discarding is safe; it just means
the work has to be done again.

### While changes are applied

A progress bar, and a note that you can close the window: applying continues in
the background.

### When it has finished

A summary of how many changes were applied, where the digiKam backup was
written, and — if anything went wrong — which faces could not be applied and
why. **Review these faces** takes you to the screen for sorting them out.

A sync that could not finish everything is not a failure. The parts that
worked are done, and the rest wait for you.

## Needs attention

One place for everything waiting on you, across every sync. There are three
kinds, and each has its own review screen. DigiMem keeps syncing everything
else the whole time.

Faces you decide to keep in one library only are remembered. They are not
offered again unless their name or their box changes.

### Faces with different names

*The same face has a different name in digiKam and Memories.* DigiMem cannot
know which is right, so it asks.

The **Who is this?** screen shows the photo zoomed in on the face in question,
with both boxes drawn on it and labelled with the name each library has. Under
the photo it tells you how much the two boxes overlap, which is a good clue
about whether they really are the same face.

Then two buttons — the digiKam name and the Memories name. Press the correct
one and DigiMem moves to the next face.

Two options sit below:

- **Use this choice for the rest of this sync** — every remaining disagreement
  in the same sync gets the same answer. Handy when one library is clearly the
  right one this time.
- **Always use this library from now on** — later syncs stop asking altogether
  and rename the other library to match. This is the same setting as *When the
  two libraries disagree* in Settings, and can be changed back there.

When every face is decided, DigiMem says so. Your decisions are saved but
nothing has been changed yet — they are applied along with the sync they belong
to, and the button on that screen takes you to it.

### Faces that could not be added

Sometimes the other library refuses a face. Usually this is Recognize's own
face detector saying it cannot see a face inside the box it was given — most
often because the box is a little off, or the face is small, turned away or
blurred.

The review screen shows the photo with the box drawn on it, who the face is
according to the library it came from, and the reason it was refused. Then:

- **Drag the box**, or drag its corners to resize it, so it sits properly over
  the face.
- **Add to <the other library> using this box** — try again with the box as it
  now stands.
- **Keep only in <the library it came from>** — leave things as they are and
  stop being asked. DigiMem remembers, and will not offer this face again
  unless its name or box changes.

**Do this for all *n* remaining faces** applies your choice to everything still
waiting, each with its own box. It is worth using once you have seen one work:
if digiKam's rectangle was good enough for that one, it is usually good enough
for the rest.

As with everything else, nothing is written until the changes are applied, and
the screen offers to take you there when you have worked through them all.

### Face boxes that look wrong

Before each sync, DigiMem looks at digiKam itself for face boxes that can never
sync cleanly, however many times it tries. It looks for two things:

- **The same person tagged twice in one photo.** A person has one face in a
  photograph, so one of the two boxes is almost certainly a mistake.
- **A box drawn inside another person's box.** Faces do not sit inside one
  another.

Left alone, a wrongly drawn box gets proposed on every sync and refused on
every sync, for ever, because the other library has somebody else in that spot.

The **Which of these is wrong?** screen shows the photo with the boxes drawn on
it and numbered:

- With two boxes, each one has its own **Remove box** button, and **Keep both,
  this is fine** if the photo genuinely does show what it seems to.
- With more than two, tick the ones you mean, then use **Keep *n* selected** or
  **Remove *n* selected**. With nothing ticked, the buttons offer to keep them
  all or remove them all — the latter is for a photo where the person is simply
  not there.

**Skip for now** moves on without deciding.

Removing a box changes digiKam, so digiKam has to be closed, and `digikam4.db`
is backed up first — once per sitting, not once per box. Keeping boxes is
remembered, and you will not be asked about them again.

This check never holds up a sync. These are suspicions, not faults.

## Logs

Everything the background service has done, newest first. Most of the time you
will not need it; when something is behaving strangely, it is the fastest way
to see why.

- **Level** — *Info* is the normal amount of detail. *Everything* includes the
  fine-grained lines, *Warnings* and *Errors* narrow it down to trouble.
- **Run** — type a sync's number to see only that sync's lines.
- **Search** — filters as you type.
- **Follow** — keeps new lines arriving at the top. Turn it off to read without
  the list moving.
- **Copy for bug report** — puts what you are looking at on the clipboard,
  ready to paste into an issue.

The same lines are written to files under `logs/` in DigiMem's folder, if you
would rather read them there.

## Settings

Set once, checked before every sync.

### Libraries

Your digiKam folder, your Nextcloud address and account, and the photos folder
within Nextcloud. **Change** on any of them reopens the connection screen,
where you can edit them and test the connection. Leaving the app password blank
there keeps the saved one.

### What a sync does

- **When the two libraries disagree** — *Ask me* (the default) queues
  disagreements for you to decide. *Trust digiKam* and *Trust Memories* rename
  the other library to match, without asking. Trusting one is worth having on a
  library where most disagreements are simply one side being out of date; it is
  a blunt instrument otherwise.

  DigiMem settles what it can on its own before this setting comes into it: it
  remembers the name both libraries last agreed on for each face, so a rename
  on one side is applied to the other rather than queued as a question. Only a
  face renamed on *both* sides, or one with no history, is a real disagreement.
- **Create face boxes in Memories** — turn off for names only: faces Recognize
  already found get their digiKam name, and no new boxes are added.
- **Create face boxes in digiKam** — turn off and digiKam keeps the faces it
  already has.

The two are separate questions, and turning one off changes nothing about the
other.

### Automatic sync

- **Automatic sync** — the master switch. Off means DigiMem only syncs when you
  ask it to.
- **Apply changes without asking** — apply what a sync finds without stopping
  for approval. Faces with two different names always wait for you, whatever
  this is set to.
- **Wait after the last change** — how long DigiMem waits for you to stop
  making changes before syncing. This is why twenty renames produce one sync
  afterwards rather than twenty while you work.
- **Check for changes every** — how often DigiMem asks Nextcloud whether
  anything has moved.
- **Sync at least every** — a safety net: sync this often even if nothing
  appears to have changed.

### Notifications

Which things DigiMem tells you about — faces needing a decision, connection
problems, and (off by default) every completed sync, most of which change
nothing.

**Notify me in this browser** is the permission your browser controls. If you
refuse it, DigiMem falls back to your desktop's own notifications.

### Background service

- **Start DigiMem when I log in** — without it, syncing only happens while
  DigiMem is running.
- **Add to application menu** — creates the shortcut that opens this window.

### Storage

- **Backups to keep** — how many copies of digiKam's database to keep before
  the oldest is deleted. The folder they live in is shown.
- **Keep logs for** — how many days of logs to keep.

### Advanced

**Rebuild the face ledger** forgets every name the two libraries have agreed
on, and learns again from a full sync. It tells you how many names are
currently remembered.

This is a bigger deal than it sounds. The ledger is what lets DigiMem apply a
rename rather than ask you about it, so rebuilding means one round of decisions
for everything that currently disagrees. It is worth doing if the remembered
names have somehow got out of step with reality, and not otherwise. DigiMem
asks for confirmation first.

## Things you might want to do

**Sync one person I have just renamed.** Home → **Sync now → Sync one person…**.
Much faster than a full sync.

**See what DigiMem would do, without it doing anything.** Home → **Sync now →
Preview only (don't apply)**, then read the sync screen and **Discard this
preview** if you would rather it did not.

**Stop everything for an hour.** Home → **Pause → Pause for 1 hour**.

**Go back to how digiKam was before a sync.** DigiMem's folder has a `backups/`
directory with dated copies of `digikam4.db` taken before each local change.
The path of the one used is shown on the sync that made it. Close digiKam
before putting one back.

**Start again from scratch.** Settings → Advanced → **Rebuild the face ledger**
for the remembered names, or delete DigiMem's whole folder — see
[where DigiMem keeps things](index.md#where-digimem-keeps-things) — for
everything including history and settings. Neither touches your photos.
