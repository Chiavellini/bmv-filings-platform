#!/usr/bin/env bash
set -euo pipefail

project_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
python_bin="${PDFS_AIRFLOW_PYTHON:-python3.13}"
venv_path="${PDFS_AIRFLOW_VENV:-${project_root}/.airflow-venv}"
with_playwright="false"

if [[ "${1:-}" == "--with-playwright" ]]; then
  with_playwright="true"
elif [[ -n "${1:-}" ]]; then
  echo "usage: scripts/install_airflow_native.sh [--with-playwright]" >&2
  exit 2
fi

"${python_bin}" -c '
import sys
if sys.version_info[:2] != (3, 13):
    raise SystemExit(f"Python 3.13 is required, found {sys.version.split()[0]}")
'

if [[ ! -x "${venv_path}/bin/python" ]]; then
  "${python_bin}" -m venv "${venv_path}"
fi

airflow_python="${venv_path}/bin/python"
constraints_url="https://raw.githubusercontent.com/apache/airflow/constraints-3.3.0/constraints-3.13.txt"

"${airflow_python}" -m pip install --disable-pip-version-check \
  "apache-airflow==3.3.0" \
  --constraint "${constraints_url}"
"${airflow_python}" -m pip install --disable-pip-version-check \
  "apache-airflow==3.3.0" \
  -e "${project_root}[browser]"
"${airflow_python}" -m pip check

if [[ "${with_playwright}" == "true" ]]; then
  "${airflow_python}" -m playwright install chromium
fi

echo "Native Airflow environment ready: ${venv_path}"
echo "Next: ${venv_path}/bin/python scripts/configure_airflow.py --estate /absolute/estate/path"
