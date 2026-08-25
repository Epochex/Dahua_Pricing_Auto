import pytest
from fastapi import HTTPException

from backend.app import main


def test_existing_admin_write_is_fail_closed_and_accepts_configured_token(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "UPLIFT_CFG", tmp_path / "uplift.json")
    monkeypatch.setenv("DAHUA_PRICE_DATA_ADMIN_TOKEN", "correct-token")
    with pytest.raises(HTTPException) as denied:
        main._require_price_data_admin_token(None)
    assert denied.value.status_code == 403
    assert "correct-token" not in str(denied.value.detail)

    main._require_price_data_admin_token("correct-token")
    assert main.admin_put_uplift({"CCTV": 0.03}) == {"ok": True, "count": 1}

    route = next(route for route in main.app.routes if getattr(route, "path", None) == "/api/admin/uplift" and "PUT" in route.methods)
    assert any(dep.call is main._require_price_data_admin_token for dep in route.dependant.dependencies)


def test_admin_write_is_denied_when_token_not_configured(monkeypatch):
    monkeypatch.delenv("DAHUA_PRICE_DATA_ADMIN_TOKEN", raising=False)
    with pytest.raises(HTTPException) as denied:
        main._require_price_data_admin_token("anything")
    assert denied.value.status_code == 403
