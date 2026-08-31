from __future__ import annotations

import json
from pathlib import Path

from backend.app.historical_pricing_evidence import HistoricalPricingEvidenceIndex


def _write(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def _state(created_at: str, items: list[dict]) -> dict:
    return {
        "created_at": created_at,
        "status": "done",
        "report": {"count_total": len(items), "items": items},
    }


def test_temporal_queries_exclude_held_out_and_later_results(tmp_path: Path) -> None:
    runtime = tmp_path / "runtime"
    _write(
        runtime / "outputs" / "job-1" / "state.json",
        _state(
            "2026-01-01T00:00:00+00:00",
            [
                {
                    "pn": "PN-1",
                    "internal_model": "MODEL-A",
                    "status": "ok",
                    "category": "IPC",
                    "price_group": "IPC",
                    "warnings": [],
                }
            ],
        ),
    )
    _write(
        runtime / "outputs" / "job-2" / "state.json",
        _state(
            "2026-02-01T00:00:00+00:00",
            [
                {
                    "pn": "PN-1",
                    "internal_model": "MODEL-A",
                    "status": "ok",
                    "category": "PTZ",
                    "price_group": "PTZ",
                    "warnings": ["changed mapping"],
                }
            ],
        ),
    )
    _write(
        runtime / "outputs" / "job-3" / "state.json",
        _state(
            "2026-03-01T00:00:00+00:00",
            [
                {
                    "pn": "PN-1",
                    "internal_model": "MODEL-A",
                    "status": "ok",
                    "category": "PTZ",
                    "price_group": "PTZ",
                    "warnings": [],
                }
            ],
        ),
    )

    index = HistoricalPricingEvidenceIndex(runtime)
    before_second = index.search_pricing_results(
        pn="PN-1",
        as_of="2026-02-01T00:00:00+00:00",
    )
    comparison = index.compare_prior_classifications(
        pn="PN-1",
        as_of="2026-03-01T00:00:00+00:00",
    )

    assert before_second["count"] == 1
    assert before_second["entries"][0]["category"] == "IPC"
    assert comparison["summary_code"] == "prior_classification_conflict"
    assert comparison["selected_category"] is None
    assert comparison["category_counts"] == {"IPC": 1, "PTZ": 1}
    assert len(index.temporal_episodes()) == 2


def test_sheet_snapshots_are_deduplicated_by_business_row(tmp_path: Path) -> None:
    runtime = tmp_path / "runtime"
    base_task = {
        "sheet": "July",
        "row_index": 7,
        "pn": "PN-7",
        "internal_model": "MODEL-7",
        "customer_name": "Customer A",
        "price_level": "Gold",
        "product_line": "IPC",
        "description": "initial request",
        "normalized_status": "pending",
    }
    _write(
        runtime / "agent" / "sheet_parsed" / "push-1.json",
        {
            "push_id": "push-1",
            "received_at": "2026-07-01T00:00:00+00:00",
            "tasks": [base_task],
        },
    )
    _write(
        runtime / "agent" / "sheet_parsed" / "push-2.json",
        {
            "push_id": "push-2",
            "received_at": "2026-07-02T00:00:00+00:00",
            "tasks": [{**base_task, "description": "corrected request"}],
        },
    )

    index = HistoricalPricingEvidenceIndex(runtime)
    result = index.search_quote_requests(pn="PN-7")

    assert index.refresh()["quote_records"] == 1
    assert result["count"] == 1
    assert result["entries"][0]["request_description"] == "corrected request"
    assert result["entries"][0]["customer_ref"].startswith("historical-customer:")
