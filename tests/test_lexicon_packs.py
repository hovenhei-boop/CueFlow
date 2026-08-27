from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from cueflow.canonical import hash_json
from cueflow.errors import ContractError, IntegrityError
from cueflow.lexicon_packs import PACK_SCHEMA_VERSION, OfficialPackStore


def _pack(
    root: Path,
    pack_id: str,
    domain: str,
    version: str,
    terms: list[dict[str, Any]],
) -> tuple[Path, dict[str, str]]:
    manifest = {
        "schema_version": PACK_SCHEMA_VERSION,
        "pack_id": pack_id,
        "domain": domain,
        "version": version,
        "license": {"name": "CC0-1.0", "url": "https://creativecommons.org/publicdomain/zero/1.0/"},
        "term_count": len(terms),
        "terms_hash": hash_json({"terms": terms}),
    }
    descriptor = root / f"{pack_id}-{version}.json"
    descriptor.write_text(
        json.dumps({"manifest": manifest, "terms": terms}, ensure_ascii=False),
        encoding="utf-8",
    )
    return descriptor, {
        "pack_id": pack_id,
        "domain": domain,
        "version": version,
        "source": str(descriptor),
        "manifest_hash": hash_json(manifest),
    }


def _catalog(path: Path, packs: list[dict[str, str]]) -> Path:
    path.write_text(
        json.dumps({"schema_version": PACK_SCHEMA_VERSION, "packs": packs}),
        encoding="utf-8",
    )
    return path


def test_explicit_setup_defaults_to_all_domains_and_projects_share_store(tmp_path: Path) -> None:
    first_descriptor, first = _pack(
        tmp_path,
        "ai-software",
        "technology",
        "1.0.0",
        [
            {
                "term": "CueFlow",
                "category": "proper_noun",
                "proper_noun_subtype": "product_brand_model_software",
            }
        ],
    )
    assert first_descriptor.is_file()
    _second_descriptor, second = _pack(
        tmp_path,
        "culture-humanities",
        "culture",
        "1.0.0",
        [{"term": "史记", "category": "proper_noun", "proper_noun_subtype": "work_or_title"}],
    )
    catalog = _catalog(tmp_path / "catalog.json", [first, second])
    store = OfficialPackStore(tmp_path / "app-data")

    result = store.setup(catalog)
    assert {row["pack_id"] for row in result["installed"]} == {
        "ai-software",
        "culture-humanities",
    }
    assert {row["status"] for row in store.status()["packs"]} == {"ready"}
    assert {row["term"] for row in store.installed_terms()} == {"CueFlow", "史记"}

    same_global_store = OfficialPackStore(tmp_path / "app-data")
    assert same_global_store.installed_terms() == store.installed_terms()
    assert store.uninstall(["culture-humanities"])["uninstalled"] == [
        "culture-humanities"
    ]
    assert [row["pack_id"] for row in store.status()["packs"]] == ["ai-software"]


def test_domain_selection_update_integrity_and_repair(tmp_path: Path) -> None:
    _descriptor, v1 = _pack(
        tmp_path,
        "ai-software",
        "technology",
        "1.0.0",
        [{"term": "ASR", "category": "noun_or_term", "proper_noun_subtype": None}],
    )
    _other_descriptor, other = _pack(
        tmp_path,
        "culture-humanities",
        "culture",
        "1.0.0",
        [{"term": "诗经", "category": "proper_noun", "proper_noun_subtype": "work_or_title"}],
    )
    catalog = _catalog(tmp_path / "catalog.json", [v1, other])
    store = OfficialPackStore(tmp_path / "app-data")
    installed = store.setup(catalog, domains=["technology"])
    assert [row["pack_id"] for row in installed["installed"]] == ["ai-software"]

    _v2_descriptor, v2 = _pack(
        tmp_path,
        "ai-software",
        "technology",
        "1.1.0",
        [{"term": "Qwen-ASR", "category": "noun_or_term", "proper_noun_subtype": None}],
    )
    updated_catalog = _catalog(tmp_path / "catalog-v2.json", [v2, other])
    update = store.update(catalog=updated_catalog)
    assert update["updated"] == [
        {"pack_id": "ai-software", "from_version": "1.0.0", "to_version": "1.1.0"}
    ]
    with pytest.raises(ContractError, match="downgrade"):
        store.update(catalog=catalog)

    installed_terms = store.root / "versions" / "ai-software" / "1.1.0" / "terms.json"
    installed_terms.write_text("[]", encoding="utf-8")
    assert store.status()["packs"][0]["status"] == "invalid"
    assert store.repair()["repaired"] == [
        {"pack_id": "ai-software", "version": "1.1.0"}
    ]
    assert store.status()["packs"][0]["status"] == "ready"

    descriptor_value = json.loads(_v2_descriptor.read_text(encoding="utf-8"))
    descriptor_value["terms"][0]["term"] = "tampered"
    bad_descriptor = tmp_path / "bad.json"
    bad_descriptor.write_text(json.dumps(descriptor_value), encoding="utf-8")
    bad_item = dict(v2)
    bad_item["source"] = str(bad_descriptor)
    bad_item["pack_id"] = "bad-pack"
    bad_catalog = _catalog(tmp_path / "bad-catalog.json", [bad_item])
    with pytest.raises((ContractError, IntegrityError)):
        OfficialPackStore(tmp_path / "bad-store").setup(bad_catalog)


def test_pack_store_requires_explicit_setup_and_honors_lock(tmp_path: Path) -> None:
    store = OfficialPackStore(tmp_path / "app-data")
    with pytest.raises(IntegrityError, match="explicit pack setup"):
        store.list_packs()
    store.root.mkdir(parents=True)
    (store.root / ".lock").write_text("busy", encoding="ascii")
    with pytest.raises(IntegrityError, match="locked"):
        store.uninstall(["ai-software"])
