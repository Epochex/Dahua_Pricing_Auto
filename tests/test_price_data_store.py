import json
from pathlib import Path

import pandas as pd

from backend.app.price_data_store import PriceDataStore


def _write_price(path: Path, pns: list[str], *, sys: bool = False) -> None:
    column = "Part Num" if sys else "Part No."
    pd.DataFrame({column: pns, "Internal Model": [f"MODEL-{x}" for x in pns]}).to_excel(path, index=False)


def _runtime(tmp_path: Path) -> Path:
    runtime = tmp_path / "runtime"
    (runtime / "data").mkdir(parents=True)
    (runtime / "mapping").mkdir()
    _write_price(runtime / "data" / "FrancePrice.xlsx", ["FR-OLD"])
    _write_price(runtime / "data" / "SysPrice.xlsx", ["SY-OLD"], sys=True)
    pd.DataFrame({"prefix": []}).to_csv(runtime / "mapping" / "productline_map_france_full.csv", index=False)
    pd.DataFrame({"prefix": []}).to_csv(runtime / "mapping" / "productline_map_sys_full.csv", index=False)
    return runtime


def test_stage_publish_restart_and_rollback_are_versioned(tmp_path):
    runtime = _runtime(tmp_path)
    store = PriceDataStore(runtime)
    original_bundle, original_id = store.bootstrap()

    assert runtime.joinpath("data").is_symlink()
    assert original_bundle.fr_idx_raw == {"FR-OLD": 0}

    inputs = tmp_path / "inputs"
    inputs.mkdir()
    france = inputs / "france.xlsx"
    sys = inputs / "sys.xlsx"
    _write_price(france, ["FR-OLD", "FR-NEW"])
    _write_price(sys, ["SY-NEW"], sys=True)

    candidate = store.create_candidate(france, sys, actor="alice@example.com", current=original_bundle)
    assert candidate["valid"] is True
    assert candidate["files"]["france"]["row_count"] == 2
    assert len(candidate["files"]["france"]["sha256"]) == 64
    assert candidate["diff"]["france"]["pns_added"] == 1

    published, manifest = store.publish(candidate["version_id"], actor="alice@example.com")
    assert published.fr_idx_raw == {"FR-OLD": 0, "FR-NEW": 1}
    assert published.france_price_path.is_file()
    assert store.active_version_id() == candidate["version_id"]
    assert pd.read_excel(runtime / "data" / "FrancePrice.xlsx").shape[0] == 2

    restarted_bundle, restarted_id = PriceDataStore(runtime).bootstrap()
    assert restarted_id == candidate["version_id"]
    assert "FR-NEW" in restarted_bundle.fr_idx_raw

    rolled_back, _ = store.rollback(original_id, actor="alice@example.com")
    assert rolled_back.fr_idx_raw == {"FR-OLD": 0}
    public = store.metadata()
    assert public["active_version"] == original_id
    assert len(public["versions"]) == 2
    assert all("/" not in item["files"]["france"]["filename"] for item in public["versions"])

    events = [json.loads(line)["event"] for line in store.audit_path.read_text(encoding="utf-8").splitlines()]
    assert events[-4:] == ["bootstrap", "candidate_uploaded", "published", "rolled_back"]


def test_invalid_candidate_is_not_kept_and_is_audited(tmp_path):
    runtime = _runtime(tmp_path)
    store = PriceDataStore(runtime)
    current, _ = store.bootstrap()
    invalid = tmp_path / "invalid.xlsx"
    pd.DataFrame({"wrong": [1]}).to_excel(invalid, index=False)

    try:
        store.create_candidate(invalid, invalid, actor="admin", current=current)
    except Exception as exc:
        assert "validation failed" in str(exc)
    else:
        raise AssertionError("invalid candidate unexpectedly accepted")

    assert store.list_versions()["candidates"] == []
    assert "candidate_validation_failed" in store.audit_path.read_text(encoding="utf-8")
