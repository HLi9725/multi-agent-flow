import os

from scripts.run_task import _runner_for


def test_explicit_authority_root_owns_checkpoint_store(tmp_path):
    project = tmp_path / "project"
    authority = project / ".yy-flow"
    authority.mkdir(parents=True)

    runner = _runner_for(str(project), str(authority), project_id="project")

    assert runner.checkpoint_store.data_root == os.path.realpath(authority)
    assert runner.checkpoint_store.checkpoint_dir.startswith(os.path.realpath(authority))
