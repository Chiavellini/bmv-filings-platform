#!/usr/bin/env bash
set -euo pipefail

if [[ "$#" -ne 2 ]]; then
  echo "usage: scripts/run_airflow_native.sh /absolute/path/to/airflow.env COMPONENT" >&2
  exit 2
fi

environment_file="$1"
component="$2"

case "${component}" in
  api-server|scheduler|dag-processor) ;;
  *)
    echo "unsupported Airflow component: ${component}" >&2
    exit 2
    ;;
esac

if [[ "${environment_file}" != /* || ! -r "${environment_file}" ]]; then
  echo "Airflow environment must be a readable absolute path: ${environment_file}" >&2
  exit 2
fi

set -a
# The generated file is owner-only and shell-safe JSON-quoted dotenv.
# shellcheck disable=SC1090
source "${environment_file}"
set +a

: "${PDFS_PROJECT_ROOT:?PDFS_PROJECT_ROOT is required}"
: "${PDFS_AIRFLOW_VENV:?PDFS_AIRFLOW_VENV is required}"

if [[ "${PDFS_PROJECT_ROOT}" != /* || ! -d "${PDFS_PROJECT_ROOT}" ]]; then
  echo "PDFS_PROJECT_ROOT must be an existing absolute directory" >&2
  exit 2
fi
airflow_executable="${PDFS_AIRFLOW_VENV}/bin/airflow"
if [[ "${PDFS_AIRFLOW_VENV}" != /* || ! -x "${airflow_executable}" ]]; then
  echo "configured Airflow executable is missing: ${airflow_executable}" >&2
  exit 2
fi

cd "${PDFS_PROJECT_ROOT}"
exec "${airflow_executable}" "${component}"
