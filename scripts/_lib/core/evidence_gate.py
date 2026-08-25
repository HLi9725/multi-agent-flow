import os
import hashlib
from typing import Optional

from .evidence_schema import (
    EvidenceRecord, EvidenceType, EvidenceGateError
)
from .evidence_store import EvidenceStore

class EvidenceGate:
    def __init__(self, store: EvidenceStore, project_root: str):
        self.store = store
        self.project_root = os.path.abspath(project_root)

    def validate_evidence(self, evidence_id: str) -> bool:
        """
        Validates evidence strictly against the Phase 2B constraints.
        Raises EvidenceGateError if validation fails, otherwise returns True.
        """
        record = self.store.read(evidence_id)
        meta = record.metadata

        # 1. Reject fake or non-real host
        if not meta.is_real_host:
            raise EvidenceGateError("Evidence rejected: is_real_host is False.")

        if not meta.host_session_id or meta.host_session_id.startswith("fake-session:"):
            raise EvidenceGateError(f"Evidence rejected: Invalid or fake host_session_id '{meta.host_session_id}'.")

        if not meta.host_invocation_id:
            raise EvidenceGateError("Evidence rejected: Missing host_invocation_id.")

        # 2. Check baseline/result commit presence
        if not record.baseline_commit:
            raise EvidenceGateError("Evidence rejected: Missing baseline_commit.")
        if not record.result_commit:
            raise EvidenceGateError("Evidence rejected: Missing result_commit.")

        # 3. Validate artifacts
        for art in record.artifacts:
            art_path = os.path.abspath(os.path.join(self.project_root, art.relative_path))

            # Path traversal check
            if not art_path.startswith(self.project_root):
                raise EvidenceGateError(f"Evidence rejected: Artifact path traversal detected '{art.relative_path}'.")

            if not os.path.exists(art_path):
                raise EvidenceGateError(f"Evidence rejected: Artifact does not exist '{art.relative_path}'.")

            # Verify SHA-256
            try:
                with open(art_path, 'rb') as f:
                    file_bytes = f.read()
                actual_hash = hashlib.sha256(file_bytes).hexdigest()
                if actual_hash != art.sha256_hash:
                    raise EvidenceGateError(f"Evidence rejected: Artifact hash mismatch for '{art.relative_path}'. Expected {art.sha256_hash}, got {actual_hash}.")
            except IOError as e:
                raise EvidenceGateError(f"Evidence rejected: Failed to read artifact '{art.relative_path}': {str(e)}")

        # 4. Interactive Confirmation check
        if record.evidence_type == EvidenceType.USER_CONFIRMATION:
            confirmation_id = meta.extra.get("confirmation_id")
            if not confirmation_id or str(confirmation_id).startswith("fake"):
                raise EvidenceGateError("Evidence rejected: Fake or missing user confirmation ID.")

        return True
