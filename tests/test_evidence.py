import os
import pytest
import json
import hashlib
import time
from dataclasses import FrozenInstanceError

from scripts._lib.core.evidence_schema import (
    EvidenceRecord, EvidenceMetadata, ArtifactRecord,
    EvidenceType, EvidenceSecurityError, EvidenceIntegrityError, EvidenceGateError, freeze_value
)
from scripts._lib.core.evidence_store import EvidenceStore, canonical_json
from scripts._lib.core.evidence_gate import EvidenceGate

@pytest.fixture
def store(tmp_path):
    store_dir = tmp_path / "evidence"
    return EvidenceStore(str(store_dir))

@pytest.fixture
def dummy_metadata():
    return EvidenceMetadata(
        project_id="proj1",
        task_id="T001",
        actor_role="DEV",
        host_id="real_host",
        adapter="real_adapter",
        host_session_id="session_xyz",
        host_invocation_id="inv_xyz",
        is_real_host=True,
        workspace_mode="branch",
        transition_from="TODO",
        transition_to="IN_PROGRESS",
        created_at=time.time(),
        extra={"safe": "value"}
    )

@pytest.fixture
def expected_context(dummy_metadata):
    return {
        "project_id": "proj1",
        "task_id": "T001",
        "actor_role": "DEV",
        "host_id": "real_host",
        "adapter": "real_adapter",
        "transition_from": "TODO",
        "transition_to": "IN_PROGRESS",
        "baseline_commit": "abc",
        "result_commit": "def"
    }

def test_canonical_json_and_immutability():
    # Deep Immutability
    meta = EvidenceMetadata(
        project_id="proj1", task_id="T001", actor_role="DEV", host_id="host1", adapter="ad1",
        host_session_id="s1", host_invocation_id="i1", is_real_host=True, workspace_mode="x",
        transition_from="a", transition_to="b", created_at=0, extra={"nested": {"k": "v"}}
    )
    with pytest.raises(TypeError): # MappingProxyType doesn't support item assignment
        meta.extra["nested"]["k"] = "modified"

    # Same object gives exact same canonical bytes
    b1 = canonical_json({"a": 1, "c": meta})
    b2 = canonical_json({"c": meta, "a": 1})
    assert b1 == b2

def test_secret_masking(store, dummy_metadata):
    meta = EvidenceMetadata(
        **{**dummy_metadata.__dict__, "extra": {
            "invocation_token": "secret123",
            "authorization": "Bearer token",
            "nested": [{"cookie": "sess=1"}, "mongodb://user:pass@host"]
        }}
    )
    rec = EvidenceRecord(
        evidence_id="evt_sec", evidence_type=EvidenceType.TASK_START,
        baseline_commit="abc", result_commit="def", artifacts=(), metadata=meta
    )
    with pytest.raises(EvidenceSecurityError, match="invocation_token must be rejected"):
        store.append(rec)

    meta2 = EvidenceMetadata(
        **{**dummy_metadata.__dict__, "extra": {
            "authorization": "Bearer token",
            "nested": [{"cookie": "sess=1"}, "mongodb://user:pass@host"],
            "output": "Some log with Authorization: Bearer TOPSECRET and more"
        }}
    )
    rec2 = EvidenceRecord(
        evidence_id="evt_sec2", evidence_type=EvidenceType.TASK_START,
        baseline_commit="abc", result_commit="def", artifacts=(), metadata=meta2
    )
    store.append(rec2)
    loaded = store.read("evt_sec2")
    assert loaded.metadata.extra["authorization"] == "***MASKED***"
    assert loaded.metadata.extra["nested"][0]["cookie"] == "***MASKED***"
    assert loaded.metadata.extra["nested"][1] == "***MASKED***"
    assert loaded.metadata.extra["output"] == "***MASKED***"

def test_store_concurrency_collision_and_override(store, dummy_metadata):
    rec = EvidenceRecord(
        evidence_id="evt_coll", evidence_type=EvidenceType.TASK_START,
        baseline_commit="abc", result_commit="def", artifacts=(), metadata=dummy_metadata
    )
    store.append(rec)
    with pytest.raises(EvidenceSecurityError, match="Forbidden|forbidden"):
        store.append(rec)

def test_gate_rejects_fake_and_mismatch(store, expected_context, dummy_metadata, tmp_path):
    gate = EvidenceGate(store, str(tmp_path))

    # Fake session
    m1 = EvidenceMetadata(**{**dummy_metadata.__dict__, "host_session_id": "test-session"})
    r1 = EvidenceRecord(evidence_id="e1", evidence_type=EvidenceType.TASK_START, baseline_commit="abc", result_commit="def", artifacts=(), metadata=m1)
    store.append(r1)
    with pytest.raises(EvidenceGateError, match="fake/test/mock/simulate"):
        gate.validate_evidence("e1", expected_context)

    # Context mismatch
    m2 = EvidenceMetadata(**{**dummy_metadata.__dict__, "actor_role": "QA"})
    r2 = EvidenceRecord(evidence_id="e2", evidence_type=EvidenceType.TASK_START, baseline_commit="abc", result_commit="def", artifacts=(), metadata=m2)
    store.append(r2)
    with pytest.raises(EvidenceGateError, match="Context mismatch for actor_role"):
        gate.validate_evidence("e2", expected_context)

    # Missing artifacts on COMPLETE
    r3 = EvidenceRecord(evidence_id="e3", evidence_type=EvidenceType.TASK_COMPLETE, baseline_commit="abc", result_commit="def", artifacts=(), metadata=dummy_metadata)
    store.append(r3)
    with pytest.raises(EvidenceGateError, match="TASK_COMPLETE missing artifacts"):
        gate.validate_evidence("e3", expected_context)

def test_artifact_path_escapes(store, expected_context, dummy_metadata, tmp_path):
    gate = EvidenceGate(store, str(tmp_path))

    r1 = EvidenceRecord(evidence_id="e_art1", evidence_type=EvidenceType.TASK_COMPLETE, baseline_commit="abc", result_commit="def", artifacts=(
        ArtifactRecord(relative_path="../project_evil/file.txt", sha256_hash=""),
    ), metadata=dummy_metadata)
    store.append(r1)
    with pytest.raises(EvidenceGateError, match="Invalid relative path|traversal"):
        gate.validate_evidence("e_art1", expected_context)
