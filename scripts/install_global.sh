#!/usr/bin/env bash
# YY-Flow shared canonical installer (Linux/macOS).
# Default source: HLi9725/multi-agent-flow
# Default ref: main
# A successful static parse of this file is not a completed install.
set -u

SOURCE="${YY_FLOW_INSTALL_SOURCE:-HLi9725/multi-agent-flow}"
REF="${1:-main}"
DEST="${YY_FLOW_INSTALL_DEST:-}"
ROOT="$(cd "$(dirname "$0")" && pwd)"
INSTALLER="${ROOT}/_lib/onboarding/global_install.py"

if [ ! -f "${INSTALLER}" ]; then
  echo "[FAILED] missing installer module"
  exit 2
fi

PY="python3"
if ! command -v python3 >/dev/null 2>&1; then
  PY="python"
fi

set +e
if [ -n "${DEST}" ]; then
  "${PY}" "${INSTALLER}" --source "${SOURCE}" --ref "${REF}" --dest "${DEST}"
else
  "${PY}" "${INSTALLER}" --source "${SOURCE}" --ref "${REF}"
fi
status=$?
set -e
if [ "${status}" -ne 0 ]; then
  echo "[FAILED] installer exit ${status}"
  exit "${status}"
fi
echo "[SUCCESS] shared canonical install finished"
exit 0
