from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
import urllib.parse
import urllib.request
import uuid
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any, cast

from cueflow.canonical import hash_json
from cueflow.errors import ContractError, IntegrityError
from cueflow.term_candidates import normalize_surface_form, validate_category

PACK_SCHEMA_VERSION = "1.0.0"
_IDENTIFIER = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
_VERSION = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$")


def pack_data_root() -> Path:
    configured = os.getenv("CUEFLOW_DATA_HOME")
    if configured:
        return Path(configured).expanduser().resolve() / "lexicon-packs"
    local_app_data = os.getenv("LOCALAPPDATA")
    if local_app_data:
        return Path(local_app_data).resolve() / "CueFlow" / "lexicon-packs"
    return Path.home() / ".local" / "share" / "CueFlow" / "lexicon-packs"


class OfficialPackStore:
    """Application-global, immutable official Lexicon Pack storage."""

    def __init__(self, root: Path | None = None) -> None:
        self.root = (root or pack_data_root()).resolve()
        self.versions_root = self.root / "versions"
        self.temp_root = self.root / "tmp"
        self.catalog_path = self.root / "catalog.json"
        self.current_path = self.root / "current.json"

    def list_packs(self, catalog: Path | None = None) -> dict[str, Any]:
        catalog_value = self._catalog(catalog)
        installed = self._current()
        return {
            "schema_version": PACK_SCHEMA_VERSION,
            "packs": [
                {
                    "pack_id": item["pack_id"],
                    "domain": item["domain"],
                    "catalog_version": item["version"],
                    "installed_version": installed.get(str(item["pack_id"])),
                    "installed": str(item["pack_id"]) in installed,
                }
                for item in catalog_value["packs"]
            ],
        }

    def setup(
        self,
        catalog: Path,
        *,
        domains: Sequence[str] | None = None,
    ) -> dict[str, Any]:
        """Persist a catalog and explicitly install all domains by default."""
        return self.install(catalog=catalog, domains=domains)

    def install(
        self,
        *,
        catalog: Path | None = None,
        pack_ids: Sequence[str] | None = None,
        domains: Sequence[str] | None = None,
    ) -> dict[str, Any]:
        if pack_ids is not None and domains is not None:
            raise ContractError("choose Pack IDs or domains, not both")
        self._ensure_layout()
        with self._lock():
            catalog_value = self._catalog(catalog, persist=catalog is not None)
            selected = _select_catalog_packs(catalog_value, pack_ids=pack_ids, domains=domains)
            current = self._current()
            installed: list[dict[str, str]] = []
            for item in selected:
                pack_id = str(item["pack_id"])
                version = str(item["version"])
                self._install_catalog_item(item, catalog_base=self.catalog_path.parent)
                current[pack_id] = version
                installed.append({"pack_id": pack_id, "version": version})
            self._write_current(current)
        return {"status": "installed", "installed": installed}

    def uninstall(self, pack_ids: Sequence[str]) -> dict[str, Any]:
        if not pack_ids:
            raise ContractError("Pack uninstall requires at least one Pack ID")
        self._ensure_layout()
        with self._lock():
            current = self._current()
            removed = [pack_id for pack_id in pack_ids if current.pop(pack_id, None) is not None]
            self._write_current(current)
        return {"status": "uninstalled", "uninstalled": removed}

    def update(self, *, catalog: Path | None = None) -> dict[str, Any]:
        self._ensure_layout()
        with self._lock():
            catalog_value = self._catalog(catalog, persist=False)
            current = self._current()
            by_id = {str(item["pack_id"]): item for item in catalog_value["packs"]}
            missing = set(current).difference(by_id)
            if missing:
                raise ContractError(
                    f"updated catalog omits installed Official Packs: {sorted(missing)}"
                )
            downgraded = [
                pack_id
                for pack_id, old_version in current.items()
                if _version_key(str(by_id[pack_id]["version"]))
                < _version_key(old_version)
            ]
            if downgraded:
                raise ContractError(
                    f"updated catalog would downgrade Official Packs: {sorted(downgraded)}"
                )
            if catalog is not None:
                self._atomic_json(self.catalog_path, catalog_value)
            updated: list[dict[str, str]] = []
            for pack_id, old_version in sorted(current.items()):
                item = by_id.get(pack_id)
                if item is None:
                    continue
                new_version = str(item["version"])
                if _version_key(new_version) <= _version_key(old_version):
                    continue
                self._install_catalog_item(item, catalog_base=self.catalog_path.parent)
                current[pack_id] = new_version
                updated.append(
                    {
                        "pack_id": pack_id,
                        "from_version": old_version,
                        "to_version": new_version,
                    }
                )
            self._write_current(current)
        return {"status": "updated", "updated": updated}

    def status(self) -> dict[str, Any]:
        current = self._current()
        packs: list[dict[str, Any]] = []
        for pack_id, version in sorted(current.items()):
            destination = self._version_dir(pack_id, version)
            try:
                manifest, terms = _read_installed_pack(destination)
            except (ContractError, IntegrityError, OSError) as exc:
                packs.append(
                    {
                        "pack_id": pack_id,
                        "version": version,
                        "status": "invalid",
                        "error": str(exc),
                    }
                )
            else:
                packs.append(
                    {
                        "pack_id": pack_id,
                        "domain": manifest["domain"],
                        "version": version,
                        "term_count": len(terms),
                        "status": "ready",
                    }
                )
        return {
            "schema_version": PACK_SCHEMA_VERSION,
            "root": str(self.root),
            "catalog_configured": self.catalog_path.is_file(),
            "packs": packs,
        }

    def repair(self, *, catalog: Path | None = None) -> dict[str, Any]:
        self._ensure_layout()
        with self._lock():
            catalog_value = self._catalog(catalog, persist=catalog is not None)
            current = self._current()
            by_id = {str(item["pack_id"]): item for item in catalog_value["packs"]}
            repaired: list[dict[str, str]] = []
            for child in self.temp_root.iterdir():
                if child.is_dir() and child.name.startswith("install-"):
                    shutil.rmtree(child)
                elif child.is_file() and child.name.startswith("download-"):
                    child.unlink()
            for pack_id, version in sorted(current.items()):
                destination = self._version_dir(pack_id, version)
                try:
                    _read_installed_pack(destination)
                    continue
                except (ContractError, IntegrityError, OSError) as exc:
                    item = by_id.get(pack_id)
                    if item is None or str(item["version"]) != version:
                        raise IntegrityError(
                            f"cannot repair {pack_id}@{version}: catalog has no matching version"
                        ) from exc
                    if destination.exists():
                        shutil.rmtree(destination)
                    self._install_catalog_item(item, catalog_base=self.catalog_path.parent)
                    repaired.append({"pack_id": pack_id, "version": version})
        return {"status": "repaired", "repaired": repaired}

    def installed_terms(self) -> list[dict[str, Any]]:
        """Return the shared retrieval pool; Source does not consume it."""
        result: list[dict[str, Any]] = []
        for pack_id, version in sorted(self._current().items()):
            _manifest, terms = _read_installed_pack(self._version_dir(pack_id, version))
            result.extend(
                {"pack_id": pack_id, "pack_version": version, **dict(term)}
                for term in terms
            )
        return result

    def _ensure_layout(self) -> None:
        self.versions_root.mkdir(parents=True, exist_ok=True)
        self.temp_root.mkdir(parents=True, exist_ok=True)

    def _catalog(self, path: Path | None, *, persist: bool = False) -> dict[str, Any]:
        source = path.resolve() if path is not None else self.catalog_path
        if not source.is_file():
            raise IntegrityError(
                "Official Pack catalog is not configured; run explicit pack setup first"
            )
        value = _read_json(source, "Official Pack catalog")
        catalog = _validate_catalog(value, source.parent)
        if persist:
            self._atomic_json(self.catalog_path, catalog)
        return catalog

    def _current(self) -> dict[str, str]:
        if not self.current_path.is_file():
            return {}
        value = _read_json(self.current_path, "Official Pack current pointer")
        if not isinstance(value, Mapping) or set(value) != {"schema_version", "installed"}:
            raise IntegrityError("Official Pack current pointer has invalid fields")
        if value["schema_version"] != PACK_SCHEMA_VERSION:
            raise IntegrityError("Official Pack current pointer schema is incompatible")
        installed = value["installed"]
        if not isinstance(installed, Mapping):
            raise IntegrityError("Official Pack current pointer installed map is invalid")
        result: dict[str, str] = {}
        for pack_id, version in installed.items():
            _validate_identifier(pack_id, "Pack ID")
            _validate_version(version)
            result[str(pack_id)] = str(version)
        return result

    def _write_current(self, current: Mapping[str, str]) -> None:
        self._atomic_json(
            self.current_path,
            {
                "schema_version": PACK_SCHEMA_VERSION,
                "installed": dict(sorted(current.items())),
            },
        )

    def _install_catalog_item(
        self, item: Mapping[str, Any], *, catalog_base: Path
    ) -> None:
        pack_id = str(item["pack_id"])
        version = str(item["version"])
        destination = self._version_dir(pack_id, version)
        if destination.exists():
            manifest, _terms = _read_installed_pack(destination)
            if hash_json(manifest) != item["manifest_hash"]:
                raise IntegrityError(f"installed Pack manifest mismatch: {pack_id}@{version}")
            return
        descriptor = _load_descriptor(str(item["source"]), catalog_base, self.temp_root)
        manifest, terms = _validate_descriptor(descriptor)
        if manifest["pack_id"] != pack_id or manifest["version"] != version:
            raise IntegrityError("Official Pack descriptor identity does not match catalog")
        if hash_json(manifest) != item["manifest_hash"]:
            raise IntegrityError("Official Pack manifest hash does not match catalog")
        temp = self.temp_root / ("install-" + uuid.uuid4().hex)
        temp.mkdir()
        try:
            self._atomic_json(temp / "manifest.json", manifest)
            self._atomic_json(temp / "terms.json", terms)
            destination.parent.mkdir(parents=True, exist_ok=True)
            os.replace(temp, destination)
        except BaseException:
            shutil.rmtree(temp, ignore_errors=True)
            raise
        _read_installed_pack(destination)

    def _version_dir(self, pack_id: str, version: str) -> Path:
        _validate_identifier(pack_id, "Pack ID")
        _validate_version(version)
        return self.versions_root / pack_id / version

    def _atomic_json(self, destination: Path, value: Any) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        fd, raw = tempfile.mkstemp(
            prefix=destination.name + ".", suffix=".tmp", dir=destination.parent
        )
        temp = Path(raw)
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as output:
                json.dump(value, output, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
                output.write("\n")
                output.flush()
                os.fsync(output.fileno())
            os.replace(temp, destination)
        except BaseException:
            temp.unlink(missing_ok=True)
            raise

    @contextmanager
    def _lock(self) -> Iterator[None]:
        lock_path = self.root / ".lock"
        try:
            descriptor = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError as exc:
            raise IntegrityError("Official Pack store is locked by another operation") from exc
        try:
            os.write(descriptor, str(os.getpid()).encode("ascii"))
            os.close(descriptor)
            yield
        finally:
            try:
                os.close(descriptor)
            except OSError:
                pass
            lock_path.unlink(missing_ok=True)


def _validate_catalog(value: Any, base: Path) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != {"schema_version", "packs"}:
        raise ContractError("Official Pack catalog fields are invalid")
    if value["schema_version"] != PACK_SCHEMA_VERSION:
        raise ContractError("Official Pack catalog schema is incompatible")
    raw_packs = value["packs"]
    if not isinstance(raw_packs, list) or not raw_packs:
        raise ContractError("Official Pack catalog requires at least one Pack")
    packs: list[dict[str, str]] = []
    seen: set[str] = set()
    for raw in raw_packs:
        if not isinstance(raw, Mapping) or set(raw) != {
            "pack_id",
            "domain",
            "version",
            "source",
            "manifest_hash",
        }:
            raise ContractError("Official Pack catalog entry fields are invalid")
        pack_id = _validate_identifier(raw["pack_id"], "Pack ID")
        domain = _validate_identifier(raw["domain"], "Pack domain")
        version = _validate_version(raw["version"])
        source = _string(raw["source"], "Pack source")
        manifest_hash = _hash(raw["manifest_hash"], "Pack manifest hash")
        if pack_id in seen:
            raise ContractError("Official Pack catalog contains duplicate Pack IDs")
        seen.add(pack_id)
        source_path = Path(source)
        parsed = urllib.parse.urlparse(source)
        if not source_path.is_absolute() and parsed.scheme not in {"", "https"}:
            raise ContractError("Official Pack source must be a local path or HTTPS URL")
        if source_path.is_absolute():
            source = str(source_path.resolve())
        elif not parsed.scheme:
            source = str((base / source).resolve())
        packs.append(
            {
                "pack_id": pack_id,
                "domain": domain,
                "version": version,
                "source": source,
                "manifest_hash": manifest_hash,
            }
        )
    packs.sort(key=lambda item: item["pack_id"])
    return {"schema_version": PACK_SCHEMA_VERSION, "packs": packs}


def _validate_descriptor(value: Any) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    if not isinstance(value, Mapping) or set(value) != {"manifest", "terms"}:
        raise ContractError("Official Pack descriptor fields are invalid")
    manifest = _validate_manifest(value["manifest"])
    raw_terms = value["terms"]
    if not isinstance(raw_terms, list):
        raise ContractError("Official Pack terms must be an array")
    terms: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in raw_terms:
        if not isinstance(raw, Mapping) or set(raw) != {
            "term",
            "category",
            "proper_noun_subtype",
        }:
            raise ContractError("Official Pack term fields are invalid")
        term = _string(raw["term"], "Official Pack term")
        normalized = normalize_surface_form(term)
        category = _string(raw["category"], "Official Pack category")
        subtype_value = raw["proper_noun_subtype"]
        subtype = None if subtype_value is None else _string(subtype_value, "proper noun subtype")
        category, subtype = validate_category(category, subtype)
        if normalized in seen:
            raise ContractError("Official Pack contains duplicate normalized terms")
        seen.add(normalized)
        terms.append(
            {
                "term": term,
                "category": category,
                "proper_noun_subtype": subtype,
            }
        )
    if manifest["term_count"] != len(terms):
        raise IntegrityError("Official Pack term count does not match manifest")
    if manifest["terms_hash"] != hash_json({"terms": terms}):
        raise IntegrityError("Official Pack terms hash does not match manifest")
    return manifest, terms


def _validate_manifest(value: Any) -> dict[str, Any]:
    fields = {
        "schema_version",
        "pack_id",
        "domain",
        "version",
        "license",
        "term_count",
        "terms_hash",
    }
    if not isinstance(value, Mapping) or set(value) != fields:
        raise ContractError("Official Pack manifest fields are invalid")
    if value["schema_version"] != PACK_SCHEMA_VERSION:
        raise ContractError("Official Pack manifest schema is incompatible")
    license_value = value["license"]
    if not isinstance(license_value, Mapping) or set(license_value) != {"name", "url"}:
        raise ContractError("Official Pack license metadata is invalid")
    term_count = value["term_count"]
    if isinstance(term_count, bool) or not isinstance(term_count, int) or term_count < 0:
        raise ContractError("Official Pack term count is invalid")
    license_url = _string(license_value["url"], "Pack license URL")
    parsed_license = urllib.parse.urlparse(license_url)
    if parsed_license.scheme != "https" or not parsed_license.netloc:
        raise ContractError("Official Pack license URL must use HTTPS")
    return {
        "schema_version": PACK_SCHEMA_VERSION,
        "pack_id": _validate_identifier(value["pack_id"], "Pack ID"),
        "domain": _validate_identifier(value["domain"], "Pack domain"),
        "version": _validate_version(value["version"]),
        "license": {
            "name": _string(license_value["name"], "Pack license name"),
            "url": license_url,
        },
        "term_count": term_count,
        "terms_hash": _hash(value["terms_hash"], "Pack terms hash"),
    }


def _read_installed_pack(path: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    if not path.is_dir():
        raise IntegrityError(f"installed Official Pack directory is missing: {path}")
    return _validate_descriptor(
        {
            "manifest": _read_json(path / "manifest.json", "Official Pack manifest"),
            "terms": _read_json(path / "terms.json", "Official Pack terms"),
        }
    )


def _load_descriptor(source: str, base: Path, temp_root: Path) -> Any:
    parsed = urllib.parse.urlparse(source)
    if parsed.scheme == "https":
        temp_root.mkdir(parents=True, exist_ok=True)
        destination = temp_root / ("download-" + uuid.uuid4().hex + ".json")
        try:
            with urllib.request.urlopen(source, timeout=30) as response:  # noqa: S310
                destination.write_bytes(response.read())
            return _read_json(destination, "downloaded Official Pack")
        finally:
            destination.unlink(missing_ok=True)
    path = Path(source)
    if not path.is_absolute():
        path = base / path
    return _read_json(path.resolve(), "Official Pack descriptor")


def _select_catalog_packs(
    catalog: Mapping[str, Any],
    *,
    pack_ids: Sequence[str] | None,
    domains: Sequence[str] | None,
) -> list[Mapping[str, Any]]:
    packs = cast(list[Mapping[str, Any]], catalog["packs"])
    if pack_ids is None and domains is None:
        return packs
    selected_ids = set(pack_ids or ())
    selected_domains = set(domains or ())
    result = [
        item
        for item in packs
        if item["pack_id"] in selected_ids or item["domain"] in selected_domains
    ]
    found_ids = {str(item["pack_id"]) for item in result}
    found_domains = {str(item["domain"]) for item in result}
    if selected_ids.difference(found_ids):
        raise ContractError(f"unknown Official Pack IDs: {sorted(selected_ids - found_ids)}")
    if selected_domains.difference(found_domains):
        raise ContractError(
            f"unknown Official Pack domains: {sorted(selected_domains - found_domains)}"
        )
    if not result:
        raise ContractError("Official Pack selection is empty")
    return result


def _read_json(path: Path, name: str) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise IntegrityError(f"cannot read {name}: {path}") from exc


def _validate_identifier(value: Any, name: str) -> str:
    result = _string(value, name)
    if _IDENTIFIER.fullmatch(result) is None:
        raise ContractError(f"{name} must use lowercase ASCII letters, digits, and hyphens")
    return result


def _validate_version(value: Any) -> str:
    result = _string(value, "Pack version")
    if _VERSION.fullmatch(result) is None:
        raise ContractError("Pack version must be numeric SemVer")
    return result


def _version_key(value: str) -> tuple[int, int, int]:
    _validate_version(value)
    major, minor, patch = value.split(".")
    return int(major), int(minor), int(patch)


def _hash(value: Any, name: str) -> str:
    result = _string(value, name)
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", result):
        raise ContractError(f"{name} must be a lowercase SHA-256 value")
    return result


def _string(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value:
        raise ContractError(f"{name} must be a non-empty string")
    return value
