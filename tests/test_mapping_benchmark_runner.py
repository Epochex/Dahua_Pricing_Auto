import tempfile
from pathlib import Path

import pytest

from backend.app.mapping_benchmark_runner import (
    run_reference_mapping_benchmark,
    run_reference_mapping_benchmark_with_ablations,
)


ROOT = Path(__file__).resolve().parents[1]


def _temporary_benchmark_dir(tmp_path: Path):
    memory_temp = Path("/dev/shm")
    parent = memory_temp if memory_temp.is_dir() else tmp_path
    return tempfile.TemporaryDirectory(prefix="mapping-benchmark-test-", dir=parent)


def test_reference_benchmark_executes_full_investigation_chain(tmp_path: Path) -> None:
    with _temporary_benchmark_dir(tmp_path) as temporary:
        report = run_reference_mapping_benchmark(
            manifest_path=ROOT / "benchmarks" / "mapping_investigation_v1.jsonl",
            scenarios_path=ROOT / "benchmarks" / "mapping_investigation_scenarios_v1.jsonl",
            work_dir=Path(temporary),
        )

    assert report["case_count"] == 12
    assert report["case_pass_rate"] == 1.0
    assert report["required_signal_recall"] == 1.0
    assert report["eligible_for_release"] is True
    assert report["chain_metrics"]["bad-broad-rule-003"]["tool_calls"] == 3
    assert report["chain_metrics"]["refuse-no-evidence-009"]["tool_calls"] == 5


def test_benchmark_ablation_proves_memory_and_document_retrieval_contribution(
    tmp_path: Path,
) -> None:
    with _temporary_benchmark_dir(tmp_path) as temporary:
        report = run_reference_mapping_benchmark_with_ablations(
            manifest_path=ROOT / "benchmarks" / "mapping_investigation_v1.jsonl",
            scenarios_path=ROOT / "benchmarks" / "mapping_investigation_scenarios_v1.jsonl",
            work_dir=Path(temporary),
        )

    assert report["ablations"]["without_case_memory"]["regressed_case_ids"] == [
        "bad-broad-rule-003"
    ]
    assert report["ablations"]["without_document_retrieval"]["regressed_case_ids"] == [
        "unseen-suffix-007"
    ]
    assert report["ablations"]["without_case_memory"]["eligible_for_release"] is False
    assert report["ablations"]["without_document_retrieval"]["eligible_for_release"] is False


def test_benchmark_rejects_tampered_scenario(tmp_path: Path) -> None:
    source = ROOT / "benchmarks" / "mapping_investigation_scenarios_v1.jsonl"
    tampered = tmp_path / "tampered.jsonl"
    tampered.write_text(
        source.read_text(encoding="utf-8").replace("IPC-HFW-001", "IPC-HFW-TAMPERED", 1),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="scenario hash mismatch"):
        run_reference_mapping_benchmark(
            manifest_path=ROOT / "benchmarks" / "mapping_investigation_v1.jsonl",
            scenarios_path=tampered,
            work_dir=tmp_path / "work",
        )
