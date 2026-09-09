import os
import hashlib
import json
from typing import Optional, Dict, Any, Mapping
from dataclasses import dataclass

from .evidence_schema import (
    EvidenceRecord, EvidenceType, EvidenceGateError
)
from .evidence_store import EvidenceStore
from .agent_schema import HostCapabilities, AgentHandle, AgentResult, ConfirmationResult


def _plain_evidence_value(value: Any) -> Any:
    """Convert frozen Evidence payloads into deterministic JSON-compatible values."""
    if isinstance(value, Mapping):
        return {str(key): _plain_evidence_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain_evidence_value(item) for item in value]
    return value

@dataclass(frozen=True)
class EvidenceValidationContext:
    project_id: str
    task_id: str
    actor_role: str
    transition_from: str
    transition_to: str
    baseline_commit: str
    result_commit: str
    expected_invocation_id: str
    expected_adapter: str
    expected_workspace_mode: str
    expected_evidence_type: EvidenceType
    host_handle: AgentHandle
    expected_capabilities: HostCapabilities
    agent_result: Optional[AgentResult] = None
    confirmation_result: Optional[ConfirmationResult] = None
    expected_metadata: Optional[Mapping[str, Any]] = None
    require_qa_semantics: bool = False

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
        if _plain_evidence_value(actual) != _plain_evidence_value(expected):
            raise EvidenceGateError(f"Evidence rejected: Context mismatch for {field_name}. Expected {expected}, got {actual}")

    def validate_evidence(self, evidence_id: str, ctx: EvidenceValidationContext) -> bool:
        record = self.store.read(evidence_id)
        meta = record.metadata

        # 1. Check Handle constraints
        if ctx.host_handle.is_real_host is False:
            raise EvidenceGateError("Evidence rejected: Context handle is not a real host.")

        # 2. Non-empty critical fields (self-checks for safety, real check is against context)
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

        # 4. Cross validate explicit expected context (1:1 Exact Matches)
        self._check_match("evidence_type", record.evidence_type, ctx.expected_evidence_type)
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
        self._check_match("host_invocation_id", meta.host_invocation_id, ctx.expected_invocation_id)
        self._check_match("adapter", meta.adapter, ctx.expected_adapter)
        self._check_match("workspace_mode", meta.workspace_mode, ctx.expected_workspace_mode)

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

        if ctx.expected_metadata:
            for key, expected in ctx.expected_metadata.items():
                self._check_match(f"metadata_{key}", meta.extra.get(key), expected)

        # QA Evidence must prove semantic coverage, not merely a successful host call.
        if ctx.actor_role == "QA" and ctx.require_qa_semantics:
            required_qa_fields = (
                "qa_decision",
                "qa_request_id",
                "qa_report_hash",
                "acceptance_criteria_hash",
                "covered_criterion_ids",
                "required_test_command_count",
                "negative_scenario_count",
                "uncovered_risk_count",
                "defect_count",
                "test_command_hash",
                "test_output_hash",
                "test_exit_codes",
                "qa_report",
                "required_test_commands",
                "runner_test_results",
            )
            for field_name in required_qa_fields:
                if field_name not in meta.extra:
                    raise EvidenceGateError(f"Evidence rejected: QA semantic field '{field_name}' is missing.")
            for hash_field in (
                "qa_report_hash",
                "acceptance_criteria_hash",
                "test_command_hash",
                "test_output_hash",
            ):
                value = str(meta.extra.get(hash_field, ""))
                if len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
                    raise EvidenceGateError(f"Evidence rejected: QA semantic hash '{hash_field}' is invalid.")

            decision = meta.extra.get("qa_decision")
            exit_codes = tuple(meta.extra.get("test_exit_codes") or ())
            qa_report = meta.extra.get("qa_report")
            required_commands = meta.extra.get("required_test_commands")
            runner_results = meta.extra.get("runner_test_results")
            if not isinstance(qa_report, Mapping):
                raise EvidenceGateError("Evidence rejected: QA report payload is not an object.")
            if not isinstance(required_commands, (list, tuple)) or not required_commands:
                raise EvidenceGateError("Evidence rejected: Required QA command payload is empty or invalid.")
            if not isinstance(runner_results, (list, tuple)) or not runner_results:
                raise EvidenceGateError("Evidence rejected: Runner QA result payload is empty or invalid.")

            recomputed_report_hash = hashlib.sha256(
                json.dumps(_plain_evidence_value(qa_report), ensure_ascii=False, sort_keys=True).encode("utf-8")
            ).hexdigest()
            recomputed_command_hash = hashlib.sha256(
                json.dumps(_plain_evidence_value(required_commands), ensure_ascii=False, sort_keys=True).encode("utf-8")
            ).hexdigest()
            recomputed_output_hash = hashlib.sha256(
                json.dumps(_plain_evidence_value(runner_results), ensure_ascii=False, sort_keys=True).encode("utf-8")
            ).hexdigest()
            self._check_match("qa_report_hash_recomputed", meta.extra.get("qa_report_hash"), recomputed_report_hash)
            self._check_match("test_command_hash_recomputed", meta.extra.get("test_command_hash"), recomputed_command_hash)
            self._check_match("test_output_hash_recomputed", meta.extra.get("test_output_hash"), recomputed_output_hash)
            self._check_match("qa_report_decision", qa_report.get("decision"), decision)
            self._check_match("qa_report_task_id", qa_report.get("task_id"), ctx.task_id)
            self._check_match("qa_report_baseline_commit", qa_report.get("baseline_commit"), ctx.baseline_commit)
            self._check_match("qa_report_candidate_commit", qa_report.get("candidate_commit"), ctx.result_commit)
            self._check_match("qa_report_session_id", qa_report.get("session_id"), ctx.host_handle.session_id)
            self._check_match("qa_report_request_id", qa_report.get("qa_request_id"), meta.extra.get("qa_request_id"))
            self._check_match(
                "qa_report_acceptance_criteria_hash",
                qa_report.get("acceptance_criteria_hash"),
                meta.extra.get("acceptance_criteria_hash"),
            )

            report_coverage = tuple(
                item.get("criterion_id")
                for item in qa_report.get("acceptance_coverage", ())
                if isinstance(item, Mapping) and item.get("status") == "PASS"
            )
            report_negative = tuple(qa_report.get("negative_scenarios") or ())
            report_risks = tuple(qa_report.get("uncovered_risks") or ())
            report_defects = tuple(qa_report.get("defects") or ())
            result_exit_codes = tuple(
                item.get("exit_code")
                for item in runner_results
                if isinstance(item, Mapping)
            )
            result_commands = tuple(
                item.get("command")
                for item in runner_results
                if isinstance(item, Mapping)
            )
            report_commands = tuple(
                item.get("command")
                for item in qa_report.get("test_commands", ())
                if isinstance(item, Mapping)
            )
            if len(result_commands) != len(runner_results):
                raise EvidenceGateError("Evidence rejected: Runner QA results contain a non-object item.")
            for item in runner_results:
                if not isinstance(item.get("exit_code"), int) or isinstance(item.get("exit_code"), bool):
                    raise EvidenceGateError("Evidence rejected: Runner QA exit code is invalid.")
                output_hash = str(item.get("output_hash", ""))
                if len(output_hash) != 64 or any(char not in "0123456789abcdef" for char in output_hash):
                    raise EvidenceGateError("Evidence rejected: Runner QA output hash is invalid.")
            self._check_match("runner_test_commands_recomputed", result_commands, tuple(required_commands))
            self._check_match("covered_criterion_ids_recomputed", tuple(meta.extra.get("covered_criterion_ids") or ()), report_coverage)
            self._check_match("required_test_command_count_recomputed", meta.extra.get("required_test_command_count"), len(required_commands))
            self._check_match("negative_scenario_count_recomputed", meta.extra.get("negative_scenario_count"), len(report_negative))
            self._check_match("uncovered_risk_count_recomputed", meta.extra.get("uncovered_risk_count"), len(report_risks))
            expected_defect_count = len(report_defects) + (0 if result_exit_codes and all(code == 0 for code in result_exit_codes) else 1)
            self._check_match("defect_count_recomputed", meta.extra.get("defect_count"), expected_defect_count)
            self._check_match("test_exit_codes_recomputed", exit_codes, result_exit_codes)
            if decision == "PASS":
                if len(report_commands) != len(set(report_commands)) or set(report_commands) != set(required_commands):
                    raise EvidenceGateError("Evidence rejected: QA PASS report commands do not match required commands exactly once.")
                if ctx.transition_to != "PENDING_USER_ACCEPTANCE":
                    raise EvidenceGateError("Evidence rejected: QA PASS has an invalid transition target.")
                if not meta.extra.get("covered_criterion_ids"):
                    raise EvidenceGateError("Evidence rejected: QA PASS has no acceptance coverage.")
                if int(meta.extra.get("required_test_command_count", 0)) < 1:
                    raise EvidenceGateError("Evidence rejected: QA PASS has no required command evidence.")
                if int(meta.extra.get("negative_scenario_count", 0)) < 1:
                    raise EvidenceGateError("Evidence rejected: QA PASS has no negative scenario evidence.")
                if int(meta.extra.get("uncovered_risk_count", 0)) != 0:
                    raise EvidenceGateError("Evidence rejected: QA PASS contains uncovered risks.")
                if int(meta.extra.get("defect_count", 0)) != 0:
                    raise EvidenceGateError("Evidence rejected: QA PASS contains defects.")
                if not exit_codes or any(code != 0 for code in exit_codes):
                    raise EvidenceGateError("Evidence rejected: QA PASS contains a failing or missing test command.")
            elif decision == "FAIL":
                if ctx.transition_to != "BUILDING":
                    raise EvidenceGateError("Evidence rejected: QA FAIL has an invalid transition target.")
                if int(meta.extra.get("defect_count", 0)) < 1:
                    raise EvidenceGateError("Evidence rejected: QA FAIL must contain a structured defect.")
            else:
                raise EvidenceGateError(f"Evidence rejected: Unknown QA decision '{decision}'.")

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
