import json
import threading
from dataclasses import replace

import pytest

from scripts._lib.core.agent_schema import AgentPermissionRequiredError
from scripts._lib.core.runner_checkpoint_store import RunnerCheckpointStore, CheckpointStoreError
from scripts._lib.core.runner_schema import RunnerCheckpoint, RunnerState, TaskExecutionSpec
from scripts._lib.core.production_runner import ProductionRunner
from scripts._lib.core.runner_transition_gate import validate_managed_transition
import scripts._lib.core.production_runner as runner_module


def make_store(tmp_path):
    return RunnerCheckpointStore(data_root=str(tmp_path / 'data'), project_root=str(tmp_path), project_id='demo')


def make_checkpoint(**kwargs):
    return RunnerCheckpoint(task_id='T0770', project_id='demo', state='BUILDING',
                            current_role='BUILDER', **kwargs)


def test_cancel_and_stale_writer_are_serialized(tmp_path, monkeypatch):
    store = make_store(tmp_path)
    initial = make_checkpoint(run_id='first')
    store.save_checkpoint(initial)
    entered = threading.Event()
    release = threading.Event()
    errors = []
    original = store._save_locked

    def intercepted(checkpoint, **kwargs):
        if checkpoint.state == 'CANCELLED':
            entered.set()
            assert release.wait(5)
        return original(checkpoint, **kwargs)

    monkeypatch.setattr(store, '_save_locked', intercepted)
    def write(checkpoint):
        try:
            store.save_checkpoint(checkpoint)
        except CheckpointStoreError as exc:
            errors.append(str(exc))
    cancel = threading.Thread(target=write, args=(replace(initial, state='CANCELLED'),))
    cancel.start()
    assert entered.wait(5)
    late = threading.Thread(target=write, args=(initial,))
    late.start()
    release.set()
    cancel.join(5)
    late.join(5)
    assert not cancel.is_alive() and not late.is_alive()
    assert store.load_checkpoint('T0770').state == 'CANCELLED'
    assert len(errors) == 1 and 'durable cancellation' in errors[0]


def test_restart_archives_cancelled_run_and_rejects_old_results(tmp_path):
    store = make_store(tmp_path)
    old = replace(make_checkpoint(run_id='old'), state='CANCELLED')
    store.save_checkpoint(old)
    new = make_checkpoint(run_id='new')
    with pytest.raises(CheckpointStoreError, match='already exists'):
        store.initialize_checkpoint(new)
    store.initialize_checkpoint(new, restart_cancelled=True)
    assert store.load_checkpoint('T0770').run_id == 'new'
    assert list((tmp_path / 'data' / 'runner_checkpoints').rglob('*.archive_*'))
    with pytest.raises(CheckpointStoreError, match='Stale run'):
        store.save_checkpoint(old)


def test_resume_does_not_read_or_migrate_before_acquiring_lock(tmp_path, monkeypatch):
    store = make_store(tmp_path)
    store.save_checkpoint(make_checkpoint())
    lock = store.acquire_runner_lock('T0770')
    monkeypatch.setattr(store, 'load_checkpoint', lambda *a: pytest.fail('Read before acquiring lock'))
    try:
        result = ProductionRunner(checkpoint_store=store).resume(str(tmp_path), 'T0770')
        assert not result.success and 'locked' in result.message
    finally:
        store.release_runner_lock(lock)


def test_plain_start_preserves_paused_checkpoint(tmp_path, monkeypatch):
    store = make_store(tmp_path)
    checkpoint = replace(make_checkpoint(), state='APPROVAL_REQUIRED', evidence_ids=('evi-old',))
    store.save_checkpoint(checkpoint)
    monkeypatch.setattr(runner_module, 'verify_optimistic_concurrency', lambda *a, **k: True)
    spec = TaskExecutionSpec(project_id='demo', project_root=str(tmp_path), authority_root=str(tmp_path),
                            requirement_text='r', acceptance_criteria='a', acceptance_criteria_hash='a'*64, task_version='1',
                            task_id='T0770', task_name='repair', status_at_read='进行中')
    runner = ProductionRunner(checkpoint_store=store)
    monkeypatch.setattr(runner, '_execute_loop', lambda *a, **k: pytest.fail('Must not restart'))
    result = runner.start(spec)
    assert not result.success and result.evidence_ids == ('evi-old',)
    assert store.load_checkpoint('T0770') == checkpoint


def test_permission_diagnostics_survive_and_do_not_consume_repair_budget(tmp_path):
    store = make_store(tmp_path)
    runner = ProductionRunner(checkpoint_store=store)
    checkpoint = make_checkpoint(total_attempts=17)
    error = AgentPermissionRequiredError('Permission denied', diagnostics={
        'host_session_id': 'conv-real', 'host_invocation_id': 'conv-real:step_2',
        'host_exit_code': 0,
        'denied_actions': [{'action': 'escalate_admin', 'display_name': 'Bash'}],
        'tool_events': [{'tool_name': 'run_command', 'command': 'git status'}],
        'host_message': 'password=private --dangerously-skip-permissions',
    })
    paused = runner._permission_checkpoint(checkpoint, error)
    store.save_checkpoint(paused)
    restored = store.load_checkpoint('T0770')
    assert restored.total_attempts == 16 and restored.approval_attempts == 1
    assert restored.approval_diagnostics['host_exit_code'] == 0
    assert restored.approval_diagnostics['denied_actions'][0]['action'] == 'escalate_admin'
    assert 'git status' in restored.approval_diagnostics['tool_events']
    assert 'private' not in restored.approval_diagnostics['host_message']
    assert '--dangerously-skip-permissions' not in restored.approval_diagnostics['host_message']


@pytest.mark.parametrize('source,target', [('进行中','审查中'),('审查中','测试中'),('测试中','已完成'),('已完成','已验收')])
def test_managed_transition_requires_proof_but_standalone_is_unchanged(tmp_path, source, target):
    store = make_store(tmp_path)
    validate_managed_transition(store.data_root, 'T0770', source, target)
    store.save_checkpoint(make_checkpoint())
    with pytest.raises(Exception, match='persisted transition evidence'):
        validate_managed_transition(store.data_root, 'T0770', source, target)


@pytest.mark.parametrize('state,board', [('COMPLETION_PENDING','测试中'),('REJECTION_PENDING','已完成')])
def test_pending_transition_recovery_finishes_board_write(tmp_path, monkeypatch, state, board):
    store = make_store(tmp_path)
    spec = TaskExecutionSpec(project_id='demo', project_root=str(tmp_path), authority_root=str(tmp_path),
                            requirement_text='r', acceptance_criteria='a', acceptance_criteria_hash='a'*64, task_version='1',
                            task_id='T0770', task_name='repair', status_at_read=board)
    checkpoint = replace(make_checkpoint(), state=state, worktree_path=str(tmp_path),
                         execution_spec_snapshot=runner_module._execution_spec_snapshot(spec))
    store.save_checkpoint(checkpoint)
    monkeypatch.setattr(runner_module, 'load_task_execution_spec', lambda **k: spec)
    monkeypatch.setattr(runner_module, 'verify_optimistic_concurrency', lambda *a, **k: True)
    runner = ProductionRunner(checkpoint_store=store)
    transitions = []
    monkeypatch.setattr(runner, '_do_state_transition', lambda *a, **k: (transitions.append(a) or (True, None)))
    monkeypatch.setattr(runner, '_execute_with_checkpoint_guard', lambda *a, **k: k['existing_checkpoint'])
    result = runner.resume(str(tmp_path), 'T0770')
    assert len(transitions) == 1
    assert result.state == ('PENDING_USER_ACCEPTANCE' if state == 'COMPLETION_PENDING' else 'NEEDS_USER_INPUT')
