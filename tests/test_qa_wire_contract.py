"""Replay the observed status/commands shape through the same path as canonical QA."""
import copy
import json

import pytest

from scripts._lib.core.production_runner import (
    ProductionRunner, QA_MODEL_JSON_SCHEMA, _extract_embedded_json_object,
    _qa_semantic_fingerprint,
)
from scripts._lib.core.runner_schema import QA_JSON_SCHEMA
from scripts._lib.core.qa_protocol_diagnostics import report_diagnostic, repair_changes_known_semantics


def assessment():
    return {
        "decision": "PASS",
        "acceptance_coverage": [{"criterion_id": "AC-01", "status": "PASS", "evidence": "test_ok passed"}],
        "negative_scenarios": [{"name": "reject duplicate", "status": "PASS", "evidence": "test_duplicate passed"}],
        "defects": [], "uncovered_risks": [], "summary": "Checks complete",
    }


def parse(data, exit_code=0, no_tests_detected=False):
    return ProductionRunner.__new__(ProductionRunner)._parse_qa_structured_json(
        json.dumps(data) if isinstance(data, dict) else data,
        task_id="T0014", baseline_commit="a" * 40, candidate_commit="b" * 40,
        session_id="runner-session", invocation_id="real-host:step_7", qa_request_id="request-1",
        acceptance_criteria_hash="c" * 64, expected_criterion_ids=["AC-01"],
        expected_test_commands=["npm test"], expected_command_results=[{
            "command": "npm test", "exit_code": exit_code, "summary": "observed result", "output_hash": "d" * 64,
            "no_tests_detected": no_tests_detected,
        }],
    )


@pytest.mark.parametrize("coverage_key", ["acceptance_coverage", "acceptance_matrix", "acceptance_criteria", "criteria"])
@pytest.mark.parametrize("decision_key", ["decision", "verdict", "status"])
def test_wire_variants_have_identical_authoritative_envelope(coverage_key, decision_key):
    data = assessment()
    data[coverage_key] = data.pop("acceptance_coverage")
    data[decision_key] = data.pop("decision")
    result = parse(data)
    assert result.decision == "PASS"
    assert result.session_id == "runner-session"
    assert result.test_commands[0]["summary"] == "observed result"


def test_real_status_commands_shape():
    data = assessment()
    data["status"] = data.pop("decision")
    data["acceptance_matrix"] = data.pop("acceptance_coverage")
    data.pop("defects")
    data["commands"] = [{"command": "npm test", "exit_code": 0, "output_hash": "d" * 64,
                         "summary": "2 passed", "counts": {"passed": 2}, "no_tests_detected": False}]
    data["negative_scenarios"][0]["criterion_id"] = "AC-01"
    assert parse(data).decision == "PASS"


def test_real_first_attempt_negative_observation_shape():
    data = assessment()
    data["verdict"] = data.pop("decision")
    data["acceptance_criteria"] = data.pop("acceptance_coverage")
    data["negative_scenarios"] = [{"scenario": "reject duplicate", "verdict": "PASS", "observed_behavior": "test_duplicate passed"}]
    assert parse(data).decision == "PASS"
    data["negative_scenarios"][0]["status"] = "FAIL"
    assert parse(data).decision == "FAIL"


@pytest.mark.parametrize("mutation", ["verdict", "identity", "command", "hash", "boolean", "unknown", "nested"])
@pytest.mark.parametrize("legacy", [False, True])
def test_both_paths_reject_conflicts(mutation, legacy):
    data = assessment()
    if legacy:
        data["criteria"] = data.pop("acceptance_coverage")
    if mutation == "verdict":
        data["status"] = "FAIL"
    elif mutation == "identity":
        data["candidate_commit"] = "e" * 40
    elif mutation == "unknown":
        data["ignored_failures"] = ["critical"]
    elif mutation == "nested":
        data["criteria" if legacy else "acceptance_coverage"][0]["id"] = "AC-99"
    else:
        data["commands"] = [{"command": "npm test", "exit_code": False if mutation == "boolean" else (1 if mutation == "command" else 0)}]
        if mutation == "hash":
            data["commands"][0]["output_hash"] = "e" * 64
    assert parse(data).decision == "FAIL"


def test_runner_failure_cannot_become_model_pass():
    assert parse(assessment(), exit_code=1).decision == "FAIL"


def test_zero_tests_cannot_become_model_pass_or_human_infrastructure_pause():
    result = parse(assessment(), no_tests_detected=True)
    assert result.decision == "FAIL"
    assert result.defects[0]["defect_id"].endswith("QA-NO-TESTS")


def test_duplicate_keys_and_multiple_distinct_reports_rejected():
    data = assessment()
    raw = json.dumps(data).replace('"decision": "PASS"', '"decision": "FAIL", "decision": "PASS"')
    assert parse(raw).decision == "FAIL"
    other = copy.deepcopy(data)
    other["decision"] = "FAIL"
    assert _extract_embedded_json_object(json.dumps(data) + json.dumps(other), QA_JSON_SCHEMA["required"]) is None


def test_wire_schema_excludes_runner_owned_fields_and_fingerprint_ignores_order():
    assert set(QA_MODEL_JSON_SCHEMA["required"]) == set(assessment())
    first = assessment()
    second = copy.deepcopy(first)
    second["status"] = second.pop("decision")
    second["criteria"] = second.pop("acceptance_coverage")
    assert _qa_semantic_fingerprint(json.dumps(first)) == _qa_semantic_fingerprint(json.dumps(second))
    second["criteria"][0]["evidence"] = "fabricated"
    assert _qa_semantic_fingerprint(json.dumps(first)) != _qa_semantic_fingerprint(json.dumps(second))


def test_complete_real_report_is_field_error_not_json_truncation():
    data = assessment()
    data['acceptance_coverage'][0]['status'] = 'COVERED'
    data['negative_scenarios'] = [{'scenario': 'reject duplicate', 'observed_behavior': 'test_duplicate passed'}]
    result = parse(data)
    assert result.decision == 'FAIL'
    assert '$.acceptance_coverage[0].status' in result.summary
    assert '$.negative_scenarios[0].status' in result.summary
    assert 'JSON_FIELD_CONTRACT' in result.summary
    assert 'not return a valid' not in result.summary
    assert not repair_changes_known_semantics(data, assessment())
    reversed_verdict = assessment()
    reversed_verdict['decision'] = 'FAIL'
    assert repair_changes_known_semantics(data, reversed_verdict)


def test_missing_fields_cannot_be_inferred_from_overall_pass():
    data = assessment()
    del data['negative_scenarios'][0]['status']
    assert parse(data).decision == 'FAIL'
    data['negative_scenarios'][0]['status'] = 'PASS'
    data['acceptance_coverage'][0]['status'] = 'COVERED'
    assert parse(data).decision == 'FAIL'


def test_diagnostics_distinguish_syntax_from_fields_without_dumping_content():
    raw = '{"summary":"private-secret'
    diag = report_diagnostic(raw)
    assert diag['kind'] == 'JSON_SYNTAX_OR_FRAMING'
    assert diag['output_chars'] == len(raw)
    assert 'private-secret' not in diag['detail']
    assert 'does not establish token truncation' in diag['detail']
    assert parse('x' * 262145).decision == 'FAIL'


def test_large_complete_unicode_report_is_not_silently_cut():
    data = assessment()
    data['summary'] = '测试完成🚨' * 1200
    assert parse(data).decision == 'PASS'
    assert QA_MODEL_JSON_SCHEMA['properties']['summary']['maxLength'] == 400
    assert 'maxLength' not in QA_JSON_SCHEMA['properties']['summary']
