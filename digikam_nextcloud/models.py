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


@dataclass
class RegionAction:
    action: str
    path: str
    person: str
    rect: tuple[float, float, float, float]
    detail: str = ""
    nc_file_id: Optional[int] = None
    nc_detection_id: Optional[int] = None
    nc_cluster_id: Optional[int] = None


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
    skipped: int = 0
    conflicts: list[RegionConflict] = field(default_factory=list)
    actions: list[RegionAction] = field(default_factory=list)
    unmatched_paths: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    # Conflicts counted in earlier session segments (detail lists not restored)
    prior_conflicts: int = 0

    @property
    def conflict_count(self) -> int:
        return self.prior_conflicts + len(self.conflicts)

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
                "skipped": self.skipped,
                "conflicts": self.conflict_count,
            },
            "conflicts": [asdict(c) for c in self.conflicts],
            "actions": [asdict(a) for a in self.actions],
            "unmatched_paths": self.unmatched_paths,
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
    ) -> int: ...

    def sample_cluster_vector(self, cluster_id: int) -> Optional[list[float]]: ...

    def commit(self) -> None: ...

    def rollback(self) -> None: ...

    def close(self) -> None: ...
