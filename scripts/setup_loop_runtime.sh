#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON_BIN="${PYTHON_BIN:-python3}"

"${PYTHON_BIN}" -m venv "${ROOT}/.venv"
if ! "${ROOT}/.venv/bin/python" -m pip install --upgrade pip; then
  echo "warning: pip self-upgrade failed; continuing with the venv pip" >&2
fi
"${ROOT}/.venv/bin/python" -m pip install -r "${ROOT}/requirements.txt"

mkdir -p "${HOME}/.codex/skills"
ln -sfn "${ROOT}/literature-loop-capture" "${HOME}/.codex/skills/literature-loop-capture"

echo "runtime_python=${ROOT}/.venv/bin/python"
echo "skill_symlink=${HOME}/.codex/skills/literature-loop-capture"
