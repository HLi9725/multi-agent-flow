import os
import pytest
import json
import hashlib
import time
import subprocess
import types
from dataclasses import FrozenInstanceError
from concurrent.futures import ThreadPoolExecutor

from scripts._lib.core.agent_schema import (
    HostCapabilities, AgentHandle, AgentResult, AgentStatus, ConfirmationResult, CapabilitySupport
)
from scripts._lib.core.evidence_schema import (
    EvidenceRecord, EvidenceMetadata, ArtifactRecord,
    EvidenceType, EvidenceSecurityError, EvidenceIntegrityError, EvidenceGateError, freeze_value
)
from scripts._lib.core.evidence_store import EvidenceStore, canonical_json
from scripts._lib.core.evidence_gate import EvidenceGate, EvidenceValidationContext

@pytest.fixture
def store(tmp_path):
    store_dir = tmp_path / "evidence"
    return EvidenceStore(str(store_dir))

@pytest.fixture
def caps():
    return HostCapabilities(is_real_host=True)

@pytest.fixture
def dummy_metadata(caps):
    extra = {"safe": "value"}
    for k, v in caps.__dict__.items():
        if k != "extra":
            extra[f"capability_{k}"] = v

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
        extra=extra
    )

@pytest.fixture
def dummy_record(dummy_metadata):
    return EvidenceRecord(
        evidence_id="evt_123",
        evidence_type=EvidenceType.TASK_START,
        baseline_commit="abc",
        result_commit="def",
        artifacts=(),
        metadata=dummy_metadata
    )

@pytest.fixture
def expected_context(caps):
    handle = AgentHandle(session_id="session_xyz", host_id="real_host", is_real_host=True)
    # Using object.__setattr__ because Handle is frozen, and invocation_id isn't in default schema
    object.__setattr__(handle, 'invocation_id', 'inv_xyz')

    res = AgentResult(session_id="session_xyz", status=AgentStatus.SUCCESS, output="done", is_real_host=True)

    return EvidenceValidationContext(
        project_id="proj1",
        task_id="T001",
        actor_role="DEV",
        transition_from="TODO",
        transition_to="IN_PROGRESS",
        baseline_commit="abc",
        result_commit="def",
        host_handle=handle,
        expected_capabilities=caps,
        agent_result=res
    )

# 1. append/read
def test_store_append_and_read(store, dummy_record):
    hash_val = store.append(dummy_record)
    assert hash_val is not None
    loaded = store.read("evt_123")
    assert loaded.evidence_id == "evt_123"
    assert loaded.content_hash == hash_val
    assert loaded.metadata.is_real_host is True

# 2. Overwrite protection
def test_store_no_overwrite(store, dummy_record):
    store.append(dummy_record)
    with pytest.raises(EvidenceSecurityError, match="already exists"):
        store.append(dummy_record)

# 3. Path traversal
def test_store_path_traversal(store, dummy_metadata):
    bad_record = EvidenceRecord(
        evidence_id="../evt_evil",
        evidence_type=EvidenceType.TASK_START,
        baseline_commit="abc",
        result_commit="def",
        artifacts=(),
        metadata=dummy_metadata
    )
    with pytest.raises(EvidenceSecurityError, match="Invalid evidence_id format"):
        store.append(bad_record)

# 4. Integrity tamper
def test_store_integrity(store, dummy_record):
    store.append(dummy_record)
    path = os.path.join(store.root_dir, "evt_123.json")
    with open(path, 'r', encoding='utf-8') as f:
        data = json.load(f)
    data["baseline_commit"] = "hacked"
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(data, f)
    with pytest.raises(EvidenceIntegrityError, match="integrity compromised"):
        store.read("evt_123")

# 5. Fake Host Rejection
def test_gate_reject_fake_session(store, expected_context, dummy_metadata, tmp_path):
    gate = EvidenceGate(store, str(tmp_path))
    # Replace metadata session to a fake one
    fake_meta = EvidenceMetadata(**{**dummy_metadata.__dict__, "host_session_id": "test-session"})
    rec = EvidenceRecord(evidence_id="evt_fake", evidence_type=EvidenceType.TASK_START, baseline_commit="abc", result_commit="def", artifacts=(), metadata=fake_meta)
    store.append(rec)
    with pytest.raises(EvidenceGateError, match="fake/test/mock/simulate"):
        gate.validate_evidence("evt_fake", expected_context)

# 6. Valid Artifact Check
def test_gate_validate_artifact(store, expected_context, dummy_metadata, tmp_path):
    art_path = tmp_path / "test_file.txt"
    content = b"artifact content"
    art_path.write_bytes(content)
    file_hash = hashlib.sha256(content).hexdigest()
    rec = EvidenceRecord(
        evidence_id="evt_art",
        evidence_type=EvidenceType.TASK_COMPLETE,
        baseline_commit="abc",
        result_commit="def",
        artifacts=(ArtifactRecord(relative_path="test_file.txt", sha256_hash=file_hash),),
        metadata=dummy_metadata
    )
    store.append(rec)
    gate = EvidenceGate(store, str(tmp_path))
    assert gate.validate_evidence("evt_art", expected_context) is True

# 7. Artifact Tampering
def test_gate_reject_tampered_artifact(store, expected_context, dummy_metadata, tmp_path):
    art_path = tmp_path / "test_file2.txt"
    content = b"artifact content"
    art_path.write_bytes(content)
    file_hash = hashlib.sha256(content).hexdigest()
    rec = EvidenceRecord(
        evidence_id="evt_art2",
        evidence_type=EvidenceType.TASK_COMPLETE,
        baseline_commit="abc",
        result_commit="def",
        artifacts=(ArtifactRecord(relative_path="test_file2.txt", sha256_hash=file_hash),),
        metadata=dummy_metadata
    )
    store.append(rec)
    art_path.write_bytes(b"tampered content")
    gate = EvidenceGate(store, str(tmp_path))
    with pytest.raises(EvidenceGateError, match="Artifact hash mismatch"):
        gate.validate_evidence("evt_art2", expected_context)

# 8. Secret Masking + Env vars
def test_secret_masking(store, dummy_metadata):
    meta = EvidenceMetadata(
        **{**dummy_metadata.__dict__, "extra": {
            "invocation_token": "secret123",
            "access_token": "token",
            "API_KEY": "apikey123",
            "AWS_ACCESS_KEY_ID": "AKIAXXXXXX",
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
            "access_token": "token",
            "AWS_ACCESS_KEY_ID": "AKIAXXXXXX",
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
    assert loaded.metadata.extra["access_token"] == "***MASKED***"
    assert loaded.metadata.extra["AWS_ACCESS_KEY_ID"] == "***MASKED***"
    assert loaded.metadata.extra["nested"][0]["cookie"] == "***MASKED***"
    assert loaded.metadata.extra["nested"][1] == "***MASKED***"
    assert loaded.metadata.extra["output"] == "***MASKED***"

# 9. Deep Immutability (no set/frozenset)
def test_canonical_json_and_immutability():
    meta = EvidenceMetadata(
        project_id="proj1", task_id="T001", actor_role="DEV", host_id="host1", adapter="ad1",
        host_session_id="s1", host_invocation_id="i1", is_real_host=True, workspace_mode="x",
        transition_from="a", transition_to="b", created_at=0, extra={"nested": {"k": "v"}, "arr": [1, 2]}
    )
    with pytest.raises(TypeError): # MappingProxyType doesn't support item assignment
        meta.extra["nested"]["k"] = "modified"
    assert isinstance(meta.extra["arr"], tuple)

    b1 = canonical_json({"a": 1, "c": meta})
    b2 = canonical_json({"c": meta, "a": 1})
    assert b1 == b2

# 10. Concurrency collision override
def test_store_concurrency_collision_and_override(store, dummy_record):
    def append_task():
        try:
            return store.append(dummy_record)
        except Exception as e:
            return e

    with ThreadPoolExecutor(max_workers=5) as executor:
        results = list(executor.map(lambda _: append_task(), range(5)))

    successes = [r for r in results if isinstance(r, str)]
    failures = [r for r in results if isinstance(r, Exception)]

    assert len(successes) == 1
    assert len(failures) == 4
    for f in failures:
        assert isinstance(f, EvidenceSecurityError)
        assert "already exists" in str(f)

# 11. Empty sessions/invocations rejected by Gate
def test_gate_empty_fields(store, dummy_metadata, expected_context, tmp_path):
    gate = EvidenceGate(store, str(tmp_path))
    meta = EvidenceMetadata(**{**dummy_metadata.__dict__, "host_session_id": ""})
    rec = EvidenceRecord(evidence_id="evt_empty", evidence_type=EvidenceType.TASK_START, baseline_commit="abc", result_commit="def", artifacts=(), metadata=meta)
    store.append(rec)
    with pytest.raises(EvidenceGateError, match="Critical identifier field is empty"):
        gate.validate_evidence("evt_empty", expected_context)

# 12. Context mismatch checks (None != None)
def test_gate_context_mismatch_none(store, dummy_metadata, expected_context, tmp_path):
    gate = EvidenceGate(store, str(tmp_path))
    # Context modified to have None for actor_role (which is illegal)
    bad_context = EvidenceValidationContext(
        **{**expected_context.__dict__, "actor_role": None}
    )
    rec = EvidenceRecord(evidence_id="evt_cx1", evidence_type=EvidenceType.TASK_START, baseline_commit="abc", result_commit="def", artifacts=(), metadata=dummy_metadata)
    store.append(rec)
    with pytest.raises(EvidenceGateError, match="None is not allowed"):
        gate.validate_evidence("evt_cx1", bad_context)

# 13. Model simulated User Confirmation rejection
def test_gate_model_confirmation(store, dummy_metadata, expected_context, tmp_path):
    gate = EvidenceGate(store, str(tmp_path))
    meta = EvidenceMetadata(
        **{**dummy_metadata.__dict__, "extra": {
            **dummy_metadata.extra,
            "confirmation_id": "c123",
            "confirmed_at": 1234567,
            "user_source": "model"
        }}
    )
    conf_res = ConfirmationResult(request_id="c123", selected_option="yes", is_confirmed=True, is_real_host=True)
    c_context = EvidenceValidationContext(
        **{**expected_context.__dict__, "confirmation_result": conf_res}
    )
    rec = EvidenceRecord(evidence_id="evt_conf", evidence_type=EvidenceType.USER_CONFIRMATION, baseline_commit="abc", result_commit="def", artifacts=(), metadata=meta)
    store.append(rec)
    with pytest.raises(EvidenceGateError, match="is not whitelisted"):
        gate.validate_evidence("evt_conf", c_context)

# 14. Symlink / Junction test
def test_gate_junction_escape(store, dummy_metadata, expected_context, tmp_path):
    gate = EvidenceGate(store, str(tmp_path))

    ext_dir = tmp_path / "external"
    ext_dir.mkdir()
    ext_file = ext_dir / "target.txt"
    ext_file.write_bytes(b"evil content")
    file_hash = hashlib.sha256(b"evil content").hexdigest()

    project_root = tmp_path / "project"
    project_root.mkdir()

    link_path = project_root / "linked"
    if os.name == 'nt':
        res = subprocess.run(f'cmd /c mklink /J "{link_path}" "{ext_dir}"', shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        assert res.returncode == 0
    else:
        os.symlink(ext_dir, link_path)

    gate.project_root = os.path.realpath(str(project_root))

    rec = EvidenceRecord(
        evidence_id="evt_junc",
        evidence_type=EvidenceType.TASK_COMPLETE,
        baseline_commit="abc",
        result_commit="def",
        artifacts=(ArtifactRecord(relative_path="linked/target.txt", sha256_hash=file_hash),),
        metadata=dummy_metadata
    )
    store.append(rec)
    with pytest.raises(EvidenceGateError, match="traversal"):
        gate.validate_evidence("evt_junc", expected_context)
