"""Upload paths contain only server-owned UUIDs and digests."""

from pathlib import Path
from uuid import UUID


def document_path(root: Path, pack_id: UUID, sha256: str) -> Path:
    return root / str(pack_id) / (sha256 + ".pdf")


def safe_filename(name: str | None) -> str:
    return (name or "document.pdf").replace("\\", "/").rsplit("/", 1)[-1] or "document.pdf"
