import pytest

from backend.engine.core.pricing_engine import (
    compute_channel_prices,
    compute_ddp_a_from_fob,
    compute_fob_from_manual_price_field,
)


def _reverse(field, value):
    rule = dict(reseller=0.12, gold=0.22, silver=0.30, ivory=0.35, msrp_on_installer=0.50)
    return compute_fob_from_manual_price_field(
        field,
        value,
        category="IPC",
        price_rule_dict=rule,
        price_group="IPC",
        effective_price_group="IPC",
        price_rule_key="_default_",
        series_display="",
        france_row=None,
        sys_row=None,
    )[0]


def test_manual_gold_price_reverses_to_fob():
    rule = dict(reseller=0.12, gold=0.22, silver=0.30, ivory=0.35, msrp_on_installer=0.50)
    fob = 100.0
    ddp = compute_ddp_a_from_fob(fob, "IPC")
    gold = compute_channel_prices(ddp, rule)["Gold(EUR)"]

    assert _reverse("Gold(EUR)", gold) == pytest.approx(fob)


def test_manual_msrp_price_reverses_through_ivory_and_ddp_to_fob():
    rule = dict(reseller=0.12, gold=0.22, silver=0.30, ivory=0.35, msrp_on_installer=0.50)
    fob = 100.0
    ddp = compute_ddp_a_from_fob(fob, "IPC")
    msrp = compute_channel_prices(ddp, rule)["MSRP(EUR)"]

    assert _reverse("MSRP(EUR)", msrp) == pytest.approx(fob)


def test_manual_sys_basis_price_uses_existing_basis_to_fob_formula():
    fob = _reverse("Sys Basis Price Used", 100.0)

    assert fob == pytest.approx(90.0)
