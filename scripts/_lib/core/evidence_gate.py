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

    def _check_match(self, field_name: str, actual: Any, expected: Any):
        if actual is None or expected is None:
            raise EvidenceGateError(f"Evidence rejected: {field_name} is missing in context or evidence.")
        if actual != expected:
            raise EvidenceGateError(f"Evidence rejected: Context mismatch for {field_name}. Expected {expected}, got {actual}")

    def validate_evidence(self, evidence_id: str, expected_context: Dict[str, Any]) -> bool:
        record = self.store.read(evidence_id)
        meta = record.metadata

        # 1. Non-empty critical fields
        for f in [meta.host_id, meta.adapter, meta.host_session_id, meta.host_invocation_id, meta.workspace_mode]:
            if not f:
                raise EvidenceGateError("Evidence rejected: Critical identifier field is empty.")

        # 2. Reject fake
        if not meta.is_real_host:
            raise EvidenceGateError("Evidence rejected: is_real_host is False.")
        
        if (self._contains_fake(meta.host_id) or self._contains_fake(meta.adapter) or 
            self._contains_fake(meta.host_session_id) or self._contains_fake(meta.host_invocation_id)):
            raise EvidenceGateError("Evidence rejected: Contains fake/test/mock/simulate identifier.")

        if not record.baseline_commit or not record.result_commit:
            raise EvidenceGateError("Evidence rejected: Missing commits.")

        # 3. Cross validate context (must exact match, None != None rejected by _check_match)
        self._check_match("host_session_id", meta.host_session_id, expected_context.get("host_session_id"))
        self._check_match("host_invocation_id", meta.host_invocation_id, expected_context.get("host_invocation_id"))
        self._check_match("workspace_mode", meta.workspace_mode, expected_context.get("workspace_mode"))
        self._check_match("task_id", meta.task_id, expected_context.get("task_id"))
        self._check_match("actor_role", meta.actor_role, expected_context.get("actor_role"))
        self._check_match("transition_from", meta.transition_from, expected_context.get("transition_from"))
        self._check_match("transition_to", meta.transition_to, expected_context.get("transition_to"))
        self._check_match("baseline_commit", record.baseline_commit, expected_context.get("baseline_commit"))
        self._check_match("result_commit", record.result_commit, expected_context.get("result_commit"))

        # Capabilities matching (If expected context provides capabilities, match it)
        if "expected_capabilities" in expected_context:
            for k, v in expected_context["expected_capabilities"].items():
                self._check_match(f"capability_{k}", meta.extra.get(f"capability_{k}"), v)
             
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
            self._check_match("confirmation_id", meta.extra.get("confirmation_id"), expected_context.get("confirmation_id"))
            self._check_match("confirmed_at", meta.extra.get("confirmed_at"), expected_context.get("confirmed_at"))
            self._check_match("user_source", meta.extra.get("user_source"), expected_context.get("user_source"))
            user_source = meta.extra.get("user_source", "")
            if not user_source or user_source in ("model", "fake", "simulated", "system"):
                 raise EvidenceGateError("Evidence rejected: USER_CONFIRMATION must come from explicit user source, not model/fake.")
                 
        return True
