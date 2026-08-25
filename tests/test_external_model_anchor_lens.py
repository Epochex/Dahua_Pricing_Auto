from types import SimpleNamespace

import pandas as pd

from backend.app import main as app_main
from backend.engine.core.loader import DataBundle


def _prices(start):
    return {
        "FOB C(EUR)": start,
        "DDP A(EUR)": start + 10,
        "Suggested Reseller(EUR)": start + 20,
        "Gold(EUR)": start + 30,
        "Silver(EUR)": start + 40,
        "Ivory(EUR)": start + 50,
        "MSRP(EUR)": start + 60,
    }


def _row(internal, external, start=1):
    return {
        "status": "ok",
        "final_values": {
            "Internal Model": internal,
            "External Model": external,
            **_prices(start),
        },
        "calculated_fields": list(_prices(start).keys()),
        "meta": {},
        "warnings": [],
    }


def _install_engine(monkeypatch, france_rows):
    bundle = DataBundle(
        france_df=pd.DataFrame(france_rows),
        sys_df=pd.DataFrame(),
        map_fr=pd.DataFrame(),
        map_sys=pd.DataFrame(),
        fr_idx_raw={},
        fr_idx_base={},
        sys_idx_raw={},
        sys_idx_base={},
    )
    monkeypatch.setattr(app_main, "_engine", SimpleNamespace(data=bundle))


def test_external_model_anchor_cache_is_partitioned_by_lens(monkeypatch):
    external = "DH-IPC-HFW3449T-ASE"
    _install_engine(
        monkeypatch,
        [
            {
                "Part No.": "fr-0280",
                "Internal Model": "DH-IPC-HFW3449TP-ASE-0280B",
                "External Model": external,
                **_prices(100),
            },
            {
                "Part No.": "fr-0360",
                "Internal Model": "DH-IPC-HFW3449TP-ASE-0360B",
                "External Model": external,
                **_prices(150),
            },
        ],
    )
    cache = {}
    row_0360 = _row("DH-IPC-HFW3449TP-ASE-0360B", external)
    row_0280 = _row("DH-IPC-HFW3449TP-ASE-0280B", external)

    app_main._apply_external_model_anchor_to_row(
        row_0360, apply_france_anchor=True, anchor_cache=cache
    )
    app_main._apply_external_model_anchor_to_row(
        row_0280, apply_france_anchor=True, anchor_cache=cache
    )

    assert row_0360["meta"]["external_model_anchor_pn"] == "fr-0360"
    assert row_0360["meta"]["external_model_anchor_lens_signature"] == ["0360"]
    assert row_0360["final_values"]["FOB C(EUR)"] == 150
    assert row_0280["meta"]["external_model_anchor_pn"] == "fr-0280"
    assert row_0280["meta"]["external_model_anchor_lens_signature"] == ["0280"]
    assert row_0280["final_values"]["FOB C(EUR)"] == 100
    assert len(cache) == 2


def test_external_model_anchor_rejects_lens_mismatch(monkeypatch):
    external = "DH-IPC-HFW3449T-ASE"
    _install_engine(
        monkeypatch,
        [
            {
                "Part No.": "fr-0280",
                "Internal Model": "DH-IPC-HFW3449TP-ASE-0280B",
                "External Model": external,
                **_prices(100),
            }
        ],
    )
    row = _row("DH-IPC-HFW3449TP-ASE-0600B", external, start=7)

    changed = app_main._apply_external_model_anchor_to_row(
        row, apply_france_anchor=True, anchor_cache={}
    )

    assert changed is False
    assert row["meta"]["external_model_anchor_applied"] is False
    assert row["meta"]["external_model_anchor_rejection_reason"] == "lens_mismatch"
    assert "external_model_anchor_rejected_lens_mismatch" in row["warnings"]
    assert row["final_values"]["FOB C(EUR)"] == 7


def test_external_model_anchor_rejects_ambiguous_models_without_lens(monkeypatch):
    external = "DHI-GENERIC-MODEL"
    _install_engine(
        monkeypatch,
        [
            {
                "Part No.": "fr-left",
                "Internal Model": "DHI-GENERIC-MODEL-LEFT",
                "External Model": external,
                **_prices(100),
            },
            {
                "Part No.": "fr-right",
                "Internal Model": "DHI-GENERIC-MODEL-RIGHT",
                "External Model": external,
                **_prices(150),
            },
        ],
    )
    row = _row("DHI-GENERIC-MODEL", external, start=9)

    changed = app_main._apply_external_model_anchor_to_row(
        row, apply_france_anchor=True, anchor_cache={}
    )

    assert changed is False
    assert row["meta"]["external_model_anchor_applied"] is False
    assert (
        row["meta"]["external_model_anchor_rejection_reason"]
        == "ambiguous_external_model_after_lens_filter"
    )
    assert row["final_values"]["FOB C(EUR)"] == 9
