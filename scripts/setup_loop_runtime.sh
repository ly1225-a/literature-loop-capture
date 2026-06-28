#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON_BIN="${PYTHON_BIN:-python3}"

"${PYTHON_BIN}" -m venv "${ROOT}/.venv"
if ! "${ROOT}/.venv/bin/python" -m pip install --upgrade pip; then
  echo "warning: pip self-upgrade failed; continuing with the venv pip" >&2
fi
"${ROOT}/.venv/bin/python" -m pip install -r "${ROOT}/requirements.txt"

echo "runtime_python=${ROOT}/.venv/bin/python"
if [[ -n "${AGENT_SKILL_DIR:-}" ]]; then
  mkdir -p "${AGENT_SKILL_DIR}"
  ln -sfn "${ROOT}/literature-loop-capture" "${AGENT_SKILL_DIR}/literature-loop-capture"
  echo "skill_symlink=${AGENT_SKILL_DIR}/literature-loop-capture"
else
  echo "skill_symlink=not_created"
  echo "set AGENT_SKILL_DIR to create a skill symlink"
fi
