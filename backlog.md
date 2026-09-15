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

## Trust one library

I know my digiKam library is correct. So both manual and automatic syncs need
an option to **trust digiKam** or **trust Memories**: instead of asking about
every disagreement, the trusted library's name simply wins and the change is
made to the other one.

Three settings, then, rather than two: ask me, trust digiKam, trust Memories.
"Ask me" stays the default, because the wrong choice here rewrites names in
bulk.

### What already exists

The engine has `prefer_digikam_on_conflict`, which the command line uses and
the interface hardcodes to `False`. Turning it on makes a digiKam name
overwrite a disagreeing Memories name without asking, which is exactly "trust
digiKam" for the forward direction.

So the work is mostly exposure rather than new engine code:

- A settings value, and the same choice offered on the conflict review screen
  as "always do this".
- The reverse of it. There is no `prefer_memories_on_conflict`; the reverse
  pass in `reverse.py` would need the matching branch, producing
  `reassign_digikam` actions rather than conflicts.
- Wiring it through `AppService.preview`, which currently passes `False`.

### How it interacts with the ledger

Phase 2 already resolves most disagreements without asking, by remembering the
name both libraries last agreed on. Trusting a library only changes what
happens to the cases the ledger cannot settle: a face renamed on both sides,
and a face with no history at all.

On a library with no history that is still most of them, which is exactly the
situation that makes this worth having.

### Why it is deferred

It writes names in bulk with no review step, so it wants the ledger and the
review screens proven first. Phase 2 gives both.

## Bulk retry for rejected faces

The review screen already has "keep all remaining rejected faces where they
are". It needs the opposite: add all remaining using their digiKam boxes.

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

It also strengthens the case for making `confirmed` the default on every
insert, rather than only on a retry. The counter-argument stands: it hands
Recognize descriptors from boxes its own detector rejected, which may weaken
its clustering. Worth measuring before changing the default.

## Related: the Memories side is much sparser than digiKam

Measured on the development library, sampling 80 photos that carry faces:

- Half of digiKam's face rectangles have no Recognize detection at that spot.
- A fifth are detected by Recognize but unnamed.
- Just under a third already agree.

digiKam knows 133 people; Memories knows 12. A full sync would propose creating
roughly 6,900 face boxes in Memories, which is the slow path and the one that
produces rejections.

Worth deciding separately whether creating boxes in Memories should be its own
setting, so a first sync can be "names only" and leave box creation for later.
