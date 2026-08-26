from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any
from xml.etree import ElementTree
from zipfile import BadZipFile, ZipFile, ZipInfo

from cueflow.errors import ContractError, UnsupportedReferenceError

MAX_OOXML_ENTRIES = 10_000
MAX_OOXML_EXPANDED_BYTES = 512 * 1024 * 1024
MAX_OOXML_ENTRY_BYTES = 64 * 1024 * 1024

REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
OFFICE_REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
WORD_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
PRESENTATION_NS = "http://schemas.openxmlformats.org/presentationml/2006/main"
DRAWING_NS = "http://schemas.openxmlformats.org/drawingml/2006/main"
SHEET_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"


@dataclass(frozen=True)
class DocumentExtraction:
    detected_format: str
    blocks: tuple[dict[str, Any], ...]
    metadata: Mapping[str, Any]

    def content(self) -> dict[str, Any]:
        return {
            "format": self.detected_format,
            "blocks": [dict(block) for block in self.blocks],
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True)
class PdfClassification:
    route: str
    page_count: int
    text_page_count: int
    content_without_text_pages: tuple[int, ...]


def extract_text_document(path: Path, detected_format: str) -> DocumentExtraction:
    if detected_format not in {"txt", "md"}:
        raise ContractError("deterministic text document route accepts only TXT or MD")
    try:
        text = path.read_text(encoding="utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise UnsupportedReferenceError("Reference text is not valid UTF-8") from exc
    except OSError as exc:
        raise UnsupportedReferenceError(f"Reference text is unreadable: {path}") from exc
    return DocumentExtraction(
        detected_format,
        ({"ordinal": 0, "kind": "text", "text": text},),
        {"encoding": "utf-8"},
    )


def extract_text_cues(path: Path, detected_format: str) -> DocumentExtraction:
    try:
        text = path.read_text(encoding="utf-8", errors="strict")
    except (OSError, UnicodeDecodeError) as exc:
        raise UnsupportedReferenceError(
            f"Reference cue file is not readable UTF-8: {path}"
        ) from exc
    if detected_format == "srt":
        cues = _parse_srt(text)
    elif detected_format == "vtt":
        cues = _parse_vtt(text)
    elif detected_format == "ass":
        cues = _parse_ass(text)
    else:
        raise ContractError("cue parser accepts only SRT, VTT, or ASS")
    if not cues:
        raise UnsupportedReferenceError("Reference subtitle file contains no readable cues")
    return DocumentExtraction(detected_format, tuple(cues), {"encoding": "utf-8"})


def extract_ooxml(path: Path, detected_format: str) -> DocumentExtraction:
    if detected_format not in {"docx", "pptx", "xlsx"}:
        raise ContractError("OOXML route accepts only DOCX, PPTX, or XLSX")
    try:
        with ZipFile(path) as archive:
            _validate_ooxml_archive(archive)
            if detected_format == "docx":
                blocks = _extract_docx(archive)
            elif detected_format == "pptx":
                blocks = _extract_pptx(archive)
            else:
                blocks = _extract_xlsx(archive)
    except BadZipFile as exc:
        raise UnsupportedReferenceError("Reference OOXML container is corrupt") from exc
    except RuntimeError as exc:
        raise UnsupportedReferenceError("Reference OOXML container is encrypted") from exc
    if not blocks:
        raise UnsupportedReferenceError("Reference OOXML document contains no extractable text")
    return DocumentExtraction(detected_format, tuple(blocks), {"parser": "stdlib-zip-xml"})


def classify_pdf(path: Path) -> PdfClassification:
    reader = _pdf_reader(path)
    text_pages = 0
    content_without_text: list[int] = []
    for page_index, page in enumerate(reader.pages):
        text = (page.extract_text() or "").strip()
        if text:
            text_pages += 1
            continue
        contents = page.get_contents()
        if contents is None:
            continue
        try:
            raw = contents.get_data()
        except Exception as exc:
            raise UnsupportedReferenceError(
                f"PDF page {page_index + 1} content cannot be inspected"
            ) from exc
        if raw.strip():
            content_without_text.append(page_index + 1)
    route = "cloud_document_parse" if content_without_text else "document_text"
    return PdfClassification(
        route=route,
        page_count=len(reader.pages),
        text_page_count=text_pages,
        content_without_text_pages=tuple(content_without_text),
    )


def extract_text_layer_pdf(path: Path) -> DocumentExtraction:
    classification = classify_pdf(path)
    if classification.route != "document_text":
        raise UnsupportedReferenceError(
            "PDF has content pages without a reliable text layer; use Cloud document parse"
        )
    reader = _pdf_reader(path)
    blocks = tuple(
        {
            "ordinal": page_index,
            "kind": "page",
            "page_number": page_index + 1,
            "text": page.extract_text() or "",
        }
        for page_index, page in enumerate(reader.pages)
    )
    if not any(str(block["text"]).strip() for block in blocks):
        raise UnsupportedReferenceError("PDF contains no extractable text")
    return DocumentExtraction(
        "pdf",
        blocks,
        {
            "parser": "pypdf",
            "page_count": classification.page_count,
            "text_page_count": classification.text_page_count,
        },
    )


def _parse_srt(text: str) -> list[dict[str, Any]]:
    normalized = text.replace("\r\n", "\n").replace("\r", "\n").strip()
    cues: list[dict[str, Any]] = []
    for block in re.split(r"\n{2,}", normalized):
        lines = block.splitlines()
        if lines and lines[0].strip().isdigit():
            lines = lines[1:]
        if not lines or "-->" not in lines[0]:
            raise UnsupportedReferenceError("malformed SRT cue")
        start_raw, end_raw = (part.strip().split()[0] for part in lines[0].split("-->", 1))
        cues.append(_cue(len(cues), _parse_clock(start_raw), _parse_clock(end_raw), lines[1:]))
    return cues


def _parse_vtt(text: str) -> list[dict[str, Any]]:
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    blocks = re.split(r"\n{2,}", normalized.strip())
    cues: list[dict[str, Any]] = []
    for block in blocks:
        lines = block.splitlines()
        if not lines or lines[0].lstrip("\ufeff").startswith("WEBVTT"):
            continue
        if lines[0].startswith(("NOTE", "STYLE", "REGION")):
            continue
        if "-->" not in lines[0]:
            lines = lines[1:]
        if not lines or "-->" not in lines[0]:
            raise UnsupportedReferenceError("malformed VTT cue")
        start_raw, end_raw = (part.strip().split()[0] for part in lines[0].split("-->", 1))
        cues.append(_cue(len(cues), _parse_clock(start_raw), _parse_clock(end_raw), lines[1:]))
    return cues


def _parse_ass(text: str) -> list[dict[str, Any]]:
    in_events = False
    fields: list[str] | None = None
    cues: list[dict[str, Any]] = []
    for raw_line in text.replace("\r\n", "\n").replace("\r", "\n").splitlines():
        line = raw_line.strip()
        if line.lower() == "[events]":
            in_events = True
            continue
        if line.startswith("[") and line.endswith("]"):
            in_events = False
            continue
        if not in_events:
            continue
        if line.lower().startswith("format:"):
            fields = [value.strip().lower() for value in line.split(":", 1)[1].split(",")]
            continue
        if not line.lower().startswith("dialogue:"):
            continue
        if fields is None or "start" not in fields or "end" not in fields or "text" not in fields:
            raise UnsupportedReferenceError("ASS Events Format is missing Start, End, or Text")
        values = line.split(":", 1)[1].split(",", len(fields) - 1)
        if len(values) != len(fields):
            raise UnsupportedReferenceError("malformed ASS Dialogue row")
        record = dict(zip(fields, values, strict=True))
        cue_text = record["text"].replace("\\N", "\n").replace("\\n", "\n")
        cues.append(
            _cue(
                len(cues),
                _parse_ass_clock(record["start"]),
                _parse_ass_clock(record["end"]),
                cue_text.splitlines(),
            )
        )
    return cues


def _cue(ordinal: int, start_ms: int, end_ms: int, lines: list[str]) -> dict[str, Any]:
    if end_ms <= start_ms:
        raise UnsupportedReferenceError("subtitle cue must have positive duration")
    text = "\n".join(lines).strip()
    if not text:
        raise UnsupportedReferenceError("subtitle cue text must not be empty")
    return {
        "ordinal": ordinal,
        "kind": "cue",
        "start_ms": start_ms,
        "end_ms": end_ms,
        "text": text,
    }


def _parse_clock(value: str) -> int:
    normalized = value.replace(",", ".")
    parts = normalized.split(":")
    if len(parts) == 2:
        hours = "0"
        minutes, seconds = parts
    elif len(parts) == 3:
        hours, minutes, seconds = parts
    else:
        raise UnsupportedReferenceError(f"invalid subtitle time: {value}")
    try:
        seconds_value = float(seconds)
        total = (int(hours) * 3600 + int(minutes) * 60 + seconds_value) * 1000
    except ValueError as exc:
        raise UnsupportedReferenceError(f"invalid subtitle time: {value}") from exc
    return round(total)


def _parse_ass_clock(value: str) -> int:
    return _parse_clock(value.strip())


def _validate_ooxml_archive(archive: ZipFile) -> None:
    infos = archive.infolist()
    if len(infos) > MAX_OOXML_ENTRIES:
        raise UnsupportedReferenceError("OOXML archive exceeds entry limit")
    expanded = 0
    for info in infos:
        _validate_zip_info(info)
        expanded += info.file_size
        if expanded > MAX_OOXML_EXPANDED_BYTES:
            raise UnsupportedReferenceError("OOXML archive exceeds expanded-byte limit")
    if "[Content_Types].xml" not in {info.filename for info in infos}:
        raise UnsupportedReferenceError("OOXML archive has no content type manifest")


def _validate_zip_info(info: ZipInfo) -> None:
    path = PurePosixPath(info.filename)
    if path.is_absolute() or ".." in path.parts:
        raise UnsupportedReferenceError("OOXML archive contains an unsafe path")
    if info.file_size > MAX_OOXML_ENTRY_BYTES:
        raise UnsupportedReferenceError("OOXML entry exceeds byte limit")


def _xml(archive: ZipFile, name: str) -> ElementTree.Element:
    try:
        raw = archive.read(name)
    except KeyError as exc:
        raise UnsupportedReferenceError(f"OOXML archive is missing {name}") from exc
    try:
        return ElementTree.fromstring(raw)
    except ElementTree.ParseError as exc:
        raise UnsupportedReferenceError(f"OOXML part is malformed: {name}") from exc


def _extract_docx(archive: ZipFile) -> list[dict[str, Any]]:
    names = {info.filename for info in archive.infolist()}
    ordered = ["word/document.xml"] + sorted(
        name
        for name in names
        if re.fullmatch(r"word/(header|footer)\d+\.xml", name)
        or name in {"word/footnotes.xml", "word/endnotes.xml", "word/comments.xml"}
    )
    blocks: list[dict[str, Any]] = []
    for part in ordered:
        if part not in names:
            continue
        root = _xml(archive, part)
        paragraphs = root.findall(f".//{{{WORD_NS}}}p")
        for paragraph in paragraphs:
            text = "".join(node.text or "" for node in paragraph.findall(f".//{{{WORD_NS}}}t"))
            if text.strip():
                blocks.append(
                    {"ordinal": len(blocks), "kind": "paragraph", "part": part, "text": text}
                )
    return blocks


def _extract_pptx(archive: ZipFile) -> list[dict[str, Any]]:
    names = {info.filename for info in archive.infolist()}
    slides = sorted(
        (name for name in names if re.fullmatch(r"ppt/slides/slide\d+\.xml", name)),
        key=_numeric_suffix,
    )
    blocks: list[dict[str, Any]] = []
    for slide_number, part in enumerate(slides, start=1):
        root = _xml(archive, part)
        texts = [node.text or "" for node in root.findall(f".//{{{DRAWING_NS}}}t")]
        text = "\n".join(value for value in texts if value).strip()
        if text:
            blocks.append(
                {
                    "ordinal": len(blocks),
                    "kind": "slide",
                    "slide_number": slide_number,
                    "part": part,
                    "text": text,
                }
            )
        note_part = f"ppt/notesSlides/notesSlide{slide_number}.xml"
        if note_part in names:
            note_root = _xml(archive, note_part)
            note_text = "\n".join(
                node.text or "" for node in note_root.findall(f".//{{{DRAWING_NS}}}t")
            ).strip()
            if note_text:
                blocks.append(
                    {
                        "ordinal": len(blocks),
                        "kind": "notes",
                        "slide_number": slide_number,
                        "part": note_part,
                        "text": note_text,
                    }
                )
    return blocks


def _extract_xlsx(archive: ZipFile) -> list[dict[str, Any]]:
    shared_strings: list[str] = []
    names = {info.filename for info in archive.infolist()}
    if "xl/sharedStrings.xml" in names:
        root = _xml(archive, "xl/sharedStrings.xml")
        shared_strings = [
            "".join(node.text or "" for node in item.findall(f".//{{{SHEET_NS}}}t"))
            for item in root.findall(f".//{{{SHEET_NS}}}si")
        ]
    sheets = sorted(
        (name for name in names if re.fullmatch(r"xl/worksheets/sheet\d+\.xml", name)),
        key=_numeric_suffix,
    )
    blocks: list[dict[str, Any]] = []
    for sheet_number, part in enumerate(sheets, start=1):
        root = _xml(archive, part)
        cells: list[dict[str, Any]] = []
        for cell in root.findall(f".//{{{SHEET_NS}}}c"):
            address = cell.get("r") or ""
            cell_type = cell.get("t")
            formula = cell.findtext(f"{{{SHEET_NS}}}f")
            raw_value = cell.findtext(f"{{{SHEET_NS}}}v")
            inline = "".join(
                node.text or "" for node in cell.findall(f".//{{{SHEET_NS}}}t")
            )
            value: str | None = inline or raw_value
            if cell_type == "s" and raw_value is not None:
                try:
                    value = shared_strings[int(raw_value)]
                except (IndexError, ValueError) as exc:
                    raise UnsupportedReferenceError("XLSX shared string index is invalid") from exc
            if value is not None or formula is not None:
                cells.append(
                    {"address": address, "value": value, "formula": formula, "type": cell_type}
                )
        if cells:
            blocks.append(
                {
                    "ordinal": len(blocks),
                    "kind": "sheet",
                    "sheet_number": sheet_number,
                    "part": part,
                    "cells": cells,
                }
            )
    return blocks


def _numeric_suffix(value: str) -> int:
    match = re.search(r"(\d+)\.xml$", value)
    return int(match.group(1)) if match else 0


def _pdf_reader(path: Path) -> Any:
    try:
        from pypdf import PdfReader
    except ImportError as exc:  # pragma: no cover - core installation contract
        raise UnsupportedReferenceError("pypdf is required for PDF References") from exc
    try:
        reader = PdfReader(path, strict=True)
    except Exception as exc:
        raise UnsupportedReferenceError("PDF is corrupt or cannot be reliably parsed") from exc
    if reader.is_encrypted:
        raise UnsupportedReferenceError("encrypted PDF References are unsupported")
    return reader
