import os
import json
import hashlib
from typing import Dict, Any, List
from dataclasses import asdict

from .evidence_schema import (
    EvidenceRecord, EvidenceMetadata, ArtifactRecord,
    EvidenceType, EvidenceError, EvidenceSecurityError, EvidenceIntegrityError
)

def canonical_json(data: dict) -> bytes:
    return json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode('utf-8')

class EvidenceStore:
    def __init__(self, root_dir: str):
        self.root_dir = os.path.abspath(root_dir)
        os.makedirs(self.root_dir, exist_ok=True)

    def _safe_path(self, evidence_id: str) -> str:
        if not evidence_id or '/' in evidence_id or '\\' in evidence_id or '..' in evidence_id:
            raise EvidenceSecurityError("Invalid evidence_id format or path traversal attempt.")
        path = os.path.abspath(os.path.join(self.root_dir, f"{evidence_id}.json"))
        if not path.startswith(self.root_dir):
            raise EvidenceSecurityError("Path traversal detected.")
        return path

    def _mask_secrets(self, data: Any) -> Any:
        if isinstance(data, dict):
            masked = {}
            for k, v in data.items():
                k_lower = k.lower()
                if 'token' in k_lower or 'api_key' in k_lower or 'cookie' in k_lower or 'secret' in k_lower or 'password' in k_lower:
                    masked[k] = "***MASKED***"
                else:
                    masked[k] = self._mask_secrets(v)
            return masked
        elif isinstance(data, list):
            return [self._mask_secrets(item) for item in data]
        return data

    def append(self, record: EvidenceRecord) -> str:
        path = self._safe_path(record.evidence_id)
        if os.path.exists(path):
            raise EvidenceSecurityError(f"Evidence {record.evidence_id} already exists. Overwriting is forbidden.")

        data = asdict(record)
        # Remove original content_hash before hashing
        data.pop("content_hash", None)

        # Mask secrets in metadata.extra
        if "metadata" in data and "extra" in data["metadata"]:
            data["metadata"]["extra"] = self._mask_secrets(data["metadata"]["extra"])

        # Ensure strict forbidden fields are completely absent or masked
        if "invocation_token" in data.get("metadata", {}).get("extra", {}):
             data["metadata"]["extra"]["invocation_token"] = "***MASKED***"

        raw_bytes = canonical_json(data)
        content_hash = hashlib.sha256(raw_bytes).hexdigest()

        data["content_hash"] = content_hash
        final_bytes = canonical_json(data)

        tmp_path = path + ".tmp"
        try:
            with open(tmp_path, 'wb') as f:
                f.write(final_bytes)
                f.flush()
                os.fsync(f.fileno())
            # Atomic rename
            os.replace(tmp_path, path)
        except Exception as e:
            if os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass
            raise EvidenceError(f"Failed to atomically write evidence: {str(e)}")

        return content_hash

    def read(self, evidence_id: str) -> EvidenceRecord:
        path = self._safe_path(evidence_id)
        if not os.path.exists(path):
            raise EvidenceError(f"Evidence {evidence_id} not found.")

        with open(path, 'rb') as f:
            raw_bytes = f.read()

        try:
            data = json.loads(raw_bytes.decode('utf-8'))
        except json.JSONDecodeError:
            raise EvidenceIntegrityError("Evidence file is corrupted or not valid JSON.")

        stored_hash = data.pop("content_hash", None)
        computed_hash = hashlib.sha256(canonical_json(data)).hexdigest()
        if stored_hash != computed_hash:
            raise EvidenceIntegrityError(f"Evidence integrity compromised! Expected {stored_hash}, got {computed_hash}")

        # Reconstruct
        try:
            metadata = EvidenceMetadata(**data["metadata"])
            artifacts = [ArtifactRecord(**a) for a in data["artifacts"]]
            record = EvidenceRecord(
                evidence_id=data["evidence_id"],
                evidence_type=EvidenceType(data["evidence_type"]),
                baseline_commit=data["baseline_commit"],
                result_commit=data["result_commit"],
                artifacts=artifacts,
                metadata=metadata,
                content_hash=stored_hash
            )
            return record
        except Exception as e:
            raise EvidenceIntegrityError(f"Evidence schema reconstruction failed: {str(e)}")
