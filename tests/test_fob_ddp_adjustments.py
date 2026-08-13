import pytest

from backend.app import main
from backend.engine.core.pricing_engine import (
    _compute_fob_from_ddp_a,
    compute_ddp_a_from_fob,
    compute_fob_from_manual_price_field,
)
from backend.engine.core.pricing_rules import DDP_RULES


def _fob_from_sys_basis(value: float, category: str = "IPC") -> float:
    return compute_fob_from_manual_price_field(
        "Sys Basis Price Used",
        value,
        category=category,
        price_rule_dict=None,
        price_group="IPC",
        effective_price_group="IPC",
        price_rule_key="_default_",
        series_display="",
        france_row=None,
        sys_row=None,
    )[0]


def test_fob_euro_adjust_is_applied_per_category_after_basis_to_fob_calculation(monkeypatch):
    monkeypatch.setitem(DDP_RULES, "IPC", (0.10, 0.008, 0.02, 0.000198, 0.0, 0.15))
    monkeypatch.setitem(DDP_RULES, "HAC", (0.10, 0.008, 0.02, 0.000198, 0.0, 0.0))

    assert _fob_from_sys_basis(100.0) == pytest.approx(100.0 * 0.9 * 1.15)
    assert _fob_from_sys_basis(100.0, "HAC") == pytest.approx(100.0 * 0.9)


def test_ddp_adjust_is_applied_after_m1_through_m4_and_reverses_cleanly(monkeypatch):
    monkeypatch.setitem(DDP_RULES, "IPC", (0.10, 0.008, 0.02, 0.000198, 0.15, 0.0))
    fob = 100.0

    ddp = compute_ddp_a_from_fob(fob, "IPC")

    assert ddp == pytest.approx(fob * 1.10 * 1.008 * 1.02 * 1.000198 * 1.15)
    assert _compute_fob_from_ddp_a(ddp, "IPC") == pytest.approx(fob)


def test_legacy_four_value_ddp_payload_gets_zero_adjust():
    normalized = main._normalize_ddp_rules_payload({"IPC": [0.10, 0.008, 0.02, 0.000198]})

    assert normalized["IPC"] == pytest.approx((0.10, 0.008, 0.02, 0.000198, 0.0, 0.0))
