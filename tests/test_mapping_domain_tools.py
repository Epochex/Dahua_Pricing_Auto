from __future__ import annotations

from pathlib import Path

import pandas as pd

from backend.app.mapping_case_memory import MappingCaseMemory
from backend.app.mapping_domain_tools import build_mapping_domain_tools
from backend.app.mapping_evidence import EvidenceRecord
from backend.app.mapping_investigation import MappingInvestigationStore
from backend.engine.core.loader import DataBundle, normalize_pn_base, normalize_pn_raw
from backend.engine.engine import EngineConfig, PricingEngine


def _index(values: list[str]) -> tuple[dict[str, int], dict[str, int]]:
    return (
        {normalize_pn_raw(value): index for index, value in enumerate(values)},
        {normalize_pn_base(value): index for index, value in enumerate(values)},
    )


def _engine(tmp_path: Path) -> PricingEngine:
    country = pd.DataFrame(
        [
            {
                "Part No.": "PN-1",
                "Internal Model": "CAM100-A",
                "External Model": "CAM100",
                "Product Line": "Generic Camera",
                "Description": "fixed camera",
            },
            {
                "Part No.": "PN-2",
                "Internal Model": "CAM101-A",
                "External Model": "CAM101",
                "Product Line": "Generic Camera",
                "Description": "similar fixed camera",
            },
        ]
    )
    system = pd.DataFrame(
        [
            {
                "Part Num": "PN-1",
                "Internal Model": "CAM100-A",
                "External Model": "CAM100",
                "First Product Line": "Generic Camera",
            },
            {
                "Part Num": "PN-2",
                "Internal Model": "CAM101-A",
                "External Model": "CAM101",
                "First Product Line": "Generic Camera",
            },
        ]
    )
    country_map = pd.DataFrame(
        [
            {
                "rule_id": "country-camera",
                "priority": 1,
                "field1": "Product Line",
                "match_type1": "equals",
                "pattern1": "Generic Camera",
                "category": "IPC",
                "price_group_hint": "IPC",
            }
        ]
    )
    system_map = pd.DataFrame(
        [
            {
                "rule_id": "system-camera",
                "priority": 1,
                "field1": "First Product Line",
                "match_type1": "equals",
                "pattern1": "Generic Camera",
                "category": "PTZ",
                "price_group_hint": "PTZ",
            }
        ]
    )
    fr_raw, fr_base = _index(["PN-1", "PN-2"])
    sys_raw, sys_base = _index(["PN-1", "PN-2"])
    bundle = DataBundle(
        france_df=country,
        sys_df=system,
        map_fr=country_map,
        map_sys=system_map,
        fr_idx_raw=fr_raw,
        fr_idx_base=fr_base,
        sys_idx_raw=sys_raw,
        sys_idx_base=sys_base,
    )
    engine = PricingEngine(EngineConfig(runtime_dir=tmp_path))
    engine.install_data(bundle, version="v1")
    return engine


def _memory(tmp_path: Path) -> MappingCaseMemory:
    store = MappingInvestigationStore(tmp_path)
    case = store.create(
        subject_ref="product:PN-OLD",
        data_version="v1",
        verification={"selected_category": "PTZ", "status": "WARN", "signals": []},
    )
    corrected = store.triage(
        case["case_id"],
        action="correct_mapping",
        reviewer="user:owner",
        rationale_code="known_family",
        corrected_category="IPC",
        expected_revision=case["revision"],
    )
    memory = MappingCaseMemory(tmp_path)
    memory.remember(corrected, family_ref="family:cam", scope="model_family")
    return memory


def test_domain_tools_use_live_data_memory_and_document_evidence(tmp_path: Path) -> None:
    documents = [
        EvidenceRecord(
            evidence_id="document:camera-family",
            source_type="product_document",
            source_version="v1",
            authority="official_product_document",
            subject_ref="product:PN-1",
            family_ref="family:cam",
            content="CAM100 belongs to the IPC fixed-camera family.",
            attributes={"product_line": "IPC"},
        )
    ]
    tools = build_mapping_domain_tools(
        engine=_engine(tmp_path),
        memory=_memory(tmp_path),
        document_records=documents,
    )

    candidates = tools.invoke_read_only("mapping.get_candidates", {"pn": "PN-1"})
    attributes = tools.invoke_read_only("catalog.get_product_attributes", {"pn": "PN-1"})
    comparison = tools.invoke_read_only("price_data.compare_sources", {"pn": "PN-1"})
    similar = tools.invoke_read_only(
        "catalog.search_similar_products", {"pn": "PN-1", "limit": 5}
    )
    history = tools.invoke_read_only(
        "history.search_approved_cases", {"family_ref": "family:cam", "data_version": "v1"}
    )
    documents_result = tools.invoke_read_only(
        "documents.search_product_documents",
        {
            "subject_ref": "product:PN-1",
            "family_ref": "family:cam",
            "query": "CAM100 IPC fixed camera",
            "source_version": "v1",
        },
    )

    assert candidates["verification"]["status"] == "WARN"
    assert attributes["selected_category"] == "IPC"
    assert comparison["summary_code"] == "price_sources_conflict"
    assert similar["items"][0]["pn"] == "PN-2"
    assert history["count"] == 1
    assert documents_result["evidence_refs"][0] == "document:camera-family"
