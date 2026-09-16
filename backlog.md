The basic sync functionality is now working well. So here's what's next for the app.

1. Automatic operation.
2. Robustness.

- When the UI launches, right now we're thrown straight into the wizard to do the sync operation. Now, our app is going to have configuration options, such as configuring the automatic operation, login and so on. So we need to take that into account as the home screen. 

- We need to allow configuration to be a one time thing. So the user has a settings page where they can configure their Digikam photo library and nextcloud connection. When a sync occurs, these settings are simply verified, and the step is therefore skipped if everything is ok. 

- We also need automatic operation. Now, this is not as easy as it seems. We need to account for:
    A. DigiKam being open and in use when the sync is scheduled to run.
    B. Laptops and other devices that can sleep or be suspended mid-run.
    C. Temporary connection failure.

    This necessitates the following:
    - We need to know when DigiKam is open and in use, and defer any operations whilst that's the case. 
    - We should probably also know when recognize is running it's batch task. Although this typically runs off-hours. 
    - When a run is interrupted, it should be able to pick up exactly where it left off. 
    - The user might start DigiKam whilst a run is ongoing. We need to handle this. Perhaps, we can make changes to a copy of the library, and then wait for digikam to shutdown, reconcile changes in the database (i.e. our copy and the latest digikam db) and replace the primary copy.

- Ideally, we should be able to detect changes to the DB, or Memories, and then trigger a sync. We would however need a backoff to ensure that many small changes do not end up being hundreds of individual sync jobs. 

- Logging, viewable through the UI itself.

- A .desktop file (linux) and relevant aliases/shortcuts on macOS/Windows which simply open the web ui.

- Options to pause/resume automatic synchronisation.

We need to start with a proper design document based on the above, including UI prototypes of how this will work. We need to get the flow right in the UI before working on the implementation. This software should be incredibly easy to use and obvious to the end user. 
---

# Deferred

Not part of phase 2. Recorded so the reasoning is not lost.

## Sync one person when only one person changed — done

A full preview takes minutes on this library, and most of what triggers one is
a single person being named or corrected. Both fingerprints now carry a hash
per person, so a trigger can say who moved, and a change that names exactly one
person starts a run scoped to them.

Reading the per-person hashes is free: it is the same scan that produced the
whole-library value. About 20 ms over 13,756 face regions and 133 people.

Two things must stay conservative, and do. A change no one can be blamed for
looks at everyone: an older companion app, a digiKam face on a tag that is not
a person, a rename, which reads as two people rather than one. And the daily
fallback sweep is never scoped, because it exists to catch what the
fingerprints missed.

The bookkeeping is the subtle half. A scoped run only learned about its own
person, so it promotes that person's hash and leaves the whole-library values
and the fallback clock alone. The library still reads as out of step, the next
poll picks up the next person, and a queue of changed people drains one run at
a time rather than one run covering work it never looked at.

## Cross-platform process detection — done

macOS and Windows can now tell whether digiKam is open, through `psutil` in the
`desktop` extra. Linux keeps the dependency-free `/proc` scan. Confirmed
working on both platforms.

## Trust one library — done

I know my digiKam library is correct. So both manual and automatic syncs need
an option to **trust digiKam** or **trust Memories**: instead of asking about
every disagreement, the trusted library's name simply wins and the change is
made to the other one.

Three settings, then, rather than two: ask me, trust digiKam, trust Memories.
"Ask me" stays the default, because the wrong choice here rewrites names in
bulk.

### What was built

`sync.conflict_policy` in settings, offered under "What a sync does" and as
"Always use this library from now on" next to a conflict you are already
deciding. The engine takes a policy rather than the old
`prefer_digikam_on_conflict` boolean, and the command line maps its existing
config key onto it.

A trusted library no longer raises the disagreement at all. It used to be
recorded *and* overwritten, which would have filled Needs attention with
questions the setting had already answered.

Trusting Memories turned out not to need the reverse pass at all. The backlog
assumed it would, but the forward pass gained a `reassign_digikam` branch when
the ledger landed, so trusting Memories reuses it: the same rename the ledger
would have proposed had it known which side moved. The reverse pass only had to
stop asking, the way it already does for a pair the ledger has attributed.

### How it interacts with the ledger

Phase 2 already resolves most disagreements without asking, by remembering the
name both libraries last agreed on. Trusting a library only changes what
happens to the cases the ledger cannot settle: a face renamed on both sides,
and a face with no history at all.

On a library with no history that is still most of them, which is exactly the
situation that makes this worth having.

## Bulk retry for rejected faces — done

Built. The review screen's checkbox now applies to whichever choice you make,
so "Add using this box" with it ticked adds every remaining rejected face,
each keeping its own rectangle.

One click would mark every remaining rejected face as a confirmed digiKam box,
leave its rectangle alone, and put it back in the queue. Applying then sends
`confirmed: true`, which the companion app treats as "this rectangle is already
the detection": it skips Recognize's face detector and computes the descriptor
straight from the box using the same landmark alignment and recognition model
Recognize uses afterwards.

It would not adjust any box, would not bypass the overlapping-face guard, and
would not guarantee success, since landmark alignment can still fail. Anything
that fails again comes back to the queue.

This is distinct from trusting a library. A bulk retry is a decision about
faces already rejected, taken after looking at them. Trusting digiKam is a
standing policy applied before anything is rejected.

### What the first full sync showed

7,977 changes applied in about four hours, roughly two seconds each.

| | |
|---|---|
| Applied | 7,706 |
| Rejected by Recognize's detector | 264 |
| Caught by the overlap guard | 2 |

The rejected boxes were six times smaller than the accepted ones by area, and
three quarters of them under a quarter of the typical accepted size. They are
small faces in group shots, spread over 48 people.

Reviewing them one at a time showed the digiKam boxes were right in the
overwhelming majority, and the faces clear. That is 271 individual clicks to
reach a conclusion that was the same every time, which is the argument for the
bulk action.

### Confirmed is now the default — decided

Every insert offers digiKam's box as the detection, not just a retry. Recognize
detects first regardless, so this changes nothing about the faces it finds; it
only decides what happens to the ones it finds nothing in. On this library that
was 264 faces whose boxes a review found correct nearly every time, so the
alternative was a guaranteed round of clicks for a known answer.

The counter-argument was accepted rather than answered: it hands Recognize
descriptors from boxes its own detector rejected, and those descriptors join
clusters and pull their centroids, which can merge two people. Nobody reviews
them now.

So the effect is at least recorded. The companion app returns the descriptor
confidence and Face Sync kept throwing it away; an insert result now carries
`score`, where zero means the box was taken as given. Comparing those faces
against their own cluster's other members is what would show the damage, if
there is any.

A server too old for confirmed inserts refuses them outright, so an unreviewed
face settles for the detector rather than failing the run. A reviewed one still
insists, because falling back would only put it through the detector that
rejected it in the first place.

## Related: the Memories side is much sparser than digiKam

Measured on the development library, sampling 80 photos that carry faces:

- Half of digiKam's face rectangles have no Recognize detection at that spot.
- A fifth are detected by Recognize but unnamed.
- Just under a third already agree.

digiKam knows 133 people; Memories knows 12. A full sync would propose creating
roughly 6,900 face boxes in Memories, which is the slow path and the one that
produces rejections.

Decided: creating boxes is its own setting, one per direction, under "What a
sync does". Both are on, which is what every sync did before they existed.
Turning off "Create face boxes in Memories" leaves a names-only sync: faces
Recognize already found get their digiKam name, and the 6,900 slow inserts wait
for a later decision. Naming and box creation are separate questions, so the
disagreement above is still raised either way.
