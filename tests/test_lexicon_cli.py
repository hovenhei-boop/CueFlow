from __future__ import annotations

import json
from pathlib import Path

import pytest

from cueflow.canonical import hash_json
from cueflow.cli import build_parser, main
from cueflow.lexicon_packs import PACK_SCHEMA_VERSION


def test_project_lexicon_cli_and_structured_blacklist_conflict(
    tmp_path: Path, capfd: pytest.CaptureFixture[str]
) -> None:
    project = tmp_path / "project"
    assert main(["init", str(project), "--name", "Lexicon CLI"]) == 0
    capfd.readouterr()

    assert (
        main(
            [
                "lexicon",
                "blacklist",
                "add",
                str(project),
                "CueFlow",
                "--permanent",
            ]
        )
        == 0
    )
    blacklist = json.loads(capfd.readouterr().out)
    assert blacklist["status"] == "blacklisted"

    assert (
        main(
            [
                "lexicon",
                "entry",
                "add",
                str(project),
                "CueFlow",
                "--category",
                "proper_noun",
                "--proper-noun-subtype",
                "product_brand_model_software",
            ]
        )
        == 2
    )
    conflict = json.loads(capfd.readouterr().err)
    assert conflict["conflicts"] == ["blacklist"]
    assert conflict["choices"] == ["unblock_and_add", "cancel"]

    assert (
        main(
            [
                "lexicon",
                "entry",
                "add",
                str(project),
                "CueFlow",
                "--category",
                "proper_noun",
                "--proper-noun-subtype",
                "product_brand_model_software",
                "--blacklist-policy",
                "unblock_and_add",
            ]
        )
        == 0
    )
    added = json.loads(capfd.readouterr().out)
    assert main(["lexicon", "entry", "list", str(project)]) == 0
    entry = json.loads(capfd.readouterr().out)["entries"][0]
    assert entry["entry_id"] == added["entry_id"]

    assert (
        main(
            [
                "lexicon",
                "entry",
                "disable",
                str(project),
                entry["entry_id"],
                "--expected-revision",
                str(entry["revision"]),
            ]
        )
        == 0
    )
    capfd.readouterr()
    assert (
        main(
            [
                "lexicon",
                "entry",
                "block",
                str(project),
                entry["entry_id"],
                "--temporary",
                "15",
                "--expected-revision",
                str(entry["revision"] + 1),
            ]
        )
        == 0
    )
    blocked = json.loads(capfd.readouterr().out)
    assert blocked["kind"] == "temporary"
    assert main(["lexicon", "blacklist", "list", str(project), "--kind", "temporary"]) == 0
    rows = json.loads(capfd.readouterr().out)["blacklist"]
    assert [row["blacklist_id"] for row in rows] == [blocked["blacklist_id"]]


def test_official_pack_cli_is_global_and_setup_is_explicit(
    tmp_path: Path,
    capfd: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CUEFLOW_DATA_HOME", str(tmp_path / "app-data"))
    terms = [
        {
            "term": "Qwen-ASR",
            "category": "proper_noun",
            "proper_noun_subtype": "product_brand_model_software",
        }
    ]
    manifest = {
        "schema_version": PACK_SCHEMA_VERSION,
        "pack_id": "ai-software",
        "domain": "technology",
        "version": "1.0.0",
        "license": {"name": "CC0-1.0", "url": "https://example.test/license"},
        "term_count": 1,
        "terms_hash": hash_json({"terms": terms}),
    }
    descriptor = tmp_path / "pack.json"
    descriptor.write_text(
        json.dumps({"manifest": manifest, "terms": terms}), encoding="utf-8"
    )
    catalog = tmp_path / "catalog.json"
    catalog.write_text(
        json.dumps(
            {
                "schema_version": PACK_SCHEMA_VERSION,
                "packs": [
                    {
                        "pack_id": "ai-software",
                        "domain": "technology",
                        "version": "1.0.0",
                        "source": str(descriptor),
                        "manifest_hash": hash_json(manifest),
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    assert main(["lexicon", "pack", "status"]) == 0
    assert json.loads(capfd.readouterr().out)["packs"] == []
    assert main(["lexicon", "pack", "setup", str(catalog)]) == 0
    setup = json.loads(capfd.readouterr().out)
    assert setup["installed"] == [{"pack_id": "ai-software", "version": "1.0.0"}]
    assert main(["lexicon", "pack", "status"]) == 0
    assert json.loads(capfd.readouterr().out)["packs"][0]["status"] == "ready"


def test_lexicon_cli_exposes_no_manual_build_or_project_pack_select() -> None:
    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["lexicon", "build", "project"])
    with pytest.raises(SystemExit):
        parser.parse_args(["lexicon", "pack", "select", "project", "ai-software"])
    with pytest.raises(SystemExit):
        parser.parse_args(["lexicon", "trash", "list", "project"])
    with pytest.raises(SystemExit):
        parser.parse_args(
            [
                "lexicon",
                "entry",
                "delete",
                "project",
                "entry-id",
                "--expected-revision",
                "1",
            ]
        )
    with pytest.raises(SystemExit):
        parser.parse_args(
            [
                "lexicon",
                "suggestions",
                "review",
                "project",
                "candidate-id",
                "--action",
                "reject",
                "--expected-revision",
                "1",
            ]
        )


def test_migrate_cli_is_explicit_and_idempotent(
    tmp_path: Path, capfd: pytest.CaptureFixture[str]
) -> None:
    project = tmp_path / "project"
    assert main(["init", str(project), "--name", "Migrate CLI"]) == 0
    capfd.readouterr()
    assert main(["migrate", str(project)]) == 0
    result = json.loads(capfd.readouterr().out)
    assert result["status"] == "already_current"
    assert result["from_version"] == result["to_version"] == 5
