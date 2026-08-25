from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from zipfile import BadZipFile, ZipFile

from cueflow.config import RuntimeConfig
from cueflow.errors import (
    ContractError,
    ProviderUnavailableError,
    ReferenceMissingError,
    UnsupportedReferenceError,
)
from cueflow.project import ProjectContext
from cueflow.schema import utc_now

TEXT_FORMATS = frozenset({"txt", "md", "srt", "vtt", "ass"})
OOXML_FORMATS = frozenset({"docx", "pptx", "xlsx"})
LEGACY_OFFICE_FORMATS = frozenset({"doc", "ppt", "xls"})
IMAGE_FORMATS = frozenset({"png", "jpeg", "webp"})
DOCUMENT_FORMATS = TEXT_FORMATS | OOXML_FORMATS | LEGACY_OFFICE_FORMATS | {"pdf"}

OLE_SIGNATURE = bytes.fromhex("D0CF11E0A1B11AE1")
PNG_SIGNATURE = bytes.fromhex("89504E470D0A1A0A")
JPEG_SIGNATURE = bytes.fromhex("FFD8FF")


@dataclass(frozen=True)
class ReferenceInspection:
    detected_format: str
    media_category: str
    ffprobe: dict[str, Any] | None = None


def register_reference_asset(
    context: ProjectContext,
    path: Path,
    *,
    runtime: RuntimeConfig | None = None,
) -> dict[str, Any]:
    filename = path.name
    if not filename:
        raise ContractError("Reference filename must not be empty")
    existing = context.registry.reference_asset_by_filename(filename)
    if existing is not None:
        return dict(existing)
    resolved = _readable_reference_path(path)
    inspection = inspect_reference(resolved, runtime=runtime)
    return dict(
        context.registry.register_reference_asset(
            {
                "filename": filename,
                "locator": str(resolved),
                "detected_format": inspection.detected_format,
                "media_category": inspection.media_category,
                "registered_at": utc_now(),
            }
        )
    )


def inspect_reference(
    path: Path,
    *,
    runtime: RuntimeConfig | None = None,
) -> ReferenceInspection:
    resolved = _readable_reference_path(path)
    suffix = resolved.suffix.lower().lstrip(".")
    try:
        with resolved.open("rb") as stream:
            header = stream.read(16)
    except OSError as exc:
        raise ReferenceMissingError(f"reference_missing: {resolved}") from exc

    if header.startswith(b"PK\x03\x04"):
        detected = _detect_ooxml(resolved)
        return ReferenceInspection(detected, "document")
    if header.startswith(OLE_SIGNATURE):
        if suffix not in LEGACY_OFFICE_FORMATS:
            raise UnsupportedReferenceError(
                "OLE Reference requires a .doc, .ppt, or .xls filename for format selection"
            )
        return ReferenceInspection(suffix, "document")
    if header.startswith(b"%PDF-"):
        return ReferenceInspection("pdf", "document")
    image_format = _image_format(header)
    if image_format is not None:
        return ReferenceInspection(image_format, "image")
    if suffix in TEXT_FORMATS:
        _validate_utf8_text(resolved)
        return ReferenceInspection(suffix, "document")

    probe = _ffprobe(resolved, runtime or RuntimeConfig.detect())
    streams = probe.get("streams")
    if not isinstance(streams, list):
        raise UnsupportedReferenceError("FFprobe returned no streams for Reference")
    has_video = any(
        isinstance(stream, dict) and stream.get("codec_type") == "video" for stream in streams
    )
    has_audio = any(
        isinstance(stream, dict) and stream.get("codec_type") == "audio" for stream in streams
    )
    if not has_video and not has_audio:
        raise UnsupportedReferenceError("Reference contains neither audio nor video streams")
    format_value = probe.get("format")
    format_name = format_value.get("format_name") if isinstance(format_value, dict) else None
    detected_format = str(format_name).split(",", 1)[0] if format_name else suffix
    if not detected_format:
        raise UnsupportedReferenceError("Reference media format could not be identified")
    return ReferenceInspection(
        detected_format=detected_format,
        media_category="video" if has_video else "audio",
        ffprobe=probe,
    )


def resolve_reference_locator(context: ProjectContext, reference_asset_id: str) -> Path:
    row = context.registry.reference_asset(reference_asset_id)
    try:
        return _readable_reference_path(Path(str(row["locator"])))
    except ReferenceMissingError as exc:
        raise ReferenceMissingError(
            f"reference_missing: {row['filename']}; run "
            f"'cueflow reference relocate {context.root} FOLDER'"
        ) from exc


def relocate_references(context: ProjectContext, folder: Path) -> dict[str, Any]:
    try:
        resolved_folder = folder.resolve(strict=True)
        if not resolved_folder.is_dir():
            raise ContractError(f"Reference relocate folder is not a directory: {folder}")
        children = list(resolved_folder.iterdir())
    except ContractError:
        raise
    except OSError as exc:
        raise ContractError(f"Reference relocate folder is unreadable: {folder}") from exc

    direct_files: dict[str, Path] = {}
    for child in children:
        try:
            if child.is_symlink() or not child.is_file():
                continue
            with child.open("rb"):
                pass
        except OSError:
            continue
        direct_files[child.name] = child.resolve()

    relocated: list[dict[str, str]] = []
    unmatched: list[dict[str, str]] = []
    skipped_readable: list[str] = []
    for row in context.registry.reference_assets():
        reference_asset_id = str(row["reference_asset_id"])
        filename = str(row["filename"])
        if _is_readable_file(Path(str(row["locator"]))):
            skipped_readable.append(reference_asset_id)
            continue
        candidate = direct_files.get(filename)
        if candidate is None:
            unmatched.append(
                {
                    "reference_asset_id": reference_asset_id,
                    "filename": filename,
                    "reason": "no exact readable direct-child filename match",
                }
            )
            continue
        context.registry.update_reference_locator(reference_asset_id, str(candidate))
        relocated.append(
            {
                "reference_asset_id": reference_asset_id,
                "filename": filename,
                "locator": str(candidate),
            }
        )
    return {
        "folder": str(resolved_folder),
        "relocated": relocated,
        "unmatched": unmatched,
        "skipped_readable_reference_ids": skipped_readable,
    }


def _readable_reference_path(path: Path) -> Path:
    try:
        if not path.is_file():
            raise ReferenceMissingError(f"reference_missing: {path}")
        with path.open("rb"):
            pass
        return path.resolve()
    except ReferenceMissingError:
        raise
    except OSError as exc:
        raise ReferenceMissingError(f"reference_missing: {path}") from exc


def _is_readable_file(path: Path) -> bool:
    try:
        if not path.is_file():
            return False
        with path.open("rb"):
            return True
    except OSError:
        return False


def _validate_utf8_text(path: Path) -> None:
    try:
        with path.open("r", encoding="utf-8", errors="strict") as stream:
            while stream.read(1024 * 1024):
                pass
    except UnicodeDecodeError as exc:
        raise UnsupportedReferenceError(f"Reference text is not valid UTF-8: {path}") from exc
    except OSError as exc:
        raise ReferenceMissingError(f"reference_missing: {path}") from exc


def _detect_ooxml(path: Path) -> str:
    try:
        with ZipFile(path) as archive:
            names = set(archive.namelist())
            if "[Content_Types].xml" not in names:
                raise UnsupportedReferenceError("ZIP Reference is not an OOXML container")
            matches = [
                detected
                for detected, marker in (
                    ("docx", "word/document.xml"),
                    ("pptx", "ppt/presentation.xml"),
                    ("xlsx", "xl/workbook.xml"),
                )
                if marker in names
            ]
    except BadZipFile as exc:
        raise UnsupportedReferenceError("Reference OOXML container is corrupt") from exc
    except OSError as exc:
        raise ReferenceMissingError(f"reference_missing: {path}") from exc
    if len(matches) != 1:
        raise UnsupportedReferenceError("Reference OOXML container type is ambiguous")
    return matches[0]


def _image_format(header: bytes) -> str | None:
    if header.startswith(PNG_SIGNATURE):
        return "png"
    if header.startswith(JPEG_SIGNATURE):
        return "jpeg"
    if len(header) >= 12 and header[:4] == b"RIFF" and header[8:12] == b"WEBP":
        return "webp"
    return None


def _ffprobe(path: Path, runtime: RuntimeConfig) -> dict[str, Any]:
    if not runtime.ffprobe:
        raise ProviderUnavailableError("ffprobe is required to inspect media References")
    command = [
        runtime.ffprobe,
        "-v",
        "error",
        "-show_format",
        "-show_streams",
        "-of",
        "json",
        str(path),
    ]
    try:
        completed = subprocess.run(command, check=False, capture_output=True, text=True)
    except OSError as exc:
        raise ProviderUnavailableError(f"media tool unavailable: {runtime.ffprobe}") from exc
    if completed.returncode != 0:
        raise UnsupportedReferenceError(
            "FFprobe could not reliably identify Reference media: " + completed.stderr.strip()
        )
    try:
        value = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise UnsupportedReferenceError("FFprobe returned invalid Reference metadata") from exc
    if not isinstance(value, dict):
        raise UnsupportedReferenceError("FFprobe returned invalid Reference metadata")
    return value
