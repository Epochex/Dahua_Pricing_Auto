from __future__ import annotations

import pandas as pd

from backend.engine.core.classifier import collect_mapping_matches
from backend.engine.core.mapping_verifier import (
    STATUS_BLOCK,
    STATUS_PASS,
    STATUS_WARN,
    MappingVerifier,
)


def _mapping(*rows: dict) -> pd.DataFrame:
    return pd.DataFrame(list(rows))


def test_collect_mapping_matches_keeps_every_match_and_provenance() -> None:
    row = pd.Series({"Product Line": "Camera IPC", "Region": "EU"})
    mapping = _mapping(
        {
            "rule_id": "broad-camera",
            "priority": 1,
            "field1": "Product Line",
            "match_type1": "contains",
            "pattern1": "Camera",
            "category": "PTZ",
            "price_group_hint": "PTZ",
        },
        {
            "rule_id": "exact-ipc-eu",
            "priority": 2,
            "field1": "Product Line",
            "match_type1": "equals",
            "pattern1": "Camera IPC",
            "field2": "Region",
            "match_type2": "equals",
            "pattern2": "EU",
            "category": "IPC",
            "price_group_hint": "IPC",
        },
    )

    matches = collect_mapping_matches(row, mapping)

    assert [item.rule_id for item in matches] == ["broad-camera", "exact-ipc-eu"]
    assert matches[0].specificity == 1
    assert matches[1].specificity == 4


def test_verifier_allows_category_supported_by_strong_model_evidence() -> None:
    row = pd.Series({"Internal Model": "DH-IPC-HFW3449", "External Model": "DH-IPC-HFW3449"})

    result = MappingVerifier().verify(
        selected_category="IPC",
        selected_price_group="IPC",
        country_row=None,
        system_row=row,
        country_mapping=pd.DataFrame(),
        system_mapping=pd.DataFrame(),
    )

    assert result.status == STATUS_PASS
    assert result.recommended_action == "continue_deterministic_pricing"


def test_verifier_blocks_unknown_mapping() -> None:
    result = MappingVerifier().verify(
        selected_category="UNKNOWN",
        selected_price_group=None,
        country_row=None,
        system_row=None,
        country_mapping=pd.DataFrame(),
        system_mapping=pd.DataFrame(),
    )

    assert result.status == STATUS_BLOCK
    assert {item.code for item in result.signals} == {"missing_mapping"}


def test_verifier_blocks_selected_category_that_conflicts_with_model_evidence() -> None:
    row = pd.Series({"Internal Model": "DH-IPC-HFW3449", "External Model": "DH-IPC-HFW3449"})

    result = MappingVerifier().verify(
        selected_category="PTZ",
        selected_price_group="PTZ",
        country_row=None,
        system_row=row,
        country_mapping=pd.DataFrame(),
        system_mapping=pd.DataFrame(),
    )

    assert result.status == STATUS_BLOCK
    assert "strong_model_evidence_conflict" in {item.code for item in result.signals}


def test_verifier_warns_when_sources_disagree() -> None:
    country_row = pd.Series({"Line": "Generic Camera"})
    system_row = pd.Series({"Line": "Generic Camera"})
    country_mapping = _mapping(
        {
            "rule_id": "country-camera",
            "priority": 1,
            "field1": "Line",
            "match_type1": "equals",
            "pattern1": "Generic Camera",
            "category": "IPC",
        }
    )
    system_mapping = _mapping(
        {
            "rule_id": "system-camera",
            "priority": 1,
            "field1": "Line",
            "match_type1": "equals",
            "pattern1": "Generic Camera",
            "category": "PTZ",
        }
    )

    result = MappingVerifier().verify(
        selected_category="IPC",
        selected_price_group="IPC",
        country_row=country_row,
        system_row=system_row,
        country_mapping=country_mapping,
        system_mapping=system_mapping,
    )

    assert result.status == STATUS_WARN
    assert "cross_source_mapping_conflict" in {item.code for item in result.signals}


def test_verifier_warns_on_family_outlier() -> None:
    row = pd.Series({"Internal Model": "GENERIC-1"})
    mapping = _mapping(
        {
            "rule_id": "generic",
            "priority": 1,
            "field1": "Internal Model",
            "match_type1": "equals",
            "pattern1": "GENERIC-1",
            "category": "PTZ",
        }
    )

    result = MappingVerifier().verify(
        selected_category="PTZ",
        selected_price_group="PTZ",
        country_row=row,
        system_row=None,
        country_mapping=mapping,
        system_mapping=pd.DataFrame(),
        family_categories=["IPC", "IPC", "IPC", "IPC"],
    )

    assert result.status == STATUS_WARN
    assert "family_category_outlier" in {item.code for item in result.signals}
