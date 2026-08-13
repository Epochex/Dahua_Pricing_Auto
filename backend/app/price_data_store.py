from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from backend.engine.core.loader import DataBundle, load_all_data


class PriceDataError(RuntimeError):
    pass


class PriceDataNotFound(PriceDataError):
    pass


class PriceDataConflict(PriceDataError):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _atomic_json(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    with tmp.open("w", encoding="utf-8") as stream:
        json.dump(payload, stream, ensure_ascii=False, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    tmp.replace(path)


class PriceDataStore:
    """Immutable price-data releases plus one atomically persisted active pointer."""

    FILES = ("FrancePrice.xlsx", "SysPrice.xlsx")
    MAP_FILES = ("productline_map_france_full.csv", "productline_map_sys_full.csv")
    VERSION_RE = re.compile(r"^[A-Za-z0-9_-]{1,100}$")

    def __init__(self, runtime_dir: Path):
        self.runtime_dir = Path(runtime_dir)
        self.root = self.runtime_dir / "price-data"
        self.releases_dir = self.root / "releases"
        self.candidates_dir = self.root / "candidates"
        self.active_path = self.root / "active.json"
        self.audit_path = self.root / "audit.jsonl"
        self._lock = threading.RLock()

    def _safe_error(self, exc: Exception) -> str:
        message = str(exc).replace(str(self.runtime_dir), "<runtime>")
        return f"{type(exc).__name__}: {message}"[:1000]

    def _append_audit(self, event: str, *, actor: str, version_id: str, extra: Optional[Dict[str, Any]] = None) -> None:
        record = {
            "event_id": uuid.uuid4().hex,
            "timestamp": _now(),
            "event": event,
            "actor": (actor or "unknown")[:200],
            "version_id": version_id,
            **(extra or {}),
        }
        self.audit_path.parent.mkdir(parents=True, exist_ok=True)
        with self.audit_path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")
            stream.flush()
            os.fsync(stream.fileno())

    def _read_manifest(self, root: Path) -> Dict[str, Any]:
        path = root / "manifest.json"
        if not path.is_file():
            raise PriceDataNotFound("price data version not found")
        return json.loads(path.read_text(encoding="utf-8"))

    @classmethod
    def _validate_version_id(cls, version_id: str) -> str:
        value = str(version_id or "")
        if not cls.VERSION_RE.fullmatch(value):
            raise PriceDataNotFound("price data version not found")
        return value

    def _copy_mappings(self, target_root: Path) -> None:
        source = self.runtime_dir / "mapping"
        target = target_root / "mapping"
        target.mkdir(parents=True, exist_ok=True)
        for name in self.MAP_FILES:
            src = source / name
            if not src.is_file():
                raise PriceDataError(f"required mapping is missing: {name}")
            shutil.copy2(src, target / name)

    @staticmethod
    def _bundle_summary(bundle: DataBundle) -> Dict[str, Any]:
        return {
            "rows": {
                "france": int(bundle.france_df.shape[0]),
                "sys": int(bundle.sys_df.shape[0]),
            },
            "unique_pns": {
                "france": len(bundle.fr_idx_raw or {}),
                "sys": len(bundle.sys_idx_raw or {}),
            },
        }

    @staticmethod
    def _diff(candidate: DataBundle, current: Optional[DataBundle]) -> Dict[str, Any]:
        if current is None:
            return {"baseline": True, "france": {}, "sys": {}}
        result: Dict[str, Any] = {"baseline": False}
        for label, new_idx, old_idx, new_df, old_df in (
            ("france", candidate.fr_idx_raw, current.fr_idx_raw, candidate.france_df, current.france_df),
            ("sys", candidate.sys_idx_raw, current.sys_idx_raw, candidate.sys_df, current.sys_df),
        ):
            new = set(new_idx or {})
            old = set(old_idx or {})
            result[label] = {
                "rows_before": int(old_df.shape[0]),
                "rows_after": int(new_df.shape[0]),
                "rows_delta": int(new_df.shape[0] - old_df.shape[0]),
                "pns_added": len(new - old),
                "pns_removed": len(old - new),
                "pns_unchanged": len(new & old),
            }
        return result

    def _manifest(self, version_id: str, bundle: DataBundle, root: Path, *, status: str, diff: Dict[str, Any]) -> Dict[str, Any]:
        created_at = _now()
        france_meta = {
            "filename": "FrancePrice.xlsx",
            "row_count": int(bundle.france_df.shape[0]),
            "sha256": _sha256(root / "data" / "FrancePrice.xlsx"),
            "size_bytes": (root / "data" / "FrancePrice.xlsx").stat().st_size,
            "updated_at": created_at,
        }
        sys_meta = {
            "filename": "SysPrice.xlsx",
            "row_count": int(bundle.sys_df.shape[0]),
            "sha256": _sha256(root / "data" / "SysPrice.xlsx"),
            "size_bytes": (root / "data" / "SysPrice.xlsx").stat().st_size,
            "updated_at": created_at,
        }
        return {
            "version_id": version_id,
            "status": status,
            "created_at": created_at,
            "validated_at": created_at,
            "valid": True,
            "validation": {"ok": True, "parser": "load_all_data"},
            "files": {"france": france_meta, "sys": sys_meta},
            **self._bundle_summary(bundle),
            "diff": diff,
        }

    def _load_root(self, root: Path) -> DataBundle:
        try:
            return load_all_data(root / "data")
        except Exception as exc:
            raise PriceDataError(f"price data validation failed: {self._safe_error(exc)}") from exc

    def bootstrap(self) -> Tuple[DataBundle, str]:
        """Load persisted active release, importing legacy runtime/data once if needed."""
        with self._lock:
            self.releases_dir.mkdir(parents=True, exist_ok=True)
            self.candidates_dir.mkdir(parents=True, exist_ok=True)
            if self.active_path.is_file():
                active = json.loads(self.active_path.read_text(encoding="utf-8"))
                version_id = self._validate_version_id(str(active.get("version_id") or ""))
                root = self.releases_dir / version_id
                self._activate_runtime_data(version_id)
                return self._load_root(root), version_id

            bundle = load_all_data(self.runtime_dir / "data")
            version_id = f"initial-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:8]}"
            temp = self.releases_dir / f".{version_id}.tmp"
            final = self.releases_dir / version_id
            shutil.copytree(self.runtime_dir / "data", temp / "data", symlinks=False)
            for name, source in (
                ("FrancePrice.xlsx", bundle.france_price_path),
                ("SysPrice.xlsx", bundle.sys_price_path),
            ):
                if source is None:
                    raise PriceDataError(f"active source is missing: {name}")
                shutil.copy2(source, temp / "data" / name)
            self._copy_mappings(temp)
            imported = self._load_root(temp)
            manifest = self._manifest(version_id, imported, temp, status="published", diff=self._diff(imported, None))
            _atomic_json(temp / "manifest.json", manifest)
            temp.replace(final)
            _atomic_json(self.active_path, {"version_id": version_id, "activated_at": _now()})
            self._activate_runtime_data(version_id)
            self._append_audit("bootstrap", actor="system", version_id=version_id)
            return self._load_root(final), version_id

    def create_candidate(self, france_source: Path, sys_source: Path, *, actor: str, current: Optional[DataBundle]) -> Dict[str, Any]:
        with self._lock:
            version_id = f"v-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:10]}"
            temp = self.candidates_dir / f".{version_id}.tmp"
            final = self.candidates_dir / version_id
            try:
                current_data = self.runtime_dir / "data"
                if current_data.exists():
                    shutil.copytree(current_data, temp / "data", symlinks=False)
                else:
                    (temp / "data").mkdir(parents=True, exist_ok=False)
                shutil.copy2(france_source, temp / "data" / "FrancePrice.xlsx")
                shutil.copy2(sys_source, temp / "data" / "SysPrice.xlsx")
                self._copy_mappings(temp)
                bundle = self._load_root(temp)
                manifest = self._manifest(version_id, bundle, temp, status="candidate", diff=self._diff(bundle, current))
                _atomic_json(temp / "manifest.json", manifest)
                temp.replace(final)
                self._append_audit("candidate_uploaded", actor=actor, version_id=version_id, extra={"files": manifest["files"]})
                return manifest
            except Exception as exc:
                shutil.rmtree(temp, ignore_errors=True)
                self._append_audit("candidate_validation_failed", actor=actor, version_id=version_id, extra={"error": self._safe_error(exc)})
                raise

    def publish(self, version_id: str, *, actor: str) -> Tuple[DataBundle, Dict[str, Any]]:
        with self._lock:
            version_id = self._validate_version_id(version_id)
            source = self.candidates_dir / version_id
            if not source.is_dir():
                raise PriceDataNotFound("candidate version not found")
            # Re-parse immediately before activation; do not trust upload-time validation only.
            bundle = self._load_root(source)
            manifest = self._read_manifest(source)
            file_keys = {"FrancePrice.xlsx": "france", "SysPrice.xlsx": "sys"}
            if any(_sha256(source / "data" / name) != manifest["files"][file_keys[name]]["sha256"] for name in self.FILES):
                raise PriceDataConflict("candidate files changed after validation")
            target = self.releases_dir / version_id
            if target.exists():
                raise PriceDataConflict("version is already published")
            source.replace(target)
            manifest = {**manifest, "status": "published", "published_at": _now()}
            _atomic_json(target / "manifest.json", manifest)
            previous = self.active_version_id()
            _atomic_json(self.active_path, {"version_id": version_id, "activated_at": _now()})
            self._activate_runtime_data(version_id)
            self._append_audit("published", actor=actor, version_id=version_id, extra={"previous_version_id": previous})
            return self._load_root(target), manifest

    def rollback(self, version_id: str, *, actor: str) -> Tuple[DataBundle, Dict[str, Any]]:
        with self._lock:
            version_id = self._validate_version_id(version_id)
            target = self.releases_dir / version_id
            if not target.is_dir():
                raise PriceDataNotFound("published version not found")
            bundle = self._load_root(target)
            manifest = self._read_manifest(target)
            previous = self.active_version_id()
            _atomic_json(self.active_path, {"version_id": version_id, "activated_at": _now()})
            self._activate_runtime_data(version_id)
            self._append_audit("rolled_back", actor=actor, version_id=version_id, extra={"previous_version_id": previous})
            return self._load_root(target), manifest

    def _activate_runtime_data(self, version_id: str) -> None:
        """Atomically switch both canonical runtime files by swapping directory symlinks."""
        version_id = self._validate_version_id(version_id)
        release = self.releases_dir / version_id
        if not (release / "data").is_dir():
            raise PriceDataNotFound("published version not found")

        current_link = self.root / "current"
        tmp_current = self.root / f".current-{uuid.uuid4().hex}"
        tmp_current.symlink_to(Path("releases") / version_id, target_is_directory=True)
        os.replace(tmp_current, current_link)

        runtime_data = self.runtime_dir / "data"
        expected = Path("price-data") / "current" / "data"
        if runtime_data.is_symlink():
            if runtime_data.readlink() == expected:
                return
            runtime_data.unlink()
        elif runtime_data.exists():
            legacy = self.root / "legacy-data"
            if legacy.exists():
                legacy = self.root / (
                    f"legacy-data-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-"
                    f"{uuid.uuid4().hex[:8]}"
                )
            runtime_data.replace(legacy)
            self._append_audit(
                "runtime_data_backed_up",
                actor="system",
                version_id=version_id,
                extra={"backup_name": legacy.name},
            )
        tmp_runtime = self.runtime_dir / f".data-{uuid.uuid4().hex}"
        tmp_runtime.symlink_to(expected, target_is_directory=True)
        os.replace(tmp_runtime, runtime_data)

    def active_version_id(self) -> Optional[str]:
        if not self.active_path.is_file():
            return None
        return str(json.loads(self.active_path.read_text(encoding="utf-8")).get("version_id") or "") or None

    def metadata(self) -> Dict[str, Any]:
        with self._lock:
            active = self.active_version_id()
            manifest = self._read_manifest(self.releases_dir / active) if active else None
            return {"active_version": active, "active": manifest, "versions": self.list_versions()["versions"]}

    def list_versions(self) -> Dict[str, Any]:
        with self._lock:
            active = self.active_version_id()
            published = [self._read_manifest(p) for p in self.releases_dir.iterdir() if p.is_dir() and (p / "manifest.json").is_file()] if self.releases_dir.exists() else []
            candidates = [self._read_manifest(p) for p in self.candidates_dir.iterdir() if p.is_dir() and (p / "manifest.json").is_file()] if self.candidates_dir.exists() else []
            published.sort(key=lambda x: str(x.get("created_at") or ""), reverse=True)
            candidates.sort(key=lambda x: str(x.get("created_at") or ""), reverse=True)
            for item in published:
                item["active"] = item.get("version_id") == active
                if item["active"]:
                    item["status"] = "active"
            return {
                "active_version": active,
                "versions": published + candidates,
                "published": published,
                "candidates": candidates,
            }
