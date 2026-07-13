import pandas as pd
import pytest

from backend.engine.core.pricing_engine import compute_prices_for_part


def _sys_map(second_line, category, price_group):
    return pd.DataFrame(
        [
            {
                "priority": 1,
                "field1": "First Product Line",
                "match_type1": "equals",
                "pattern1": "Accessory",
                "field2": "Second Product Line",
                "match_type2": "equals",
                "pattern2": second_line,
                "category": category,
                "price_group_hint": price_group,
            }
        ]
    )


def _sys_row(second_line):
    return pd.Series(
        {
            "Part Num": "1.2.3",
            "First Product Line": "Accessory",
            "Second Product Line": second_line,
            "Catelog Name": second_line,
            "Internal Model": f"TEST-{second_line.upper()}",
            "External Model": f"TEST-{second_line.upper()}",
            "Sales Type": "Distribution",
            "Min Price": 100,
            "Area Price": 200,
        }
    )


def _compute(second_line, category, price_group):
    return compute_prices_for_part(
        "1.2.3",
        france_row=None,
        sys_row=_sys_row(second_line),
        france_map=pd.DataFrame(),
        sys_map=_sys_map(second_line, category, price_group),
    )


def test_distribution_cabling_follows_fob_n_area_price():
    result = _compute("Cabling", "ACCESSORY线缆", "ACCESSORY线缆")

    assert result["sys_sales_type"] == "DISTRIBUTION"
    assert result["sys_basis_field"] == "Area Price"
    assert result["sys_basis_price_used"] == 200
    assert result["final_values"]["FOB C(EUR)"] == pytest.approx(180)


def test_distribution_power_follows_fob_n_area_price():
    result = _compute("Power", "ACCESSORY", "ACCESSORY")

    assert result["sys_sales_type"] == "DISTRIBUTION"
    assert result["sys_basis_field"] == "Area Price"
    assert result["sys_basis_price_used"] == 200
    assert result["final_values"]["FOB C(EUR)"] == pytest.approx(180)


def test_other_distribution_accessories_still_use_min_price():
    result = _compute("Camera mount", "ACCESSORY", "ACCESSORY")

    assert result["sys_sales_type"] == "DISTRIBUTION"
    assert result["sys_basis_field"] == "Min Price"
    assert result["sys_basis_price_used"] == 100
    assert result["final_values"]["FOB C(EUR)"] == pytest.approx(90)
