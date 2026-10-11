#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
ENV_FILE="${ENV_FILE:-${REPO_ROOT}/.env}"
PROFILES_DIR="${PROFILES_DIR:-${REPO_ROOT}/profiles}"

dry_run="false"
force="false"
prefix_override=""
older_than_days=""

read_env_value() {
  local key="$1"
  local file="$2"
  if [[ ! -f "${file}" ]]; then
    return 1
  fi
  local line
  line="$(grep -E "^[[:space:]]*${key}=" "${file}" | tail -n 1 || true)"
  if [[ -z "${line}" ]]; then
    return 1
  fi
  line="${line#*=}"
  line="${line%%#*}"
  line="${line%$'\r'}"
  printf '%s' "${line}"
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --prefix)
      if [[ $# -lt 2 ]]; then
        echo "Usage: $0 [--prefix PREFIX] [--older-than-days DAYS] [--dry-run] [--force]" >&2
        exit 1
      fi
      prefix_override="$2"
      shift 2
      ;;
    --older-than-days)
      if [[ $# -lt 2 ]]; then
        echo "Usage: $0 [--prefix PREFIX] [--older-than-days DAYS] [--dry-run] [--force]" >&2
        exit 1
      fi
      older_than_days="$2"
      shift 2
      ;;
    --dry-run)
      dry_run="true"
      shift
      ;;
    --force)
      force="true"
      shift
      ;;
    *)
      echo "Unknown argument: $1" >&2
      echo "Usage: $0 [--prefix PREFIX] [--older-than-days DAYS] [--dry-run] [--force]" >&2
      exit 1
      ;;
  esac
done

if [[ -n "${older_than_days}" ]]; then
  if [[ ! "${older_than_days}" =~ ^[0-9]+$ ]]; then
    echo "Invalid --older-than-days value: '${older_than_days}' (expected a non-negative integer)" >&2
    exit 1
  fi
fi

cleanup_prefix="${prefix_override}"
if [[ -z "${cleanup_prefix}" ]]; then
  cleanup_prefix="$(read_env_value "PROFILE_CLEANUP_PREFIX" "${ENV_FILE}" || true)"
fi
if [[ -z "${cleanup_prefix}" ]]; then
  cleanup_prefix="$(read_env_value "CARLIST_PROFILE_CLEANUP_PREFIX" "${ENV_FILE}" || true)"
fi
if [[ -z "${cleanup_prefix}" ]]; then
  cleanup_prefix="$(read_env_value "BOTASAURUS_PROFILE" "${ENV_FILE}" || true)"
fi
if [[ -z "${cleanup_prefix}" ]]; then
  echo "Missing cleanup prefix. Pass --prefix or set PROFILE_CLEANUP_PREFIX/BOTASAURUS_PROFILE." >&2
  exit 1
fi

if [[ ! -d "${PROFILES_DIR}" ]]; then
  echo "Profiles directory not found: ${PROFILES_DIR}"
  exit 0
fi

shopt -s nullglob
matches=("${PROFILES_DIR}/${cleanup_prefix}"*)
shopt -u nullglob

existing=()
for path in "${matches[@]}"; do
  if [[ -d "${path}" ]]; then
    existing+=("${path}")
  fi
done

if [[ -n "${older_than_days}" ]]; then
  cutoff_epoch=$(( $(date +%s) - older_than_days * 86400 ))
  aged=()
  for path in "${existing[@]}"; do
    # Symlinks are never eligible (protect linked targets).
    if [[ -L "${path}" ]]; then
      continue
    fi
    mtime_epoch="$(stat -c %Y -- "${path}" 2>/dev/null || true)"
    if [[ -z "${mtime_epoch}" ]]; then
      continue
    fi
    # Strictly older than DAYS*24h: exact N days and newer are retained.
    if [[ "${mtime_epoch}" -lt "${cutoff_epoch}" ]]; then
      aged+=("${path}")
    fi
  done
  existing=("${aged[@]}")
  echo "Retaining directories newer than or equal to ${older_than_days} day(s); ${#existing[@]} eligible to remove."
fi

if [[ ${#existing[@]} -eq 0 ]]; then
  echo "No profiles matched prefix '${cleanup_prefix}' in ${PROFILES_DIR}"
  exit 0
fi

if [[ "${force}" != "true" ]] && command -v lsof >/dev/null 2>&1; then
  busy=()
  for path in "${existing[@]}"; do
    if lsof +D "${path}" >/dev/null 2>&1; then
      busy+=("${path}")
    fi
  done
  if [[ ${#busy[@]} -gt 0 ]]; then
    echo "Some profile directories are still in use; skipping cleanup:"
    for path in "${busy[@]}"; do
      echo " - ${path}"
    done
    exit 0
  fi
fi

echo "Matched ${#existing[@]} profile path(s) for prefix '${cleanup_prefix}':"
for path in "${existing[@]}"; do
  echo " - ${path}"
done

if [[ "${dry_run}" == "true" ]]; then
  echo "Dry-run only; nothing deleted."
  exit 0
fi

deleted=0
for path in "${existing[@]}"; do
  rm -rf "${path}"
  echo "Deleted ${path}"
  deleted=$((deleted + 1))
done

if [[ ${deleted} -eq 1 ]]; then
  echo "Deleted 1 profile directory."
else
  echo "Deleted ${deleted} profile directories."
fi
