import os
import hashlib
from typing import Optional, Dict, Any

from .evidence_schema import (
    EvidenceRecord, EvidenceType, EvidenceGateError
)
from .evidence_store import EvidenceStore

class EvidenceGate:
    def __init__(self, store: EvidenceStore, project_root: str):
        self.store = store
        self.project_root = os.path.realpath(os.path.abspath(project_root))

    def _contains_fake(self, val: str) -> bool:
        if not val:
            return False
        lower = val.lower()
        for word in ['fake', 'test', 'mock', 'simulate']:
            if word in lower:
                return True
        return False

    def validate_evidence(self, evidence_id: str, expected_context: Dict[str, Any]) -> bool:
        record = self.store.read(evidence_id)
        meta = record.metadata

        if not meta.is_real_host:
            raise EvidenceGateError("Evidence rejected: is_real_host is False.")

        if (self._contains_fake(meta.host_id) or self._contains_fake(meta.adapter) or
            self._contains_fake(meta.host_session_id) or self._contains_fake(meta.host_invocation_id)):
            raise EvidenceGateError("Evidence rejected: Contains fake/test/mock/simulate identifier.")

        if not record.baseline_commit or not record.result_commit:
            raise EvidenceGateError("Evidence rejected: Missing commits.")

        # Cross validate context
        for key in ["project_id", "task_id", "actor_role", "host_id", "adapter", "transition_from", "transition_to"]:
            if meta.__dict__.get(key) != expected_context.get(key):
                raise EvidenceGateError(f"Evidence rejected: Context mismatch for {key}.")

        if record.baseline_commit != expected_context.get("baseline_commit"):
             raise EvidenceGateError("Evidence rejected: baseline_commit mismatch.")
        if record.result_commit != expected_context.get("result_commit"):
             raise EvidenceGateError("Evidence rejected: result_commit mismatch.")

        if record.evidence_type == EvidenceType.TASK_COMPLETE:
            if not record.artifacts:
                raise EvidenceGateError("Evidence rejected: TASK_COMPLETE missing artifacts.")

        for art in record.artifacts:
            if os.path.isabs(art.relative_path) or '..' in art.relative_path:
                raise EvidenceGateError(f"Evidence rejected: Invalid relative path '{art.relative_path}'.")

            art_path = os.path.realpath(os.path.join(self.project_root, art.relative_path))
            if os.path.commonpath([self.project_root, art_path]) != self.project_root:
                raise EvidenceGateError(f"Evidence rejected: Artifact path traversal detected '{art.relative_path}'.")

            if not os.path.isfile(art_path):
                raise EvidenceGateError(f"Evidence rejected: Artifact does not exist or is not a regular file '{art.relative_path}'.")

            try:
                with open(art_path, 'rb') as f:
                    file_bytes = f.read()
                actual_hash = hashlib.sha256(file_bytes).hexdigest()
                if actual_hash != art.sha256_hash:
                    raise EvidenceGateError(f"Evidence rejected: Artifact hash mismatch for '{art.relative_path}'.")
            except IOError as e:
                raise EvidenceGateError(f"Evidence rejected: Failed to read artifact '{art.relative_path}': {str(e)}")

        if record.evidence_type == EvidenceType.USER_CONFIRMATION:
            if not meta.extra.get("confirmation_id") or not meta.extra.get("confirmed_at") or not meta.extra.get("user_source"):
                raise EvidenceGateError("Evidence rejected: Missing user confirmation fields.")

        return True
