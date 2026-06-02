import pandas as pd

from backend.engine.core.loader import DataBundle
from backend.engine.core.pricing_engine import apply_atc_variant_markup


def _bundle(france_rows, sys_rows):
    return DataBundle(
        france_df=pd.DataFrame(france_rows),
        sys_df=pd.DataFrame(sys_rows),
        map_fr=pd.DataFrame(),
        map_sys=pd.DataFrame(),
        fr_idx_raw={},
        fr_idx_base={},
        sys_idx_raw={str(r["Part Num"]).upper(): i for i, r in enumerate(sys_rows)},
        sys_idx_base={},
    )


def _price_row(pn, internal, external, start):
    return {
        "Part No.": pn,
        "Internal Model": internal,
        "External Model": external,
        "FOB C(EUR)": start,
        "DDP A(EUR)": start + 10,
        "Suggested Reseller(EUR)": start + 20,
        "Gold(EUR)": start + 30,
        "Silver(EUR)": start + 40,
        "Ivory(EUR)": start + 50,
        "MSRP(EUR)": start + 100,
    }


def _sys_row(pn, internal, external, area_price):
    return {
        "Part Num": pn,
        "Internal Model": internal,
        "External Model": external,
        "Min Price": None,
        "Area Price": area_price,
        "Sales Type": "Project",
    }


def test_atc_variant_applies_sys_delta_to_same_optical_base_france_prices():
    data = _bundle(
        [
            _price_row(
                "base-0832",
                "DH-IPC-HDBW7459ZP-Z-PV-0832-X",
                "DH-IPC-HDBW7459Z-Z4-PV-X",
                200,
            )
        ],
        [
            _sys_row(
                "1.0.01.04.47274-9001",
                "DH-IPC-HDBW7459ZP-Z-PV-0832-X",
                "DH-IPC-HDBW7459Z-Z4-PV-X",
                459,
            ),
            _sys_row(
                "1.0.01.04.48195-9001",
                "DH-IPC-HDBW7459ZP-Z-PV-0832-X-ATC",
                "DH-IPC-HDBW7459Z-Z4-PV-X-ATC",
                479,
            ),
        ],
    )
    row = {
        "pn": "1.0.01.04.48195-9001",
        "status": "ok",
        "final_values": {
            "Internal Model": "DH-IPC-HDBW7459ZP-Z-PV-0832-X-ATC",
            "External Model": "DH-IPC-HDBW7459Z-Z4-PV-X-ATC",
            "FOB C(EUR)": 474.21,
            "DDP A(EUR)": 536,
            "MSRP(EUR)": 2063,
        },
        "calculated_fields": [],
        "meta": {"sys_basis_price_used": 479},
        "warnings": [],
    }

    changed = apply_atc_variant_markup(data, row, apply=True)

    assert changed is True
    assert row["meta"]["atc_variant"]["base_pn"] == "base-0832"
    assert row["meta"]["atc_variant"]["markup_eur"] == 20
    assert row["meta"]["atc_variant"]["sys_base_pn"] == "1.0.01.04.47274-9001"
    assert row["final_values"]["FOB C(EUR)"] == 494.21
    assert row["final_values"]["DDP A(EUR)"] == 556
    assert row["final_values"]["MSRP(EUR)"] == 2083


def test_atc_variant_can_fallback_from_0832_z4_to_2712_z_base():
    data = _bundle(
        [
            _price_row(
                "base-2712",
                "DH-IPC-HDBW7459ZP-Z-PV-2712-X",
                "DH-IPC-HDBW7459Z-Z-PV-X",
                180,
            )
        ],
        [
            _sys_row(
                "1.0.01.04.47273-9001",
                "DH-IPC-HDBW7459ZP-Z-PV-2712-X",
                "DH-IPC-HDBW7459Z-Z-PV-X",
                454,
            ),
            _sys_row(
                "1.0.01.04.48195-9001",
                "DH-IPC-HDBW7459ZP-Z-PV-0832-X-ATC",
                "DH-IPC-HDBW7459Z-Z4-PV-X-ATC",
                479,
            ),
        ],
    )
    row = {
        "pn": "1.0.01.04.48195-9001",
        "status": "ok",
        "final_values": {
            "Internal Model": "DH-IPC-HDBW7459ZP-Z-PV-0832-X-ATC",
            "External Model": "DH-IPC-HDBW7459Z-Z4-PV-X-ATC",
            "FOB C(EUR)": 474,
            "MSRP(EUR)": 2063,
        },
        "calculated_fields": [],
        "meta": {},
        "warnings": [],
    }

    changed = apply_atc_variant_markup(data, row, apply=True)

    assert changed is True
    assert row["meta"]["atc_variant"]["base_pn"] == "base-2712"
    assert row["meta"]["atc_variant"]["match_source"] == "strip_atc_internal_optical_fallback"
    assert row["meta"]["atc_variant"]["markup_eur"] == 25
    assert row["final_values"]["FOB C(EUR)"] == 499
    assert row["final_values"]["MSRP(EUR)"] == 2088


def test_atc_variant_applies_without_france_base_prices():
    data = _bundle(
        [],
        [
            _sys_row(
                "1.0.01.04.47274-9001",
                "DH-IPC-HDBW7459ZP-Z-PV-0832-X",
                "DH-IPC-HDBW7459Z-Z4-PV-X",
                459,
            ),
            _sys_row(
                "1.0.01.04.48195-9001",
                "DH-IPC-HDBW7459ZP-Z-PV-0832-X-ATC",
                "DH-IPC-HDBW7459Z-Z4-PV-X-ATC",
                479,
            ),
        ],
    )
    row = {
        "pn": "1.0.01.04.48195-9001",
        "status": "ok",
        "final_values": {
            "Internal Model": "DH-IPC-HDBW7459ZP-Z-PV-0832-X-ATC",
            "External Model": "DH-IPC-HDBW7459Z-Z4-PV-X-ATC",
            "FOB C(EUR)": 474,
            "DDP A(EUR)": 536,
        },
        "calculated_fields": [],
        "meta": {},
        "warnings": [],
    }

    changed = apply_atc_variant_markup(data, row, apply=True)

    assert changed is True
    assert row["meta"]["atc_variant"]["base_pn"] == "1.0.01.04.47274-9001"
    assert row["meta"]["atc_variant"]["markup_eur"] == 20
    assert row["meta"]["atc_variant"]["base_prices"] == {}
    assert row["final_values"]["FOB C(EUR)"] == 494
    assert row["final_values"]["DDP A(EUR)"] == 556
