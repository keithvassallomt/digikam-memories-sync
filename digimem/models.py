"""Data models and backend protocol."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Iterable, Optional, Protocol


@dataclass(frozen=True)
class NextcloudRequirements:
    """Nextcloud apps required by the HTTP sync path."""

    recognize_installed: bool
    face_sync_installed: bool
    face_sync_install_url: str
    # Defaulted so the many tests that build this positionally keep working.
    recognize_install_url: str = ""
    face_sync_docs_url: str = ""
    #: What the companion app says it has turned off, and why. Empty when it
    #: answered happily, or when it did not answer at all.
    face_sync_reason: str = ""
    recognize_version: str = ""

    @property
    def ready(self) -> bool:
        return self.recognize_installed and self.face_sync_installed

@dataclass
class Rect:
    """Axis-aligned box in relative coordinates (0–1 of image width/height)."""

    x: float
    y: float
    w: float
    h: float

    def clamp(self) -> "Rect":
        x = max(0.0, min(1.0, self.x))
        y = max(0.0, min(1.0, self.y))
        w = max(0.0, min(1.0 - x, self.w))
        h = max(0.0, min(1.0 - y, self.h))
        return Rect(x, y, w, h)

    def area(self) -> float:
        return max(0.0, self.w) * max(0.0, self.h)

    def iou(self, other: "Rect") -> float:
        ax2, ay2 = self.x + self.w, self.y + self.h
        bx2, by2 = other.x + other.w, other.y + other.h
        ix1, iy1 = max(self.x, other.x), max(self.y, other.y)
        ix2, iy2 = min(ax2, bx2), min(ay2, by2)
        iw, ih = max(0.0, ix2 - ix1), max(0.0, iy2 - iy1)
        inter = iw * ih
        if inter <= 0:
            return 0.0
        union = self.area() + other.area() - inter
        return inter / union if union > 0 else 0.0

    def as_tuple(self) -> tuple[float, float, float, float]:
        return (self.x, self.y, self.w, self.h)


@dataclass
class FaceRegion:
    person: str
    rect: Rect
    source: str  # "digikam" | "nextcloud"
    image_path: str = ""
    digikam_image_id: Optional[int] = None
    digikam_tag_id: Optional[int] = None
    nc_file_id: Optional[int] = None
    nc_detection_id: Optional[int] = None
    nc_cluster_id: Optional[int] = None
    # DAV parent collection name: person title, numeric cluster id, or
    # "unassigned-faces". Empty when detection is not addressable via DAV.
    dav_parent: str = ""
    file_name: str = ""
    face_vector: Optional[list[float]] = None
    threshold: float = 0.0


@dataclass
class DigikamImage:
    image_id: int
    name: str
    relative_path: str
    full_path: str
    width: int
    height: int
    file_size: int
    unique_hash: str
    orientation: int = 1
    faces: list[FaceRegion] = field(default_factory=list)


@dataclass
class NextcloudFile:
    file_id: int
    path: str  # storage-relative (files/...) or WebDAV user-relative
    name: str
    size: int
    storage_id: int = 0
    mimetype: str = ""
    webdav_path: str = ""  # path under files/{user}/ for HTTP backend


@dataclass
class NextcloudNamedFace:
    """A named Recognize detection together with its source photo."""

    file: NextcloudFile
    face: FaceRegion


@dataclass
class FileMatch:
    digikam: DigikamImage
    nextcloud: NextcloudFile
    method: str


@dataclass
class RegionConflict:
    path: str
    digikam_person: str
    digikam_rect: tuple[float, float, float, float]
    nextcloud_person: str
    nextcloud_rect: tuple[float, float, float, float]
    iou: float
    nc_detection_id: Optional[int]
    nc_file_id: int
    digikam_image_id: Optional[int] = None
    digikam_tag_id: Optional[int] = None


@dataclass
class RegionAction:
    action: str
    path: str
    person: str
    rect: tuple[float, float, float, float]
    detail: str = ""
    # The name this face carried before, when a change is being propagated.
    # Apply re-checks it against the live target and refuses if it moved.
    old_person: str = ""
    nc_file_id: Optional[int] = None
    nc_detection_id: Optional[int] = None
    nc_cluster_id: Optional[int] = None
    digikam_image_id: Optional[int] = None
    digikam_tag_id: Optional[int] = None
    nc_dav_parent: str = ""
    nc_file_name: str = ""


@dataclass
class SyncReport:
    files_digikam: int = 0
    files_nextcloud: int = 0
    files_matched: int = 0
    files_unmatched_digikam: int = 0
    faces_digikam: int = 0
    faces_nextcloud: int = 0
    assigned: int = 0
    inserted: int = 0
    created_in_digikam: int = 0
    reassigned_in_digikam: int = 0
    skipped: int = 0
    files_memories: int = 0
    faces_memories: int = 0
    files_unmatched_nextcloud: int = 0
    conflicts: list[RegionConflict] = field(default_factory=list)
    actions: list[RegionAction] = field(default_factory=list)
    unmatched_paths: list[str] = field(default_factory=list)
    unmatched_nextcloud_paths: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    # Conflicts counted in earlier session segments (detail lists not restored)
    prior_conflicts: int = 0
    # Actions recorded across the whole run, including any already written out
    # to storage. The detail cap counts these, not the list in memory, so
    # draining a batch to disk does not quietly raise the cap.
    actions_total: int = 0
    # Which faces have already been reported as a disagreement. Held separately
    # from ``conflicts`` because that list is emptied whenever a batch is
    # written out, and both passes must still see what the other found.
    conflict_keys: set[tuple[Any, Any]] = field(default_factory=set)

    @property
    def conflict_count(self) -> int:
        return self.prior_conflicts + len(self.conflicts)

    def first_sight_of(self, conflict: "RegionConflict") -> bool:
        """True the first time this face is reported as a disagreement.

        The forward and reverse passes both look at every matched pair, so
        without this the same face is counted twice.
        """
        key = (conflict.nc_file_id, conflict.nc_detection_id)
        if key == (None, None):
            key = (conflict.path, conflict.digikam_rect)
        if key in self.conflict_keys:
            return False
        self.conflict_keys.add(key)
        return True

    def to_dict(self) -> dict[str, Any]:
        return {
            "summary": {
                "files_digikam": self.files_digikam,
                "files_nextcloud": self.files_nextcloud,
                "files_matched": self.files_matched,
                "files_unmatched_digikam": self.files_unmatched_digikam,
                "faces_digikam": self.faces_digikam,
                "faces_nextcloud": self.faces_nextcloud,
                "assigned": self.assigned,
                "inserted": self.inserted,
                "created_in_digikam": self.created_in_digikam,
                "reassigned_in_digikam": self.reassigned_in_digikam,
                "skipped": self.skipped,
                "conflicts": self.conflict_count,
                "files_memories": self.files_memories,
                "faces_memories": self.faces_memories,
                "files_unmatched_nextcloud": self.files_unmatched_nextcloud,
            },
            "conflicts": [asdict(c) for c in self.conflicts],
            "actions": [asdict(a) for a in self.actions],
            "unmatched_paths": self.unmatched_paths,
            "unmatched_nextcloud_paths": self.unmatched_nextcloud_paths,
            "warnings": self.warnings,
        }


class NextcloudBackend(Protocol):
    """Shared interface for DB and HTTP Nextcloud backends."""

    user_id: str
    supports_insert: bool

    def list_image_files(
        self,
        path_prefix: str = "files/",
        storage_filter: Optional[str] = None,
    ) -> list[NextcloudFile]: ...

    def list_face_clusters(self) -> dict[str, int]: ...

    def get_or_create_cluster(self, title: str) -> int: ...

    def list_detections_for_files(
        self, file_ids: Iterable[int], files_by_id: Optional[dict[int, NextcloudFile]] = None
    ) -> dict[int, list[FaceRegion]]: ...

    def assign_person(
        self,
        detection: FaceRegion,
        person: str,
        nc_file: NextcloudFile,
        cluster_id: int,
    ) -> None: ...

    def insert_detection(
        self,
        file_id: int,
        rect: Rect,
        cluster_id: int,
        person: Optional[str] = None,
        face_vector: Optional[list[float]] = None,
        threshold: float = 0.0,
        confirmed: bool = False,
    ) -> int: ...

    def sample_cluster_vector(self, cluster_id: int) -> Optional[list[float]]: ...

    def commit(self) -> None: ...

    def rollback(self) -> None: ...

    def close(self) -> None: ...
