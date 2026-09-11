"""Evidence replay for Runner-managed tasks; standalone workflows remain compatible."""
import glob
import json
import os
from dataclasses import fields

from .agent_schema import AgentHandle, HostCapabilities, ConfirmationResult
from .evidence_gate import EvidenceGate, EvidenceValidationContext
from .evidence_schema import EvidenceType, EvidenceGateError
from .evidence_store import EvidenceStore
from .runner_schema import RunnerCheckpoint


def replay_record(store, record, checkpoint, role, source, target, *, commands=None):
    meta = record.metadata
    snapshot = checkpoint.execution_spec_snapshot
    caps = {}
    for field in fields(HostCapabilities):
        if field.name != 'extra':
            key = 'capability_' + field.name
            if key not in meta.extra:
                raise EvidenceGateError('Missing recorded capability: ' + key)
            caps[field.name] = meta.extra[key]
    expected = None
    if role == 'QA':
        expected = {'acceptance_criteria_hash': snapshot.get('acceptance_criteria_hash')}
        if commands is not None:
            expected['required_test_commands'] = tuple(commands) + (
                f"git diff --check {snapshot.get('baseline_commit')}..{checkpoint.candidate_commit} --",)
    confirmation = None
    if role == 'PM':
        confirmation = ConfirmationResult(checkpoint.confirmation_request_id, 'accept', True, True)
    gate = EvidenceGate(store, checkpoint.worktree_path or snapshot['project_root'])
    return gate.validate_evidence(record.evidence_id, EvidenceValidationContext(
        project_id=checkpoint.project_id, task_id=checkpoint.task_id,
        actor_role=role, transition_from=source, transition_to=target,
        baseline_commit=snapshot.get('baseline_commit'), result_commit=checkpoint.candidate_commit,
        expected_invocation_id=meta.host_invocation_id, expected_adapter=meta.adapter,
        expected_workspace_mode='user_confirmation' if role == 'PM' else ('workspace_write' if role == 'BUILDER' else 'workspace_read'),
        expected_evidence_type=EvidenceType.USER_CONFIRMATION if role == 'PM' else EvidenceType.TASK_TRANSITION,
        host_handle=AgentHandle(meta.host_session_id, meta.host_id, is_real_host=True),
        expected_capabilities=HostCapabilities(**caps), expected_metadata=expected,
        require_qa_semantics=role == 'QA', confirmation_result=confirmation,
    ))


def validate_managed_transition(data_root, task_id, source, target, evidence_id=None):
    protected = {
        ('进行中', '审查中'): ('BUILDER', 'BUILDING', 'REVIEWING'),
        ('审查中', '测试中'): ('REVIEWER', 'REVIEWING', 'TESTING'),
        ('测试中', '已完成'): ('QA', 'TESTING', 'PENDING_USER_ACCEPTANCE'),
        ('已完成', '已验收'): ('PM', 'PENDING_USER_ACCEPTANCE', 'ACCEPTED'),
    }
    if (source, target) not in protected:
        return
    matches = glob.glob(os.path.join(data_root, 'runner_checkpoints', '*', task_id + '.json'))
    if not matches:
        return  # Existing non-Runner single-role workflows keep their contract.
    if len(matches) != 1:
        raise EvidenceGateError('Ambiguous Runner checkpoint; select the correct project data root.')
    with open(matches[0], encoding='utf-8') as handle:
        checkpoint = RunnerCheckpoint.from_dict(json.load(handle))
    if checkpoint.state in ('CANCELLED', 'APPROVAL_REQUIRED', 'FAILED', 'NEEDS_USER_INPUT'):
        raise EvidenceGateError('Runner is paused or cancelled; resume through Runner before advancing.')
    if not evidence_id or evidence_id not in checkpoint.evidence_ids:
        raise EvidenceGateError('Runner-managed task requires its persisted transition evidence; use run_task.py.')
    root = checkpoint.execution_spec_snapshot.get('project_root')
    if not root:
        raise EvidenceGateError('Legacy checkpoint needs Runner resume/migration before transition.')
    store = EvidenceStore(os.path.join(root, 'user_data', 'runner_evidence'))
    role, before, after = protected[(source, target)]
    replay_record(store, store.read(evidence_id), checkpoint, role, before, after,
                  commands=checkpoint.execution_options.get('test_commands') or ('python -m pytest -q',))
