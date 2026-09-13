# Application architecture

## Release boundary

The first usable release synchronizes face names and rectangles in both
directions between digiKam and Nextcloud Memories/Recognize. The interface may
be developed against the existing one-way engine, but the application is not
complete until Memories → digiKam changes work too.

## Components

- `digikam_nextcloud`: UI-independent Python engine, matching logic and
  Nextcloud client.
- Local service: owns schedules, filesystem watching, run state and the HTTP
  API used by the browser UI.
- Browser UI: the guided five-step workflow in the prototype.
- `nextcloud-app/digikam_face_sync`: authenticated Nextcloud endpoint that
  creates missing Recognize detections and descriptors.
- State database: SQLite ledger for paired faces, previous values, run history,
  conflict decisions and notifications.

The application will expose `ui`, `run` and `service` modes. Closing the browser
does not stop a scheduled run or filesystem watcher.

## Connection checks

First-run setup performs these checks in order:

1. Connect to the supplied Nextcloud URL with the username and app password.
2. Probe the user's Recognize DAV `faces` collection. HTTP 404 means Recognize
   is not installed or enabled and blocks setup.
3. Probe `digikam_face_sync/api/v1/face-import`. If its advertised API is
   missing, show an installation action. Until the app has a published page,
   that action opens `https://keithvassallo.com`.
4. Save the successful settings and credential.

## Two-way synchronization

Every paired face receives a ledger record containing its digiKam image and tag
IDs, Nextcloud file and detection IDs, last-seen person names and rectangles,
and the last successful sync revision. A later run can therefore tell which
side changed. If both sides changed the same face, it becomes a conflict rather
than silently choosing a winner.

Automatic deletion propagation is outside the initial release. Missing records
are reported for review.

## Notifications

The service emits structured events independently of the UI. Events are stored
in SQLite before a desktop notification is attempted, so they remain visible
in the in-app inbox and run history.

Initial event types are:

- `run.completed`: for example, “Updated 262 face names and created 1,390 face
  boxes.”
- `run.no_changes`: both libraries already agree.
- `conflicts.created`: for example, “12 conflicts need your attention.”
- `run.failed`: connection, authentication or processing failure.
- `connection.action_required`: credentials or a required Nextcloud app need
  attention.

Notifications are grouped by run. Their click target contains the run ID and
opens the relevant result or conflict screen. Scheduled runs preview and notify
by default; automatic application of unambiguous changes is an explicit user
setting. Conflicts always wait for a person.
