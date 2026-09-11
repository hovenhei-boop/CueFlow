from __future__ import annotations

from types import MappingProxyType
from typing import Final

from cueflow.errors import ContractError

ARTIFACT_KINDS: Final = frozenset(
    {
        "job_input",
        "media_probe",
        "timeline_audio",
        "media_object",
        "base_asr",
        "peer_asr",
        "asr_comparison",
        "review_resolution",
        "correction_transcript",
        "merge_plan",
        "selection_batch",
        "selection_result",
        "edit_resolution",
        "review_queue",
        "transcript",
        "ata_response",
        "ata_result",
        "srt_render",
    }
)

# These are semantic producer versions, not the CueFlow application version.  The initial
# values intentionally preserve the byte-identical v0.5.4 Producer projection.
ARTIFACT_PRODUCER_VERSIONS: Final = MappingProxyType(
    {
        "job_input": "0.5.4",
        "media_probe": "0.5.4",
        "timeline_audio": "0.5.4",
        "media_object": "0.5.4",
        "base_asr": "0.5.4",
        "peer_asr": "0.5.4",
        "asr_comparison": "0.5.4",
        "review_resolution": "0.5.4",
        "correction_transcript": "0.5.4",
        "merge_plan": "0.5.4",
        "selection_batch": "0.5.4",
        "selection_result": "0.5.4",
        "edit_resolution": "0.5.4",
        "review_queue": "0.5.4",
        "transcript": "0.5.4",
        "ata_response": "0.5.4",
        "ata_result": "0.5.4",
        "srt_render": "0.5.4",
    }
)


def artifact_producer_version(artifact_kind: str) -> str:
    try:
        return ARTIFACT_PRODUCER_VERSIONS[artifact_kind]
    except KeyError as exc:
        raise ContractError(f"missing Artifact producer version for kind: {artifact_kind}") from exc
