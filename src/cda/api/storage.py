"""Upload paths contain only server-owned UUIDs and digests."""

from pathlib import Path
from uuid import UUID


def document_path(root: Path, pack_id: UUID, sha256: str, suffix: str = ".pdf") -> Path:
    return root / str(pack_id) / (sha256 + suffix)


def office_suffix(filename: str | None) -> str | None:
    """`.docx` / `.xlsx` for Office uploads (X1); None means PDF."""
    suffix = Path(filename or "").suffix.lower()
    return suffix if suffix in (".docx", ".xlsx") else None


def safe_filename(name: str | None) -> str:
    return (name or "document.pdf").replace("\\", "/").rsplit("/", 1)[-1] or "document.pdf"
