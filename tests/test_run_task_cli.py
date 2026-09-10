from types import SimpleNamespace

from scripts.run_task import _overrides_from_args


def test_resume_overrides_are_optional_and_complete():
    empty = SimpleNamespace()
    assert _overrides_from_args(empty) == {}

    args = SimpleNamespace(
        builder_adapter="antigravity",
        reviewer_adapter="antigravity",
        qa_adapter="antigravity",
        max_review_cycles=4,
        max_qa_cycles=5,
        timeout_seconds=901,
        test_commands=["python -m pytest tests -q", "npm run build"],
        workspace_mode="branch",
    )
    assert _overrides_from_args(args) == {
        "builder_adapter_id": "antigravity",
        "reviewer_adapter_id": "antigravity",
        "qa_adapter_id": "antigravity",
        "max_review_cycles": 4,
        "max_qa_cycles": 5,
        "builder_timeout_seconds": 901,
        "reviewer_timeout_seconds": 901,
        "qa_timeout_seconds": 901,
        "test_commands": (
            "python -m pytest tests -q",
            "npm run build",
        ),
        "workspace_mode": "branch",
    }
