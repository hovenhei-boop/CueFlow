from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from cueflow.config import MAX_USER_KEYWORDS
from cueflow.errors import ContractError, UnsupportedReferenceError
from cueflow.schema import TEXT_REFERENCE_FORMATS

OFFICE_FORMATS = frozenset({"doc", "docx", "ppt", "pptx", "xls", "xlsx"})


@dataclass(frozen=True)
class ReferenceSpec:
    kind: str
    value: str


def build_job_input_payload(
    *,
    source_asset_id: str,
    references: Sequence[ReferenceSpec] = (),
    keywords: Sequence[str] = (),
) -> dict[str, object]:
    prepared: list[dict[str, object]] = []
    for ordinal, spec in enumerate(references):
        if spec.kind in {"pdf_url", "image_url"}:
            prepared.append(_url_reference(spec, ordinal))
        elif spec.kind == "text_file":
            prepared.append(_text_reference(Path(spec.value), ordinal))
        else:
            raise ContractError(f"unknown Reference input kind: {spec.kind}")
    return {
        "source_asset_id": source_asset_id,
        "references": prepared,
        "user_keywords": normalize_keywords(keywords),
    }


def normalize_keywords(values: Sequence[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for raw in values:
        if not isinstance(raw, str):
            raise ContractError("keywords must be strings")
        value = raw.strip()
        if not value:
            raise ContractError("keywords must not be empty or whitespace-only")
        if value not in seen:
            seen.add(value)
            result.append(value)
    if len(result) > MAX_USER_KEYWORDS:
        raise ContractError(f"at most {MAX_USER_KEYWORDS} user keywords are allowed")
    return result


def has_correction_context(payload: dict[str, object]) -> bool:
    return bool(payload.get("references") or payload.get("user_keywords"))


def _url_reference(spec: ReferenceSpec, ordinal: int) -> dict[str, object]:
    url = spec.value.strip()
    parsed = urlsplit(url)
    if parsed.scheme != "https" or not parsed.netloc:
        raise UnsupportedReferenceError(
            "v0.5.2 PDF and image References require an absolute HTTPS URL"
        )
    display_name = Path(parsed.path).name or parsed.netloc
    return {
        "ordinal": ordinal,
        "kind": spec.kind,
        "url": url,
        "display_name": display_name,
        "locator_semantics": "mutable_remote_locator",
    }


def _text_reference(path: Path, ordinal: int) -> dict[str, object]:
    suffix = path.suffix.lower().lstrip(".")
    if suffix in OFFICE_FORMATS:
        raise UnsupportedReferenceError(
            "v0.5.2 does not convert Office files; export the file to PDF and provide "
            "it with --pdf-url"
        )
    if suffix == "pdf" or suffix in {"png", "jpg", "jpeg", "webp"}:
        raise UnsupportedReferenceError(
            "v0.5.2 accepts local files only for TXT/MD/CSV/JSON; PDF and images "
            "must use --pdf-url or --image-url"
        )
    if suffix not in TEXT_REFERENCE_FORMATS:
        raise UnsupportedReferenceError("v0.5.2 text References must be TXT, MD, CSV, or JSON")
    try:
        if not path.is_file():
            raise UnsupportedReferenceError(f"Reference text file is missing: {path}")
        text = path.read_text(encoding="utf-8-sig")
    except UnicodeDecodeError as exc:
        raise UnsupportedReferenceError(f"Reference text file must be valid UTF-8: {path}") from exc
    except OSError as exc:
        raise UnsupportedReferenceError(f"Reference text file is unreadable: {path}") from exc
    if not text:
        raise UnsupportedReferenceError(f"Reference text file is empty: {path}")
    return {
        "ordinal": ordinal,
        "kind": "text",
        "format": suffix,
        "display_name": path.name,
        "text": text,
    }
