#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
venv_dir="${repo_root}/.venv"

if [[ ! -x "${venv_dir}/bin/python" ]]; then
  python3 -m venv "${venv_dir}"
fi

"${venv_dir}/bin/pip" install --quiet -r "${repo_root}/api/requirements.txt"
cd "${repo_root}/api"
"${venv_dir}/bin/ruff" check app tests alembic
"${venv_dir}/bin/python" -m pytest -q
"${venv_dir}/bin/python" -m compileall -q app
