from pathlib import Path

import pytest

from backend.app.mapping_document_store import MappingDocumentStore, MappingDocumentStoreError


def _record() -> dict:
    return {
        "evidence_id": "official-doc:IPC-HFW-1:v3",
        "source_type": "product_manual",
        "source_version": "catalog-v3",
        "authority": "official_product_document",
        "subject_ref": "product:IPC-HFW-1",
        "family_ref": "family:IPC-HFW",
        "content": "IPC-HFW-1 belongs to the IPC product line.",
        "attributes": {"product_line": "IPC", "model_family": "IPC-HFW"},
        "approved": True,
    }


def test_document_store_is_versioned_immutable_and_idempotent(tmp_path: Path) -> None:
    store = MappingDocumentStore(tmp_path)
    first = store.register(_record())
    replay = store.register(_record())

    assert first["content_hash"].startswith("sha256:")
    assert replay["idempotent_replay"] is True
    assert store.list(subject_ref="product:IPC-HFW-1")["count"] == 1
    assert store.records()[0].product_line == "IPC"

    changed = {**_record(), "content": "changed"}
    with pytest.raises(MappingDocumentStoreError, match="immutable content"):
        store.register(changed)
