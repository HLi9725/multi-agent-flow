"""Bounded, value-free diagnostics for model-owned QA wire reports.

Never infer PASS from COVERED, prose, a missing status, or a truncated payload.
These diagnostics tell the host exactly what to repair, not how to decide.
"""
import hashlib
import json
from collections.abc import Mapping

MAX_QA_OUTPUT_CHARS = 262144


def report_diagnostic(raw, report=None):
    info = {
        "output_chars": len(raw),
        "output_sha256": hashlib.sha256(raw.encode('utf-8', errors='replace')).hexdigest(),
    }
    if len(raw) > MAX_QA_OUTPUT_CHARS:
        return {**info, "kind": "OUTPUT_TOO_LARGE", "detail": "QA output exceeds 262144 characters; return a compact complete report."}
    if not isinstance(report, Mapping):
        try:
            json.loads(raw.strip())
        except json.JSONDecodeError as exc:
            return {**info, "kind": "JSON_SYNTAX_OR_FRAMING", "detail":
                    f"No unambiguous complete report; JSON decoder: {exc.msg} at line {exc.lineno}, column {exc.colno}. "
                    "This alone does not establish token truncation. Return one complete JSON object."}
        except (ValueError, RecursionError):
            pass
        return {**info, "kind": "JSON_AMBIGUOUS_OR_INVALID_ROOT", "detail":
                "Expected one report object without duplicate keys or conflicting terminal reports."}
    problems = []
    for key in ('decision', 'verdict', 'status'):
        if key in report and report[key] not in ('PASS', 'FAIL', 'pass', 'fail'):
            problems.append(f'$.{key}: must be PASS or FAIL; do not use COVERED/OK/REJECT')
    for key in ('acceptance_coverage', 'criteria', 'acceptance_criteria', 'acceptance_matrix'):
        items = report.get(key)
        if not isinstance(items, list):
            continue
        for index, item in enumerate(items):
            if not isinstance(item, Mapping):
                problems.append(f'$.{key}[{index}]: must be an object')
                continue
            status = item.get('status', item.get('decision'))
            if status not in ('PASS', 'FAIL', 'pass', 'fail'):
                problems.append(f'$.{key}[{index}].status: explicitly assess PASS or FAIL; COVERED is not a verdict')
    negatives = report.get('negative_scenarios', [])
    if isinstance(negatives, list):
        for index, item in enumerate(negatives):
            if not isinstance(item, Mapping):
                problems.append(f'$.negative_scenarios[{index}]: requires name, status and evidence')
                continue
            if item.get('status', item.get('verdict')) not in ('PASS', 'FAIL', 'pass', 'fail'):
                problems.append(f'$.negative_scenarios[{index}].status: missing/invalid; explicitly assess PASS or FAIL from evidence')
            if not item.get('evidence', item.get('observed_behavior')):
                problems.append(f'$.negative_scenarios[{index}].evidence: observed result required; a target/name is not evidence')
    # Include errors from both coverage and negative scenarios without dumping
    # source prose or credentials into status diagnostics.
    return {**info, "kind": "JSON_FIELD_CONTRACT", "detail": '; '.join(problems[:4] + problems[-4:]) if len(problems) > 8 else '; '.join(problems)
            or 'Valid JSON, but fields conflict, have unsupported types/aliases, or disagree with Runner-owned observations.'}


def repair_changes_known_semantics(before, after):
    """Allow resolution of invalid/missing statuses, never reversal of known facts."""
    if not isinstance(before, Mapping) or not isinstance(after, Mapping):
        return False
    def verdict(item, keys):
        return next((item[k].upper() for k in keys if isinstance(item.get(k), str)), None)
    old = verdict(before, ('decision', 'verdict', 'status'))
    if old in ('PASS', 'FAIL') and old != verdict(after, ('decision', 'verdict', 'status')):
        return True
    def coverage(report):
        return next((report[k] for k in ('acceptance_coverage', 'criteria', 'acceptance_criteria', 'acceptance_matrix') if isinstance(report.get(k), list)), [])
    for old_items, new_items, names, statuses in (
        (coverage(before), coverage(after), ('criterion_id', 'id'), ('status', 'decision')),
        (before.get('negative_scenarios', []), after.get('negative_scenarios', []), ('name', 'scenario'), ('status', 'verdict')),
    ):
        if not isinstance(old_items, list) or not isinstance(new_items, list):
            continue
        def name(item):
            return next((item[k] for k in names if isinstance(item.get(k), str)), None)
        indexed = {name(item): item for item in new_items if isinstance(item, Mapping)}
        for item in old_items:
            if not isinstance(item, Mapping) or name(item) is None:
                continue
            new = indexed.get(name(item))
            if new is None:
                return True
            old_status = verdict(item, statuses)
            if old_status in ('PASS', 'FAIL') and old_status != verdict(new, statuses):
                return True
            evidence = item.get('evidence', item.get('proof', item.get('observed_behavior')))
            if evidence is not None and evidence != new.get('evidence', new.get('proof', new.get('observed_behavior'))):
                return True
    for key in ('defects', 'uncovered_risks'):
        if isinstance(before.get(key), list):
            stable = lambda xs: sorted(json.dumps(x, sort_keys=True) for x in xs)
            if not isinstance(after.get(key, []), list) or stable(before[key]) != stable(after.get(key, [])):
                return True
    return False
