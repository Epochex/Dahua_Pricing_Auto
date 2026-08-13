import pandas as pd
import pytest

from backend.engine.core.pricing_engine import (
    PRICE_COLS,
    PRICE_PRIORITY_COUNTRY_FIRST,
    PRICE_PRIORITY_SYSTEM_FIRST,
    compute_prices_for_part,
    normalize_price_priority,
    UPLIFT_PCT_BY_LINE,
)
from backend.engine.core.pricing_rules import DDP_RULES


def _france_row():
    row = {
        "Part No.": "1.2.3",
        "Series": "IPC",
        "Internal Model": "IPC-TEST",
        "External Model": "IPC-TEST",
    }
    row.update({field: 200 + index for index, field in enumerate(PRICE_COLS)})
    return pd.Series(row)


def _sys_row(*, min_price=100):
    return pd.Series(
        {
            "Part Num": "1.2.3",
            "First Product Line": "Camera",
            "Second Product Line": "Network Camera",
            "Internal Model": "IPC-TEST",
            "External Model": "IPC-TEST",
            "Sales Type": "Distribution",
            "Min Price": min_price,
            "Area Price": 150,
        }
    )


def _france_map():
    return pd.DataFrame(
        [
            {
                "priority": 1,
                "field1": "Series",
                "match_type1": "equals",
                "pattern1": "IPC",
                "category": "IPC",
                "price_group_hint": "IPC",
            }
        ]
    )


def _compute(priority, *, min_price=100):
    return compute_prices_for_part(
        "1.2.3",
        france_row=_france_row(),
        sys_row=_sys_row(min_price=min_price),
        france_map=_france_map(),
        sys_map=pd.DataFrame(),
        price_priority=priority,
    )


def test_country_first_keeps_complete_country_prices():
    result = _compute(PRICE_PRIORITY_COUNTRY_FIRST)

    assert result["used_sys"] is False
    assert result["price_priority"] == PRICE_PRIORITY_COUNTRY_FIRST
    assert result["final_values"]["FOB C(EUR)"] == 200
    assert result["calculated_fields"] == set()


def test_system_first_recalculates_all_prices_from_system_floor():
    result = _compute(PRICE_PRIORITY_SYSTEM_FIRST)

    assert result["used_sys"] is True
    assert result["price_priority"] == PRICE_PRIORITY_SYSTEM_FIRST
    assert result["sys_basis_field"] == "Min Price"
    assert result["sys_basis_price_used"] == 100
    assert result["final_values"]["FOB C(EUR)"] == pytest.approx(90)
    assert set(PRICE_COLS).issubset(result["calculated_fields"])


def test_system_first_reports_each_applied_extra_adjust(monkeypatch):
    monkeypatch.setitem(UPLIFT_PCT_BY_LINE, "IPC", 0.05)
    monkeypatch.setitem(DDP_RULES, "IPC", (0.10, 0.008, 0.02, 0.000198, 0.12, 0.15))

    result = _compute(PRICE_PRIORITY_SYSTEM_FIRST)

    assert result["sys_uplift_key"] == "IPC"
    assert result["sys_uplift_pct"] == pytest.approx(0.05)
    assert result["fob_euro_adjust_pct"] == pytest.approx(0.15)
    assert result["ddp_adjust_pct"] == pytest.approx(0.12)


def test_system_first_falls_back_to_country_when_system_floor_is_missing():
    result = _compute(PRICE_PRIORITY_SYSTEM_FIRST, min_price=None)

    assert result["used_sys"] is False
    assert result["final_values"]["FOB C(EUR)"] == 200
    assert result["calculated_fields"] == set()


def test_invalid_price_priority_is_rejected():
    with pytest.raises(ValueError, match="price_priority"):
        normalize_price_priority("unknown")
