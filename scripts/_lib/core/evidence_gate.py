import os
import hashlib
from typing import Optional, Dict, Any
from dataclasses import dataclass

from .evidence_schema import (
    EvidenceRecord, EvidenceType, EvidenceGateError
)
from .evidence_store import EvidenceStore
from .agent_schema import HostCapabilities, AgentHandle, AgentResult, ConfirmationResult

@dataclass(frozen=True)
class EvidenceValidationContext:
    project_id: str
    task_id: str
    actor_role: str
    transition_from: str
    transition_to: str
    baseline_commit: str
    result_commit: str
    host_handle: AgentHandle
    expected_capabilities: HostCapabilities
    agent_result: Optional[AgentResult] = None
    confirmation_result: Optional[ConfirmationResult] = None

class EvidenceGate:
    def __init__(self, store: EvidenceStore, project_root: str):
        self.store = store
        self.project_root = os.path.realpath(os.path.abspath(project_root))

    def _contains_fake(self, val: str) -> bool:
        if not val:
            return False
        lower = val.lower()
        for word in ['fake', 'test', 'mock', 'simulate', 'model', 'assistant', 'system']:
            if word in lower:
                return True
        return False

    def _check_match(self, field_name: str, actual: Any, expected: Any):
        if actual is None or expected is None:
            raise EvidenceGateError(f"Evidence rejected: {field_name} is missing in context or evidence (None is not allowed).")
        if actual != expected:
            raise EvidenceGateError(f"Evidence rejected: Context mismatch for {field_name}. Expected {expected}, got {actual}")

    def validate_evidence(self, evidence_id: str, ctx: EvidenceValidationContext) -> bool:
        record = self.store.read(evidence_id)
        meta = record.metadata

        # 1. Check Handle constraints
        if ctx.host_handle.is_real_host is False:
            raise EvidenceGateError("Evidence rejected: Context handle is not a real host.")

        # 2. Non-empty critical fields
        for f in [meta.host_id, meta.adapter, meta.host_session_id, meta.host_invocation_id, meta.workspace_mode]:
            if not f or str(f).strip() == "":
                raise EvidenceGateError("Evidence rejected: Critical identifier field is empty.")

        # 3. Reject fake identifiers in evidence
        if not meta.is_real_host:
            raise EvidenceGateError("Evidence rejected: Evidence claims is_real_host is False.")

        if (self._contains_fake(meta.host_id) or self._contains_fake(meta.adapter) or
            self._contains_fake(meta.host_session_id) or self._contains_fake(meta.host_invocation_id)):
            raise EvidenceGateError("Evidence rejected: Contains fake/test/mock/simulate identifier.")

        if not record.baseline_commit or not record.result_commit:
            raise EvidenceGateError("Evidence rejected: Missing commits.")

        # 4. Cross validate explicit expected context
        self._check_match("project_id", meta.project_id, ctx.project_id)
        self._check_match("task_id", meta.task_id, ctx.task_id)
        self._check_match("actor_role", meta.actor_role, ctx.actor_role)
        self._check_match("transition_from", meta.transition_from, ctx.transition_from)
        self._check_match("transition_to", meta.transition_to, ctx.transition_to)
        self._check_match("baseline_commit", record.baseline_commit, ctx.baseline_commit)
        self._check_match("result_commit", record.result_commit, ctx.result_commit)

        # Handle details matching
        self._check_match("host_id", meta.host_id, ctx.host_handle.host_id)
        self._check_match("host_session_id", meta.host_session_id, ctx.host_handle.session_id)
        if hasattr(ctx.host_handle, "invocation_id") and ctx.host_handle.invocation_id is not None:
             self._check_match("host_invocation_id", meta.host_invocation_id, ctx.host_handle.invocation_id)

        # Verify Result context if present
        if ctx.agent_result:
            if not ctx.agent_result.is_real_host:
                 raise EvidenceGateError("Evidence rejected: AgentResult claims is_real_host is False.")
            self._check_match("result_session_id", meta.host_session_id, ctx.agent_result.session_id)

        # Capabilities matching
        if not ctx.expected_capabilities:
            raise EvidenceGateError("Evidence rejected: expected_capabilities is missing in validation context.")
        for k, v in ctx.expected_capabilities.__dict__.items():
            if k == "extra": continue
            self._check_match(f"capability_{k}", meta.extra.get(f"capability_{k}"), v)

        # 5. Artifact Validation
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

        # 6. User Confirmation Validation
        if record.evidence_type == EvidenceType.USER_CONFIRMATION:
            if not ctx.confirmation_result:
                raise EvidenceGateError("Evidence rejected: USER_CONFIRMATION requires explicit confirmation_result in context.")
            if not ctx.confirmation_result.is_real_host:
                raise EvidenceGateError("Evidence rejected: ConfirmationResult claims is_real_host is False.")

            self._check_match("confirmation_id", meta.extra.get("confirmation_id"), ctx.confirmation_result.request_id)

            user_source = str(meta.extra.get("user_source", "")).strip().lower()
            if not user_source or user_source not in ("explicit_user",):
                 raise EvidenceGateError(f"Evidence rejected: USER_CONFIRMATION user_source '{user_source}' is not whitelisted.")

            if not meta.extra.get("confirmed_at"):
                 raise EvidenceGateError("Evidence rejected: Missing confirmed_at.")

        return True
