from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BRIDGE = ROOT / "script" / "google_sheets_agent_bridge.gs"


def test_bridge_has_realtime_and_reconciliation_triggers() -> None:
    source = BRIDGE.read_text(encoding="utf-8")
    assert '.onEdit()' in source
    assert '.everyMinutes(5)' in source
    assert 'editDebounceMillis' in source


def test_bridge_has_idempotency_and_stale_row_protection() -> None:
    source = BRIDGE.read_text(encoding="utf-8")
    assert 'snapshotHash' in source
    assert 'idempotencyKey' in source
    assert 'validateStatusUpdate_' in source
    assert 'PLA mismatch' in source


def test_bridge_cannot_send_dingtalk_group_messages() -> None:
    source = BRIDGE.read_text(encoding="utf-8").lower()
    assert 'oapi.dingtalk.com/robot/send' not in source
    assert 'sessionwebhook' not in source
