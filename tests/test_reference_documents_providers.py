from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from zipfile import ZIP_DEFLATED, ZipFile

import pytest

import cueflow.reference_documents as reference_documents
import cueflow.reference_orchestrator as reference_orchestrator
from cueflow.canonical import hash_json
from cueflow.errors import (
    ContractError,
    ProviderCleanupError,
    ProviderFormatError,
    ProviderIdentityError,
    ProviderPermissionError,
)
from cueflow.reference_documents import (
    PdfClassification,
    classify_pdf,
    extract_ooxml,
)
from cueflow.reference_providers import (
    CloudDocumentRequest,
    CloudReferenceAsr,
    QwenCloudDocumentParser,
    ReferenceAsrRequest,
)
from cueflow.schema import ArtifactEnvelope, InputRef, Producer


def _producer() -> Producer:
    return Producer(
        component="reference-test",
        component_version="0.2.1",
        provider="fixture",
        model="fixture",
        config_hash=hash_json({"fixture": True}),
    )


def test_ooxml_uses_stdlib_zip_xml_for_docx_pptx_and_xlsx(tmp_path: Path) -> None:
    fixtures = {
        "docx": {
            "[Content_Types].xml": "<Types/>",
            "word/document.xml": (
                '<w:document xmlns:w="http://schemas.openxmlformats.org/'
                'wordprocessingml/2006/main"><w:p><w:r><w:t>DOCX text</w:t>'
                "</w:r></w:p></w:document>"
            ),
        },
        "pptx": {
            "[Content_Types].xml": "<Types/>",
            "ppt/presentation.xml": "<presentation/>",
            "ppt/slides/slide1.xml": (
                '<p:sld xmlns:p="http://schemas.openxmlformats.org/'
                'presentationml/2006/main" xmlns:a="http://schemas.openxmlformats.org/'
                'drawingml/2006/main"><a:t>PPTX text</a:t></p:sld>'
            ),
        },
        "xlsx": {
            "[Content_Types].xml": "<Types/>",
            "xl/workbook.xml": "<workbook/>",
            "xl/sharedStrings.xml": (
                '<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
                "<si><t>XLSX text</t></si></sst>"
            ),
            "xl/worksheets/sheet1.xml": (
                '<worksheet xmlns="http://schemas.openxmlformats.org/'
                'spreadsheetml/2006/main"><sheetData><row><c r="A1" t="s">'
                "<v>0</v></c></row></sheetData></worksheet>"
            ),
        },
    }
    for detected_format, parts in fixtures.items():
        path = tmp_path / f"fixture.{detected_format}"
        with ZipFile(path, "w", ZIP_DEFLATED) as archive:
            for name, value in parts.items():
                archive.writestr(name, value)
        extraction = extract_ooxml(path, detected_format)
        assert extraction.metadata["parser"] == "stdlib-zip-xml"
        assert detected_format.upper() + " text" in json.dumps(extraction.content())


def test_mixed_pdf_routes_entire_document_to_cloud_and_never_page_vision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class Contents:
        def __init__(self, raw: bytes) -> None:
            self.raw = raw

        def get_data(self) -> bytes:
            return self.raw

    class Page:
        def __init__(self, text: str, raw: bytes) -> None:
            self.text = text
            self.raw = raw

        def extract_text(self) -> str:
            return self.text

        def get_contents(self) -> Contents:
            return Contents(self.raw)

    reader = SimpleNamespace(
        pages=[Page("text-layer page", b"BT text ET"), Page("", b"q /Image Do Q")]
    )
    monkeypatch.setattr(reference_documents, "_pdf_reader", lambda _path: reader)
    classification = classify_pdf(tmp_path / "mixed.pdf")
    assert classification == PdfClassification("cloud_document_parse", 2, 1, (2,))

    monkeypatch.setattr(reference_orchestrator, "classify_pdf", lambda _path: classification)
    cloud = reference_orchestrator._plan_work(
        tmp_path / "mixed.pdf",
        detected_format="pdf",
        media_category="document",
        pixel_subtitle_mode=None,
        runtime=SimpleNamespace(),
    )
    assert [(item.kind, item.evidence_role) for item in cloud] == [
        ("cloud_document", "cloud_document_parse")
    ]
    assert all(item.evidence_role != "image_visual" for item in cloud)


@pytest.mark.parametrize("detected_format", ["doc", "ppt", "xls", "png", "jpeg", "webp"])
def test_document_and_image_routes_use_remote_work_items(
    tmp_path: Path, detected_format: str
) -> None:
    image = detected_format in {"png", "jpeg", "webp"}
    specs = reference_orchestrator._plan_work(
        tmp_path / f"reference.{detected_format}",
        detected_format=detected_format,
        media_category="image" if image else "document",
        pixel_subtitle_mode=None,
        runtime=SimpleNamespace(),
    )
    assert [(item.kind, item.evidence_role) for item in specs] == [
        ("image_vision", "image_visual") if image else ("cloud_document", "cloud_document_parse")
    ]


class _StatusError(Exception):
    def __init__(self, status_code: int, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code


class _Files:
    def __init__(
        self,
        *,
        upload_error: Exception | None = None,
        state: str = "processed",
        delete_ok: bool = True,
    ) -> None:
        self.upload_error = upload_error
        self.state = state
        self.delete_ok = delete_ok
        self.deleted: list[str] = []

    def create(self, *, file: Any, purpose: str) -> SimpleNamespace:
        assert purpose == "file-extract"
        assert file.read(1)
        if self.upload_error is not None:
            raise self.upload_error
        return SimpleNamespace(id="file-fixture")

    def retrieve(self, file_id: str) -> SimpleNamespace:
        assert file_id == "file-fixture"
        return SimpleNamespace(status=self.state)

    def delete(self, file_id: str) -> SimpleNamespace:
        self.deleted.append(file_id)
        if not self.delete_ok:
            raise RuntimeError("delete failed")
        return SimpleNamespace(deleted=True)


class _Chat:
    def __init__(self, error: Exception | None = None) -> None:
        self.completions = self
        self.error = error

    def create(self, **kwargs: Any) -> SimpleNamespace:
        assert kwargs["model"] == "qwen-doc-turbo"
        assert kwargs["stream"] is False
        if self.error is not None:
            raise self.error
        return SimpleNamespace(
            id="response-fixture",
            choices=[SimpleNamespace(message=SimpleNamespace(content="document text"))],
            usage=SimpleNamespace(
                model_dump=lambda: {"prompt_tokens": 3, "completion_tokens": 2}
            ),
        )


def _document_parser(files: _Files, chat_error: Exception | None = None) -> QwenCloudDocumentParser:
    client = SimpleNamespace(files=files, chat=_Chat(chat_error))
    return QwenCloudDocumentParser(
        client_factory=lambda **_kwargs: client,
        sleep=lambda _seconds: None,
        monotonic=lambda: 0.0,
    )


def test_cloud_document_parse_deletes_file_id_and_preserves_null_cost(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DASHSCOPE_API_KEY", "fixture-key")
    monkeypatch.setenv("DASHSCOPE_BASE_URL", "https://fixture.invalid/v1")
    path = tmp_path / "legacy.doc"
    path.write_bytes(b"legacy")
    files = _Files()
    parser = _document_parser(files)
    result = parser.parse(CloudDocumentRequest(path, "doc"))
    assert result.text == "document text"
    assert result.provider_usage == {"prompt_tokens": 3, "completion_tokens": 2}
    assert result.provider_cost is None
    assert files.deleted == ["file-fixture"]
    assert parser.last_cleanup_status == "deleted"


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (_StatusError(401, "bad key"), ProviderIdentityError),
        (_StatusError(403, "forbidden"), ProviderPermissionError),
    ],
)
def test_cloud_document_distinguishes_identity_and_permission_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    error: Exception,
    expected: type[Exception],
) -> None:
    monkeypatch.setenv("DASHSCOPE_API_KEY", "fixture-key")
    monkeypatch.setenv("DASHSCOPE_BASE_URL", "https://fixture.invalid/v1")
    path = tmp_path / "legacy.ppt"
    path.write_bytes(b"legacy")
    files = _Files()
    parser = _document_parser(files, error)
    with pytest.raises(expected):
        parser.parse(CloudDocumentRequest(path, "ppt"))
    assert files.deleted == ["file-fixture"]


def test_cloud_document_format_error_and_cleanup_failure_are_explicit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DASHSCOPE_API_KEY", "fixture-key")
    monkeypatch.setenv("DASHSCOPE_BASE_URL", "https://fixture.invalid/v1")
    path = tmp_path / "legacy.xls"
    path.write_bytes(b"legacy")
    rejected = _Files(state="failed")
    with pytest.raises(ProviderFormatError):
        _document_parser(rejected).parse(CloudDocumentRequest(path, "xls"))
    assert rejected.deleted == ["file-fixture"]

    cleanup_failure = _Files(delete_ok=False)
    cleanup_parser = _document_parser(cleanup_failure)
    with pytest.raises(ProviderCleanupError):
        cleanup_parser.parse(CloudDocumentRequest(path, "xls"))
    assert cleanup_parser.last_result is not None
    assert cleanup_parser.last_result.response_id == "response-fixture"


def test_cloud_asr_keeps_local_and_provider_durations_semantically_separate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DASHSCOPE_API_KEY", "fixture-key")
    audio = tmp_path / "segment.wav"
    audio.write_bytes(b"RIFF fixture PCM/WAV")

    def post_json(
        _url: str, _headers: Any, body: bytes, _timeout: float
    ) -> tuple[int, bytes]:
        request = json.loads(body)
        assert request["parameters"]["format"] == "wav"
        assert request["parameters"]["sample_rate"] == 16_000
        assert "audio/opus" not in body.decode("utf-8")
        return 200, json.dumps(
            {
                "request_id": "asr-fixture",
                "output": {
                    "text": "Level 2",
                    "segments": [{"start": 0.5, "end": 2.0, "text": "Level 2"}],
                },
                "usage": {"duration": 35},
            }
        ).encode()

    result = CloudReferenceAsr(post_json=post_json).transcribe(
        ReferenceAsrRequest(audio, 10_000, 50_054)
    )
    assert result.provider_usage_duration == 35
    assert result.segments == ({"start_ms": 10_500, "end_ms": 12_000, "text": "Level 2"},)

    valid_payload = {
        "reference_asset_id": "ref_fixture",
        "run_id": "run_fixture",
        "work_item_id": "rwi_fixture",
        "evidence_role": "cloud_reference_asr",
        "branch": "cloud_reference_asr",
        "content": {"text": result.text, "segments": list(result.segments)},
        "provenance": {"source_start_ms": 10_000, "source_end_ms": 50_054},
        "local_measured_duration": 40.054,
        "provider_usage_duration": result.provider_usage_duration,
        "provider_usage": dict(result.provider_usage or {}),
        "provider_cost": None,
    }
    envelope = ArtifactEnvelope.create(
        artifact_kind="reference_evidence",
        scope_key="ref_fixture",
        producer=_producer(),
        inputs=(InputRef(role="reference_asset", reference_asset_id="ref_fixture"),),
        payload=valid_payload,
    )
    assert envelope.payload["local_measured_duration"] == 40.054
    assert envelope.payload["provider_usage_duration"] == 35
    with pytest.raises(ContractError, match="must not use media_duration"):
        ArtifactEnvelope.create(
            artifact_kind="reference_evidence",
            scope_key="ref_fixture",
            producer=_producer(),
            inputs=(InputRef(role="reference_asset", reference_asset_id="ref_fixture"),),
            payload={**valid_payload, "media_duration": 35},
        )
