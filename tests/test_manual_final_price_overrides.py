import math

import pytest

from backend.engine.core.pricing_engine import (
    apply_manual_final_price_overrides,
    normalize_manual_final_price_overrides,
)
from backend.engine.core.formatter import build_export_frames


def test_direct_manual_prices_replace_final_values_and_become_export_ready():
    row = {
        "status": "ok",
        "final_values": {
            "FOB C(EUR)": 66.0,
            "DDP A(EUR)": None,
            "Gold(EUR)": 99.0,
        },
        "calculated_fields": ["DDP A(EUR)", "Gold(EUR)"],
        "meta": {"manual_override": False},
    }

    result = apply_manual_final_price_overrides(
        row,
        {"DDP A(EUR)": "81.5", "Gold(EUR)": 105},
    )

    assert result["final_values"]["FOB C(EUR)"] == 66.0
    assert result["final_values"]["DDP A(EUR)"] == 81.5
    assert result["final_values"]["Gold(EUR)"] == 105.0
    assert "DDP A(EUR)" not in result["calculated_fields"]
    assert "Gold(EUR)" not in result["calculated_fields"]
    assert result["meta"]["manual_override"] is True
    assert result["meta"]["manual_final_fields"] == ["DDP A(EUR)", "Gold(EUR)"]

    export_row = build_export_frames([result])["export"].iloc[0].to_dict()
    assert export_row["DDP"] == 81.5
    assert export_row["GOLD"] == 105.0


@pytest.mark.parametrize(
    "values, message",
    [
        ({"Unknown": 10}, "not supported"),
        ({"FOB C(EUR)": 0}, "must be > 0"),
        ({"FOB C(EUR)": math.inf}, "must be > 0"),
        ({"FOB C(EUR)": "not-a-number"}, "must be a number"),
    ],
)
def test_direct_manual_price_validation(values, message):
    with pytest.raises(ValueError, match=message):
        normalize_manual_final_price_overrides(values)


def test_direct_manual_prices_do_not_turn_not_found_into_exportable_row():
    row = {"status": "not_found", "final_values": {}, "calculated_fields": []}

    result = apply_manual_final_price_overrides(row, {"FOB C(EUR)": 42})

    assert result["status"] == "not_found"
    assert result["final_values"] == {}
