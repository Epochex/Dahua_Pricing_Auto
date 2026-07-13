import pandas as pd
import pytest

from backend.engine.core.loader import DataBundle, _build_index, normalize_pn_base
from backend.engine.core.pricing_engine import compute_one


def test_dahua_dotted_pn_suffixes_share_base_key():
    assert normalize_pn_base("1.0.99.12.10604-003") == "1.0.99.12.10604"
    assert normalize_pn_base("1.0.99.12.10604-9001") == "1.0.99.12.10604"
    assert normalize_pn_base("DH-IPC-HFW1230") == "DH-IPC-HFW1230"


def test_compute_one_falls_back_from_short_suffix_to_9001_price_row():
    sys_df = pd.DataFrame(
        [
            {
                "Part Num": "1.0.99.12.10604-9001",
                "First Product Line": "Accessory",
                "Second Product Line": "Power",
                "Catelog Name": "Power",
                "Internal Model": "DHI-POWER-TEST",
                "External Model": "DHI-POWER-TEST",
                "Sales Type": "Distribution",
                "Min Price": 100,
                "Area Price": 200,
                "Standard Price": 300,
                "Release Status": "Normal",
            }
        ]
    )
    sys_idx_raw, sys_idx_base = _build_index(sys_df)
    data = DataBundle(
        france_df=pd.DataFrame(),
        sys_df=sys_df,
        map_fr=pd.DataFrame(),
        map_sys=pd.DataFrame(
            [
                {
                    "priority": 1,
                    "field1": "First Product Line",
                    "match_type1": "equals",
                    "pattern1": "Accessory",
                    "field2": "Second Product Line",
                    "match_type2": "equals",
                    "pattern2": "Power",
                    "category": "ACCESSORY",
                    "price_group_hint": "ACCESSORY",
                }
            ]
        ),
        fr_idx_raw={},
        fr_idx_base={},
        sys_idx_raw=sys_idx_raw,
        sys_idx_base=sys_idx_base,
    )

    result = compute_one(data, "1.0.99.12.10604-003")

    assert result["status"] == "ok"
    assert result["meta"]["sys_match_mode"] == "base"
    assert result["meta"]["sys_matched_pn"] == "1.0.99.12.10604-9001"
    assert result["final_values"]["Part No."] == "1.0.99.12.10604-003"
    assert result["meta"]["sys_basis_field"] == "Area Price"
    assert result["meta"]["sys_basis_price_used"] == 200
    assert result["final_values"]["FOB C(EUR)"] == pytest.approx(180)
