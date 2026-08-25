import os
import pytest
import json
import hashlib

from scripts._lib.core.evidence_schema import (
    EvidenceRecord, EvidenceMetadata, ArtifactRecord,
    EvidenceType, EvidenceSecurityError, EvidenceIntegrityError, EvidenceGateError
)
from scripts._lib.core.evidence_store import EvidenceStore, canonical_json
from scripts._lib.core.evidence_gate import EvidenceGate

@pytest.fixture
def store(tmp_path):
    store_dir = tmp_path / "evidence"
    return EvidenceStore(str(store_dir))

@pytest.fixture
def dummy_record():
    return EvidenceRecord(
        evidence_id="evt_123",
        evidence_type=EvidenceType.TASK_START,
        baseline_commit="abc",
        result_commit="def",
        artifacts=[],
        metadata=EvidenceMetadata(
            actor_role="DEV",
            host_id="real_host",
            host_session_id="session_xyz",
            host_invocation_id="inv_xyz",
            is_real_host=True,
            transition_from="TODO",
            transition_to="IN_PROGRESS"
        )
    )

def test_canonical_json():
    data = {"b": 2, "a": {"z": 1, "token": "secret"}, "c": ["hello", "world"]}
    b = canonical_json(data)
    assert b == b'{"a":{"token":"secret","z":1},"b":2,"c":["hello","world"]}'

def test_store_append_and_read(store, dummy_record):
    hash_val = store.append(dummy_record)
    assert hash_val is not None

    loaded = store.read("evt_123")
    assert loaded.evidence_id == "evt_123"
    assert loaded.content_hash == hash_val
    assert loaded.metadata.is_real_host is True

def test_store_no_overwrite(store, dummy_record):
    store.append(dummy_record)
    with pytest.raises(EvidenceSecurityError, match="Overwriting is forbidden"):
        store.append(dummy_record)

def test_store_path_traversal(store, dummy_record):
    bad_record = EvidenceRecord(
        evidence_id="../evt_evil",
        evidence_type=EvidenceType.TASK_START,
        baseline_commit="abc",
        result_commit="def",
        artifacts=[],
        metadata=dummy_record.metadata
    )
    with pytest.raises(EvidenceSecurityError, match="Invalid evidence_id format or path traversal attempt."):
        store.append(bad_record)

def test_store_secret_masking(store, dummy_record):
    rec = EvidenceRecord(
        evidence_id="evt_sec",
        evidence_type=EvidenceType.TASK_START,
        baseline_commit="abc",
        result_commit="def",
        artifacts=[],
        metadata=EvidenceMetadata(
            actor_role="DEV",
            host_id="real_host",
            host_session_id="session_xyz",
            host_invocation_id="inv_xyz",
            is_real_host=True,
            transition_from="TODO",
            transition_to="IN_PROGRESS",
            extra={"API_KEY": "super_secret", "nested": {"cookie": "my_cookie", "safe": "value"}}
        )
    )
    store.append(rec)
    loaded = store.read("evt_sec")
    assert loaded.metadata.extra["API_KEY"] == "***MASKED***"
    assert loaded.metadata.extra["nested"]["cookie"] == "***MASKED***"
    assert loaded.metadata.extra["nested"]["safe"] == "value"

def test_store_integrity(store, dummy_record):
    store.append(dummy_record)

    # Tamper with the file
    path = os.path.join(store.root_dir, "evt_123.json")
    with open(path, 'r', encoding='utf-8') as f:
        data = json.load(f)

    data["baseline_commit"] = "hacked"
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(data, f)

    with pytest.raises(EvidenceIntegrityError, match="integrity compromised"):
        store.read("evt_123")

def test_gate_reject_fake_session(store, dummy_record, tmp_path):
    gate = EvidenceGate(store, str(tmp_path))

    fake_rec = EvidenceRecord(
        evidence_id="evt_fake",
        evidence_type=EvidenceType.TASK_START,
        baseline_commit="abc",
        result_commit="def",
        artifacts=[],
        metadata=EvidenceMetadata(
            actor_role="DEV",
            host_id="fake_host",
            host_session_id="fake-session:123",
            host_invocation_id="inv_xyz",
            is_real_host=False,
            transition_from="TODO",
            transition_to="IN_PROGRESS"
        )
    )
    store.append(fake_rec)
    with pytest.raises(EvidenceGateError, match="is_real_host is False"):
        gate.validate_evidence("evt_fake")

    fake_rec2 = EvidenceRecord(
        evidence_id="evt_fake2",
        evidence_type=EvidenceType.TASK_START,
        baseline_commit="abc",
        result_commit="def",
        artifacts=[],
        metadata=EvidenceMetadata(
            actor_role="DEV",
            host_id="fake_host",
            host_session_id="fake-session:123",
            host_invocation_id="inv_xyz",
            is_real_host=True, # Maliciously claiming true
            transition_from="TODO",
            transition_to="IN_PROGRESS"
        )
    )
    store.append(fake_rec2)
    with pytest.raises(EvidenceGateError, match="Invalid or fake host_session_id"):
        gate.validate_evidence("evt_fake2")

def test_gate_validate_artifact(store, dummy_record, tmp_path):
    art_path = tmp_path / "test_file.txt"
    content = b"artifact content"
    art_path.write_bytes(content)
    file_hash = hashlib.sha256(content).hexdigest()

    rec = EvidenceRecord(
        evidence_id="evt_art",
        evidence_type=EvidenceType.TASK_COMPLETE,
        baseline_commit="abc",
        result_commit="def",
        artifacts=[ArtifactRecord(relative_path="test_file.txt", sha256_hash=file_hash)],
        metadata=dummy_record.metadata
    )
    store.append(rec)

    gate = EvidenceGate(store, str(tmp_path))
    assert gate.validate_evidence("evt_art") is True

def test_gate_reject_tampered_artifact(store, dummy_record, tmp_path):
    art_path = tmp_path / "test_file2.txt"
    content = b"artifact content"
    art_path.write_bytes(content)
    file_hash = hashlib.sha256(content).hexdigest()

    rec = EvidenceRecord(
        evidence_id="evt_art2",
        evidence_type=EvidenceType.TASK_COMPLETE,
        baseline_commit="abc",
        result_commit="def",
        artifacts=[ArtifactRecord(relative_path="test_file2.txt", sha256_hash=file_hash)],
        metadata=dummy_record.metadata
    )
    store.append(rec)

    # Tamper with the artifact file
    art_path.write_bytes(b"tampered content")

    gate = EvidenceGate(store, str(tmp_path))
    with pytest.raises(EvidenceGateError, match="Artifact hash mismatch"):
        gate.validate_evidence("evt_art2")
