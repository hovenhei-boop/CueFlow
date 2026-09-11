from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import tempfile
from collections.abc import Callable, Sequence
from dataclasses import asdict
from pathlib import Path
from typing import Any

from cueflow.errors import ContractError, ProviderError, UnsupportedReferenceError
from cueflow.job_inputs import OFFICE_FORMATS, ReferenceSpec
from cueflow.media_object_store import MediaObjectRef, MediaObjectStore, _hash_file
from cueflow.object_storage import bind_object, persist_object
from cueflow.project import RunContext
from cueflow.schema import TEXT_REFERENCE_FORMATS


def capture_references(context: RunContext, references: Sequence[ReferenceSpec]) -> None:
    """Capture once before execution. A caller's file is never deleted or reread on retry."""
    for ordinal, spec in enumerate(references):
        if spec.kind not in {"file", "text_file"}:
            raise ContractError(
                "Reference inputs must be files; caller-provided URLs are unsupported"
            )
        path = Path(spec.value)
        destination = context.root / "temp" / "inputs" / str(ordinal) / path.name
        destination.parent.mkdir(parents=True, exist_ok=True)
        try:
            shutil.copyfile(path, destination)
            digest, size = _hash_file(destination)
        except (OSError, ContractError):
            # A file that could not be captured cannot silently acquire new bytes on retry.
            destination.write_bytes(b"")
            digest, size = "sha256:" + hashlib.sha256(b"").hexdigest(), 0
        with context.registry.transaction() as tx:
            tx.execute(
                "INSERT INTO run_inputs VALUES (?, ?, 'reference', ?, ?, ?, ?, NULL)",
                (context.run_id, ordinal, path.name, digest, size, str(destination.resolve())),
            )


def office_to_pdf(
    source: Path, directory: Path, *, executable: str | None = None, timeout_seconds: float = 120
) -> Path:
    chosen = executable or shutil.which("soffice") or shutil.which("libreoffice")
    if not chosen:
        raise UnsupportedReferenceError("LibreOffice is unavailable")
    profile = directory / "profile"
    output = directory / "converted"
    output.mkdir()
    try:
        result = subprocess.run(
            [
                chosen,
                f"-env:UserInstallation={profile.resolve().as_uri()}",
                "--headless",
                "--convert-to",
                "pdf",
                "--outdir",
                str(output),
                str(source),
            ],
            timeout=timeout_seconds,
            capture_output=True,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise UnsupportedReferenceError("Office conversion unavailable or timed out") from exc
    destination = output / (source.stem + ".pdf")
    if result.returncode != 0:
        raise UnsupportedReferenceError("Office conversion failed")
    validate_pdf(destination)
    return destination


def validate_pdf(path: Path) -> None:
    try:
        with path.open("rb") as stream:
            header = stream.read(1024)
            stream.seek(max(0, path.stat().st_size - 2048))
            trailer = stream.read()
        if b"%PDF-" not in header or b"%%EOF" not in trailer or b"/Encrypt" in trailer:
            raise UnsupportedReferenceError("Reference PDF is empty, damaged or encrypted")
    except OSError as exc:
        raise UnsupportedReferenceError("Reference PDF is unavailable") from exc


def prepare_references(
    context: RunContext,
    factory: Callable[[], MediaObjectStore],
    *,
    converter: Callable[[Path, Path], Path] | None = None,
) -> list[dict[str, Any]]:
    from cueflow.lifecycle import check_cancellation

    registry, run_id = context.registry, context.run_id
    number = registry.round_number(run_id)
    result: list[dict[str, Any]] = []
    rows = registry.connection.execute(
        "SELECT * FROM run_inputs WHERE run_id=? AND kind='reference' ORDER BY ordinal",
        (run_id,),
    ).fetchall()
    for row in rows:
        check_cancellation(context)
        ordinal = int(row["ordinal"])
        current = registry.connection.execute(
            """SELECT * FROM reference_preparations WHERE run_id=? AND ordinal=?
               AND execution_round<=? ORDER BY execution_round DESC LIMIT 1""",
            (run_id, ordinal, number),
        ).fetchone()
        # A prepared result is immutable. Failures are attempted once per user retry round.
        if current and (current["status"] == "available" or current["execution_round"] == number):
            prepared = json.loads(current["prepared_json"]) if current["prepared_json"] else None
            error = current["error_message"]
        else:
            prepared, error = None, None
            store = factory()
            try:
                if row["byte_length"] == 0:
                    raise UnsupportedReferenceError("original Reference was empty or unavailable")
                with tempfile.TemporaryDirectory(dir=context.store.temp_root) as temp:
                    directory = Path(temp)
                    source = directory / str(row["display_name"])
                    if row["object_json"]:
                        ref = MediaObjectRef(**json.loads(row["object_json"]))
                        store.materialize(ref, source)
                    else:
                        original = Path(row["local_path"])
                        if _hash_file(original) != (row["content_hash"], row["byte_length"]):
                            raise ContractError("captured Reference identity changed")
                        ref = persist_object(
                            context, store, original, f"reference-source:{ordinal}"
                        )
                        # Bind first, then remove only this Run's owned staging file.
                        with registry.transaction() as tx:
                            tx.execute(
                                "UPDATE run_inputs SET object_json=?, local_path=NULL "
                                "WHERE run_id=? AND ordinal=?",
                                (json.dumps(asdict(ref)), run_id, ordinal),
                            )
                        bind_object(context, ref)
                        shutil.copyfile(original, source)
                        original.unlink()
                    prepared = _prepare_one(
                        context, store, source, directory, ordinal, converter or office_to_pdf, ref
                    )
            except (UnsupportedReferenceError, ProviderError, UnicodeError, OSError) as exc:
                # SQLite/integrity/cancellation failures deliberately escape this boundary.
                error = str(exc)
            finally:
                store.close()
        with registry.transaction() as tx:
            tx.execute(
                """INSERT INTO reference_preparations VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT(run_id, execution_round, ordinal) DO NOTHING""",
                (
                    run_id,
                    number,
                    ordinal,
                    "available" if prepared is not None else "unavailable",
                    json.dumps(prepared, ensure_ascii=False) if prepared is not None else None,
                    error,
                ),
            )
        if prepared is not None:
            # Effective ordinal is dense; original identity remains separately recorded.
            result.append({**prepared, "ordinal": len(result), "input_ordinal": ordinal})
    return result


def _prepare_one(
    context: RunContext,
    store: MediaObjectStore,
    source: Path,
    directory: Path,
    ordinal: int,
    converter: Callable[[Path, Path], Path],
    source_object: MediaObjectRef,
) -> dict[str, Any]:
    suffix = source.suffix.lower().lstrip(".")
    common: dict[str, Any] = {"display_name": source.name}
    if suffix in TEXT_REFERENCE_FORMATS:
        text = source.read_text(encoding="utf-8-sig")
        if not text:
            raise UnsupportedReferenceError("Reference text is empty")
        return {**common, "kind": "text", "format": suffix, "text": text}
    if suffix in OFFICE_FORMATS:
        source = converter(source, directory)
        suffix = "pdf"
        source_object = persist_object(context, store, source, f"reference-prepared:{ordinal}")
    if suffix == "pdf":
        validate_pdf(source)
    elif suffix not in {"png", "jpg", "jpeg", "webp"}:
        raise UnsupportedReferenceError("unsupported Reference file format")
    ref = source_object
    bind_object(context, ref)
    return {
        **common,
        "kind": "pdf_object" if suffix == "pdf" else "image_object",
        "object": asdict(ref),
    }


def resolve_reference_urls(
    references: Sequence[dict[str, Any]],
    store: MediaObjectStore,
) -> tuple[dict[str, Any], ...]:
    result = []
    for reference in references:
        if reference["kind"] in {"pdf_object", "image_object"}:
            result.append(
                {
                    **reference,
                    "kind": reference["kind"].replace("_object", "_url"),
                    "url": store.presign_get(MediaObjectRef(**reference["object"])),
                }
            )
        else:
            result.append(dict(reference))
    return tuple(result)
