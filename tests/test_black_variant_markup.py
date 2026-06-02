import pandas as pd

from backend.engine.core.pricing_engine import apply_black_variant_markup
from backend.engine.core.loader import DataBundle


def _bundle_with_france_rows(rows):
    return DataBundle(
        france_df=pd.DataFrame(rows),
        sys_df=pd.DataFrame(),
        map_fr=pd.DataFrame(),
        map_sys=pd.DataFrame(),
        fr_idx_raw={},
        fr_idx_base={},
        sys_idx_raw={},
        sys_idx_base={},
    )


def test_black_variant_prefers_hdbw_to_hfw_white_counterpart_and_applies_plus_two():
    data = _bundle_with_france_rows(
        [
            {
                "Part No.": "white-hdbw",
                "Internal Model": "DH-IPC-HDBW3449RP-ZAS-IL-27135",
                "External Model": "DH-IPC-HDBW3449R-ZAS-IL",
                "FOB C(EUR)": 103,
                "DDP A(EUR)": 118,
                "Suggested Reseller(EUR)": 145,
                "Gold(EUR)": 168,
                "Silver(EUR)": 186,
                "Ivory(EUR)": 200,
                "MSRP(EUR)": 496,
            },
            {
                "Part No.": "white-hfw",
                "Internal Model": "DH-IPC-HFW3449TP-ZAS-IL-27135",
                "External Model": "DH-IPC-HFW3449T-ZAS-IL",
                "FOB C(EUR)": 105,
                "DDP A(EUR)": 119,
                "Suggested Reseller(EUR)": 146,
                "Gold(EUR)": 172,
                "Silver(EUR)": 189,
                "Ivory(EUR)": 205,
                "MSRP(EUR)": 510,
            },
        ]
    )
    row = {
        "pn": "1.0.01.04.44964-9001",
        "status": "ok",
        "final_values": {
            "Internal Model": "DH-IPC-HDBW3449RP-ZAS-IL-27135-Black",
            "External Model": "DH-IPC-HDBW3449R-ZAS-IL-Black",
            "FOB C(EUR)": 117,
            "DDP A(EUR)": 132.7,
            "Suggested Reseller(EUR)": 150.8,
            "Gold(EUR)": 156.2,
            "Silver(EUR)": 165.9,
            "Ivory(EUR)": 177.0,
            "MSRP(EUR)": 295.0,
        },
        "calculated_fields": [],
        "meta": {"sys_basis_price_used": 130},
        "warnings": [],
    }

    changed = apply_black_variant_markup(data, row, apply=True)

    assert changed is True
    assert row["meta"]["black_variant"]["white_pn"] == "white-hfw"
    assert row["meta"]["black_variant"]["match_source"] == "hdbw_to_hfw_internal"
    assert row["meta"]["sys_basis_price_used"] == 130
    assert row["final_values"]["FOB C(EUR)"] == 107
    assert row["final_values"]["DDP A(EUR)"] == 121
    assert row["final_values"]["Suggested Reseller(EUR)"] == 148
    assert row["final_values"]["MSRP(EUR)"] == 512
