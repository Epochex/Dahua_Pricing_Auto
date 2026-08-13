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


def _prices(start=100):
    return {
        "FOB C(EUR)": start,
        "DDP A(EUR)": start + 10,
        "Suggested Reseller(EUR)": start + 20,
        "Gold(EUR)": start + 30,
        "Silver(EUR)": start + 40,
        "Ivory(EUR)": start + 50,
        "MSRP(EUR)": start + 60,
    }


def _query_row(internal, external, *, category=None, series_key=None, prices=None):
    return {
        "pn": "black-pn",
        "status": "ok",
        "final_values": {
            "Internal Model": internal,
            "External Model": external,
            **(prices or _prices(200)),
        },
        "calculated_fields": [],
        "meta": {"category": category, "series_key": series_key},
        "warnings": [],
    }


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


def test_ipc5_black_variant_is_excluded_by_policy_even_when_white_price_exists():
    data = _bundle_with_france_rows(
        [
            {
                "Part No.": "white-ipc5",
                "Internal Model": "DH-IPC-HFW5442TP-ASE-0280B",
                "External Model": "DH-IPC-HFW5442T-ASE",
                **_prices(100),
            }
        ]
    )
    row = _query_row(
        "DH-IPC-HFW5442TP-ASE-0280B-Black",
        "DH-IPC-HFW5442T-ASE-Black",
        category="IPC",
        series_key="IPC3",  # model family must win if legacy metadata disagrees
    )

    changed = apply_black_variant_markup(data, row, apply=True)

    assert changed is False
    assert row["meta"]["black_variant"]["policy_allowed"] is False
    assert row["meta"]["black_variant"]["policy_rule"] == "ipc_other_no_markup"
    assert row["meta"]["black_variant"]["markup_eur"] == 0
    assert row["meta"]["black_variant"]["applied"] is False
    assert row["final_values"]["FOB C(EUR)"] == 200


def test_non_ipc_black_variant_uses_white_price_plus_one():
    data = _bundle_with_france_rows(
        [
            {
                "Part No.": "black-alarm-row",
                "Internal Model": "DHI-ARA12-W2",
                "External Model": "DHI-ARA12-W2-Black",
                **_prices(80),
            },
            {
                "Part No.": "white-alarm",
                "Internal Model": "DHI-ARA12-W2",
                "External Model": "DHI-ARA12-W2",
                **_prices(30),
            }
        ]
    )
    row = _query_row(
        "DHI-ARA12-W2-Black",
        "DHI-ARA12-W2-Black",
        # Exercise a known legacy-classifier failure: explicit non-IPC model
        # family must win over an incorrect IPC1 category result.
        category="IPC",
        series_key="IPC1",
    )

    changed = apply_black_variant_markup(data, row, apply=True)

    assert changed is True
    assert row["meta"]["black_variant"]["policy_rule"] == "non_ipc_plus_1"
    assert row["meta"]["black_variant"]["markup_eur"] == 1
    assert row["meta"]["black_variant"]["white_pn"] == "white-alarm"
    assert row["final_values"]["FOB C(EUR)"] == 31
    assert row["final_values"]["MSRP(EUR)"] == 91


def test_external_model_match_selects_same_lens_instead_of_first_row():
    shared_external = "DH-IPC-HFW3449T-ASE"
    data = _bundle_with_france_rows(
        [
            {
                "Part No.": "white-0280",
                "Internal Model": "DH-IPC-HFW3449TP-ASE-0280B",
                "External Model": shared_external,
                **_prices(100),
            },
            {
                "Part No.": "white-0360",
                "Internal Model": "DH-IPC-HFW3449TP-ASE-0360B",
                "External Model": shared_external,
                **_prices(150),
            },
        ]
    )
    # Only External Model carries the Black token, so the fallback has to
    # disambiguate the shared External Model with the Internal Model lens code.
    row = _query_row(
        "DH-IPC-HFW3449TP-ASE-0360B",
        f"{shared_external}-Black",
        category="IPC",
        series_key="IPC3",
    )

    changed = apply_black_variant_markup(data, row, apply=True)

    assert changed is True
    variant = row["meta"]["black_variant"]
    assert variant["white_pn"] == "white-0360"
    assert variant["black_lens_signature"] == ["0360"]
    assert variant["white_lens_signature"] == ["0360"]
    assert row["final_values"]["FOB C(EUR)"] == 152


def test_black_variant_rejects_different_lens_counterpart():
    shared_external = "DH-IPC-HFW3449T-ASE"
    data = _bundle_with_france_rows(
        [
            {
                "Part No.": "white-0280",
                "Internal Model": "DH-IPC-HFW3449TP-ASE-0280B",
                "External Model": shared_external,
                **_prices(100),
            }
        ]
    )
    row = _query_row(
        "DH-IPC-HFW3449TP-ASE-0360B",
        f"{shared_external}-Black",
        category="IPC",
        series_key="IPC3",
    )

    changed = apply_black_variant_markup(data, row, apply=True)

    assert changed is False
    variant = row["meta"]["black_variant"]
    assert variant["eligible"] is False
    assert variant["match_rejection_reason"] == "lens_mismatch"
    assert "black_variant_lens_mismatch" in row["warnings"]
    assert row["final_values"]["FOB C(EUR)"] == 200


def test_black_variant_rejects_ambiguous_external_model_without_lens_code():
    shared_external = "DH-IPC-PDW3849-A180-E2-ASTE"
    data = _bundle_with_france_rows(
        [
            {
                "Part No.": "white-left",
                "Internal Model": "DH-IPC-PDW3849P-A180-E2-ASTE-LEFT",
                "External Model": shared_external,
                **_prices(100),
            },
            {
                "Part No.": "white-right",
                "Internal Model": "DH-IPC-PDW3849P-A180-E2-ASTE-RIGHT",
                "External Model": shared_external,
                **_prices(120),
            },
        ]
    )
    row = _query_row(
        "DH-IPC-PDW3849P-A180-E2-ASTE",
        f"{shared_external}-Black",
        category="IPC",
        series_key="IPC3",
    )

    changed = apply_black_variant_markup(data, row, apply=True)

    assert changed is False
    variant = row["meta"]["black_variant"]
    assert variant["eligible"] is False
    assert variant["match_ambiguous"] is True
    assert variant["match_rejection_reason"] == "ambiguous_external_model_after_lens_filter"
