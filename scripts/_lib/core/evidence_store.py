import os
import json
import hashlib
import uuid
import math
import re
from typing import Dict, Any, List, Mapping
from dataclasses import is_dataclass
from enum import Enum
import threading

from .evidence_schema import (
    EvidenceRecord, EvidenceMetadata, ArtifactRecord,
    EvidenceType, EvidenceError, EvidenceSecurityError, EvidenceIntegrityError
)

def _to_dict(obj):
    if is_dataclass(obj):
        return {k: _to_dict(getattr(obj, k)) for k in obj.__annotations__ if hasattr(obj, k)}
    elif isinstance(obj, Enum):
        return obj.value
    elif isinstance(obj, Mapping):
        return {k: _to_dict(v) for k, v in obj.items()}
    elif isinstance(obj, (list, tuple, set, frozenset)):
        return [_to_dict(v) for v in obj]
    elif isinstance(obj, float):
        if math.isnan(obj) or math.isinf(obj):
            raise ValueError("NaN/Infinity not allowed")
        return obj
    return obj

def canonical_json(data: dict) -> bytes:
    clean_data = _to_dict(data)
    return json.dumps(clean_data, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode('utf-8')

class EvidenceStore:
    def __init__(self, root_dir: str):
        self.root_dir = os.path.realpath(os.path.abspath(root_dir))
        os.makedirs(self.root_dir, exist_ok=True)
        self._lock = threading.Lock()

    def _safe_path(self, evidence_id: str) -> str:
        if not evidence_id or not re.match(r'^[\w\-]{1,64}$', evidence_id):
            raise EvidenceSecurityError("Invalid evidence_id format.")
        
        path = os.path.join(self.root_dir, f"{evidence_id}.json")
        real_path = os.path.realpath(path)
        real_root = self.root_dir
        
        if os.path.commonpath([real_root, real_path]) != real_root:
            raise EvidenceSecurityError("Path traversal detected.")
        if real_path == real_root:
            raise EvidenceSecurityError("Path collision.")
            
        return real_path

    def _mask_text(self, text: str) -> str:
        # Mask common credential patterns in strings
        v_lower = text.lower()
        if any(x in v_lower for x in ['authorization:', 'bearer ', 'cookie:', 'api_key', 'api-key', 'secret=', 'password=', 'private key']):
            return "***MASKED***"
        if any(x in v_lower for x in ['aws_access_key_id', 'aws_secret_access_key', 'openai_api_key']):
            return "***MASKED***"
        if re.search(r'(mongodb|mysql|postgres|redis|amqp|postgresql)://', v_lower):
            return "***MASKED***"
        return text

    def _mask_secrets(self, data: Any) -> Any:
        if isinstance(data, (dict, Mapping)):
            masked = {}
            for k, v in data.items():
                k_lower = str(k).lower()
                if 'invocation_token' in k_lower:
                    raise EvidenceSecurityError("invocation_token must be rejected, not masked")
                if any(x in k_lower for x in ['access_token', 'refresh_token', 'api_key', 'api-key', 'cookie', 'authorization', 'password', 'secret', 'private_key', 'private-key', 'credentials', 'aws_access_key_id', 'aws_secret_access_key', 'openai_api_key']):
                    masked[k] = "***MASKED***"
                else:
                    masked[k] = self._mask_secrets(v)
            return masked
        elif isinstance(data, (list, tuple, set, frozenset)):
            return [self._mask_secrets(item) for item in data]
        elif isinstance(data, str):
            return self._mask_text(data)
        return data

    def append(self, record: EvidenceRecord) -> str:
        path = self._safe_path(record.evidence_id)
        
        # Fast fail without lock
        if os.path.exists(path):
            raise EvidenceSecurityError(f"Evidence {record.evidence_id} already exists. Overwriting is forbidden.")

        data = _to_dict(record)
        data.pop("content_hash", None)
        
        if "metadata" in data and "extra" in data["metadata"]:
            data["metadata"]["extra"] = self._mask_secrets(data["metadata"]["extra"])

        raw_bytes = canonical_json(data)
        content_hash = hashlib.sha256(raw_bytes).hexdigest()
        
        data["content_hash"] = content_hash
        final_bytes = canonical_json(data)

        tmp_path = path + "." + str(uuid.uuid4())
        try:
            with open(tmp_path, 'wb') as f:
                f.write(final_bytes)
                f.flush()
                os.fsync(f.fileno())
            
            # FileExistsError prevents overwrite atomically at OS level
            if os.name == 'nt':
                os.rename(tmp_path, path)
            else:
                os.link(tmp_path, path)
                os.unlink(tmp_path)
        except (FileExistsError, OSError) as e:
            if os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass
            raise EvidenceSecurityError(f"Failed to atomically write evidence or file already exists: {str(e)}")

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

        try:
            metadata = EvidenceMetadata(**data["metadata"])
            artifacts = tuple(ArtifactRecord(**a) for a in data["artifacts"])
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
