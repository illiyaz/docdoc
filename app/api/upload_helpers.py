"""Upload file helpers — archive extraction, email attachments, format routing.

Step 24b: Handles all 47 supported formats for file upload.
- Archives (ZIP/7z): extracted recursively, contents processed individually
- Emails (EML/MSG): body kept for EmailReader, attachments saved alongside
- Everything else: saved directly to upload directory

No heavy dependencies (no Presidio, no SQLAlchemy). Safe to import anywhere.
"""
from __future__ import annotations

import logging
import zipfile
from pathlib import Path

logger = logging.getLogger(__name__)

# Supported extensions for upload (matches reader registry + archives)
SUPPORTED_EXTENSIONS = frozenset({
    # Documents
    ".pdf", ".xlsx", ".xls", ".xlsm", ".docx", ".csv", ".tsv",
    ".html", ".htm", ".xml", ".eml", ".msg",
    ".parquet", ".avro",
    # Images (Step 23b — vision-first extraction)
    ".jpg", ".jpeg", ".png", ".bmp", ".webp", ".gif",
    ".heic", ".heif", ".tif", ".tiff",
    # Archives (extracted recursively)
    ".zip", ".7z",
})

# Archives need special handling — extract contents, process each file
ARCHIVE_EXTENSIONS = frozenset({".zip", ".7z"})

# Email formats — save body + extract attachments as separate files
EMAIL_EXTENSIONS = frozenset({".eml", ".msg"})

# Extensions to silently skip during upload
SKIP_EXTENSIONS = frozenset({
    ".ds_store", ".txt", ".log", ".tmp", ".swp",
    ".gitignore", ".gitkeep",
})


def is_supported(filename: str) -> bool:
    """Return True if the file extension is supported by a reader."""
    ext = Path(filename).suffix.lower()
    return ext in SUPPORTED_EXTENSIONS


def should_skip(filename: str) -> bool:
    """Return True if the file should be silently skipped."""
    name = Path(filename).name.lower()
    ext = Path(filename).suffix.lower()
    return ext in SKIP_EXTENSIONS or name.startswith(".")


def safe_filename(directory: Path, original_name: str) -> Path:
    """Return a unique path under directory, adding _1, _2 suffix for dupes."""
    target = directory / original_name
    if not target.exists():
        return target
    stem = Path(original_name).stem
    suffix = Path(original_name).suffix
    counter = 1
    while True:
        candidate = directory / f"{stem}_{counter}{suffix}"
        if not candidate.exists():
            return candidate
        counter += 1


def extract_archive(archive_path: Path, dest_dir: Path) -> list[dict]:
    """Extract a ZIP or 7z archive and return metadata for supported files.

    Unsupported and hidden files inside the archive are silently skipped.
    Nested archives are extracted recursively (max 1 level deep).
    Returns list of {"name": ..., "size_bytes": ..., "extension": ...}.
    """
    extracted: list[dict] = []
    ext = archive_path.suffix.lower()

    if ext == ".zip":
        try:
            with zipfile.ZipFile(archive_path, "r") as zf:
                for info in zf.infolist():
                    if info.is_dir():
                        continue
                    member_name = Path(info.filename).name
                    if should_skip(member_name) or not is_supported(member_name):
                        continue

                    dest = safe_filename(dest_dir, member_name)
                    with zf.open(info) as src, open(dest, "wb") as dst:
                        dst.write(src.read())

                    member_ext = Path(member_name).suffix.lower()
                    file_size = dest.stat().st_size

                    # Recursively extract nested archives (1 level)
                    if member_ext in ARCHIVE_EXTENSIONS:
                        nested = extract_archive(dest, dest_dir)
                        extracted.extend(nested)
                        dest.unlink(missing_ok=True)
                    else:
                        extracted.append({
                            "name": dest.name,
                            "size_bytes": file_size,
                            "extension": member_ext,
                        })
        except zipfile.BadZipFile:
            logger.warning("Bad ZIP file: %s", archive_path)
    elif ext == ".7z":
        try:
            import py7zr
            with py7zr.SevenZipFile(archive_path, mode="r") as szf:
                szf.extractall(path=str(dest_dir))

            # Walk extracted files
            for child in dest_dir.rglob("*"):
                if child.is_dir():
                    continue
                child_name = child.name
                if should_skip(child_name) or not is_supported(child_name):
                    child.unlink(missing_ok=True)
                    continue
                child_ext = child.suffix.lower()
                extracted.append({
                    "name": child_name,
                    "size_bytes": child.stat().st_size,
                    "extension": child_ext,
                })
        except ImportError:
            logger.warning("py7zr not installed — cannot extract .7z files")
        except Exception:
            logger.warning("Failed to extract 7z archive: %s", archive_path, exc_info=True)

    return extracted


def extract_email_attachments(email_path: Path, dest_dir: Path) -> list[dict]:
    """Extract attachments from an EML or MSG file, saving them to dest_dir.

    The email file itself is kept (body text is read by EmailReader).
    Only supported-format attachments are saved.
    Returns list of {"name": ..., "size_bytes": ..., "extension": ..., "source": ...}.
    """
    import email as _email_lib
    import email.policy as _email_policy

    extracted: list[dict] = []
    ext = email_path.suffix.lower()

    if ext == ".eml":
        try:
            raw = email_path.read_bytes()
            msg = _email_lib.message_from_bytes(raw, policy=_email_policy.default)

            if msg.is_multipart():
                for part in msg.walk():
                    disposition = str(part.get("Content-Disposition", ""))
                    if "attachment" not in disposition:
                        continue
                    att_name = part.get_filename()
                    if not att_name:
                        continue
                    if should_skip(att_name) or not is_supported(att_name):
                        continue
                    payload = part.get_payload(decode=True)
                    if not payload:
                        continue

                    dest = safe_filename(dest_dir, att_name)
                    dest.write_bytes(payload)
                    extracted.append({
                        "name": dest.name,
                        "size_bytes": len(payload),
                        "extension": Path(att_name).suffix.lower(),
                        "source": f"attachment from {email_path.name}",
                    })
        except Exception:
            logger.warning("Failed to extract EML attachments: %s", email_path, exc_info=True)

    elif ext == ".msg":
        try:
            import extract_msg
            msg = extract_msg.Message(str(email_path))

            for att in msg.attachments:
                att_name = getattr(att, "longFilename", None) or getattr(att, "shortFilename", None)
                if not att_name:
                    continue
                if should_skip(att_name) or not is_supported(att_name):
                    continue
                payload = att.data
                if not payload:
                    continue

                dest = safe_filename(dest_dir, att_name)
                dest.write_bytes(payload)
                extracted.append({
                    "name": dest.name,
                    "size_bytes": len(payload),
                    "extension": Path(att_name).suffix.lower(),
                    "source": f"attachment from {email_path.name}",
                })
            msg.close()
        except ImportError:
            logger.warning("extract-msg not installed — cannot extract MSG attachments")
        except Exception:
            logger.warning("Failed to extract MSG attachments: %s", email_path, exc_info=True)

    return extracted


def process_uploaded_file(
    content: bytes,
    filename: str,
    upload_path: Path,
) -> list[dict]:
    """Save a single uploaded file, handling archives and email attachments.

    Returns list of file metadata dicts for all files produced
    (may be >1 for archives/emails with attachments).
    """
    file_ext = Path(filename).suffix.lower()
    dest = safe_filename(upload_path, filename)
    dest.write_bytes(content)
    file_size = len(content)

    result: list[dict] = []

    if file_ext in ARCHIVE_EXTENSIONS:
        # Extract archive contents, remove the archive itself
        nested = extract_archive(dest, upload_path)
        result.extend(nested)
        dest.unlink(missing_ok=True)
    else:
        # Keep the file itself
        result.append({
            "name": dest.name,
            "size_bytes": file_size,
            "extension": file_ext,
        })

        # Also extract email attachments alongside the email
        if file_ext in EMAIL_EXTENSIONS:
            att_files = extract_email_attachments(dest, upload_path)
            result.extend(att_files)

    return result