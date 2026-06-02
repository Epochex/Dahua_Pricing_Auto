from backend.engine.core.pricing_engine import resolve_price_group_for_rules


def test_overseas_series_does_not_imply_eas():
    resolved = resolve_price_group_for_rules(
        "IPC",
        "IPC3",
        "Cameras for Overseas Distribution Channels",
    )

    assert resolved == "IPC"


def test_eas_token_still_resolves_to_eas():
    resolved = resolve_price_group_for_rules(
        "ACCESSORY",
        "",
        "Electronic Anti-theft System (EAS)",
    )

    assert resolved == "EAS"
