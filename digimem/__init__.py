"""digiKam → Nextcloud Recognize face-region sync."""

from .sync import sync
from .models import SyncReport

__all__ = ["sync", "SyncReport"]
__version__ = "0.3.0"
