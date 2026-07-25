#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(CDPATH='' cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
SNAPSHOT_SCRIPT="${SCRIPT_DIR}/source_snapshot.py"
LOCK_SCRIPT="${SCRIPT_DIR}/review_lock.py"
STATE_SCRIPT="${SCRIPT_DIR}/review_state.py"
PUBLISH_SCRIPT="${SCRIPT_DIR}/review_publish.py"

usage() {
    cat <<'EOF'
Usage:
  review-json.sh init REPO NAME
  review-json.sh validate REPO REVIEW_ID
  review-json.sh validate-event REPO REVIEW_ID
  review-json.sh report REPO REVIEW_ID TOKEN
  review-json.sh inspect REPO REVIEW_ID \
    [--exclude PATH] [--additional-input PATH] SCOPE...
  review-json.sh template REPO REVIEW_ID TOKEN KIND
  review-json.sh snapshot REPO [--exclude PATH] [--additional-input PATH] SCOPE...
  review-json.sh lock acquire|status REPO REVIEW_ID
  review-json.sh lock release REPO REVIEW_ID TOKEN
  review-json.sh publish REPO REVIEW_ID TOKEN REVIEW_SHA SOURCE_FINGERPRINT \
    [--exclude PATH] [--additional-input PATH] SCOPE...
  review-json.sh recover-publish REPO REVIEW_ID [TOKEN]

NAME must contain only lowercase letters, digits, and hyphens. init generates
and prints a short random REVIEW_ID. Artifacts are always stored in
REPO/.local/reviews as REVIEW_ID.json, REVIEW_ID.latest.md,
REVIEW_ID.event.json, and a temporary REVIEW_ID.publish.json receipt.

KIND is review, source_update, owner_reply, reviewer_update, final_review,
reviewer_timeout, or owner_timeout.
EOF
}

require_python() {
    command -v python3 >/dev/null 2>&1 || {
        printf '%s\n' 'Python 3.9 or newer is required' >&2
        return 1
    }
    python3 -c '
import sys

if sys.version_info < (3, 9):
    raise SystemExit(1)
' || {
        printf '%s\n' 'Python 3.9 or newer is required' >&2
        return 1
    }
}

set_repo_paths() {
    local repo="$1"
    REPO_ROOT="$(git -C "${repo}" rev-parse --show-toplevel)"
    LOCAL_DIR="${REPO_ROOT}/.local"
    REVIEW_DIR="${REPO_ROOT}/.local/reviews"
    if [[ -L "${LOCAL_DIR}" || -L "${REVIEW_DIR}" ]]; then
        printf '%s\n' 'review storage directories must not be symlinks' >&2
        return 1
    fi
    if ! git -C "${REPO_ROOT}" check-ignore -q --no-index \
        .local/reviews/.review-loop-probe; then
        printf '%s\n' \
            'REPO/.local must be ignored before creating a review loop' >&2
        return 1
    fi
}

set_review_paths() {
    local repo="$1"
    local review_id="$2"
    [[ "${review_id}" =~ ^[abcdefghjkmnpqrstuvwxyz23456789]{8}$ ]] || {
        printf 'invalid review ID: %s\n' "${review_id}" >&2
        return 1
    }

    set_repo_paths "${repo}"
    REVIEW_JSON="${REVIEW_DIR}/${review_id}.json"
    LATEST_REPORT="${REVIEW_DIR}/${review_id}.latest.md"
    EVENT_JSON="${REVIEW_DIR}/${review_id}.event.json"
    JOURNAL_JSON="${REVIEW_DIR}/${review_id}.publish.json"
}

new_review_id() {
    python3 -c '
import secrets

alphabet = "abcdefghjkmnpqrstuvwxyz23456789"
print("".join(secrets.choice(alphabet) for _ in range(8)))
'
}

sha256_file() {
    python3 -c '
import hashlib
import pathlib
import sys

print(hashlib.sha256(pathlib.Path(sys.argv[1]).read_bytes()).hexdigest())
' "$1"
}

snapshot() {
    local repo="$1"
    shift
    python3 "${SNAPSHOT_SCRIPT}" --repo "${repo}" "$@"
}

source_fingerprint() {
    python3 -c 'import json, sys; print(json.load(sys.stdin)["fingerprint"])'
}

require_regular_file() {
    local path="$1"
    local label="$2"
    [[ -f "${path}" && ! -L "${path}" ]] || {
        printf '%s must be a regular non-symlink file: %s\n' \
            "${label}" "${path}" >&2
        return 1
    }
}

secure_temp() {
    mktemp "${1}.tmp.XXXXXX"
}

validate_file() {
    local review_json="$1"
    local review_id="$2"
    require_regular_file "${review_json}" 'review JSON'
    python3 "${STATE_SCRIPT}" validate --review-id "${review_id}" < "${review_json}"
}

validate_event_file() {
    require_regular_file "$1" 'event JSON'
    python3 "${STATE_SCRIPT}" validate-event < "$1"
}

render_report() {
    local review_json="$1"
    local output="$2"
    local temporary
    temporary="$(secure_temp "${output}")"
    if ! python3 "${STATE_SCRIPT}" report < "${review_json}" > "${temporary}"; then
        rm -f "${temporary}"
        return 1
    fi
    mv "${temporary}" "${output}"
}

init_review() {
    local repo="$1"
    local name="$2"
    [[ "${name}" =~ ^[a-z0-9]+(-[a-z0-9]+)*$ ]] || {
        printf 'invalid review name: %s\n' "${name}" >&2
        return 1
    }
    set_repo_paths "${repo}"

    local review_id
    while :; do
        review_id="$(new_review_id)"
        set_review_paths "${REPO_ROOT}" "${review_id}"
        if [[ ! -e "${REVIEW_JSON}" && ! -L "${REVIEW_JSON}" \
            && ! -e "${LATEST_REPORT}" && ! -L "${LATEST_REPORT}" \
            && ! -e "${EVENT_JSON}" && ! -L "${EVENT_JSON}" \
            && ! -e "${JOURNAL_JSON}" && ! -L "${JOURNAL_JSON}" ]]; then
            break
        fi
    done
    mkdir -p "${REVIEW_DIR}"
    local next_review
    local next_report
    next_review="$(secure_temp "${REVIEW_JSON}")"
    next_report="$(secure_temp "${LATEST_REPORT}")"
    if ! python3 "${STATE_SCRIPT}" init "${review_id}" "${name}" > "${next_review}"; then
        rm -f "${next_review}" "${next_report}"
        return 1
    fi
    if ! validate_file "${next_review}" "${review_id}" >/dev/null; then
        rm -f "${next_review}" "${next_report}"
        return 1
    fi
    if ! python3 "${STATE_SCRIPT}" report < "${next_review}" > "${next_report}"; then
        rm -f "${next_review}" "${next_report}"
        return 1
    fi
    mv "${next_review}" "${REVIEW_JSON}"
    mv "${next_report}" "${LATEST_REPORT}"
    printf 'review_id: %s\nreview_json: %s\nlatest_report: %s\n' \
        "${review_id}" "${REVIEW_JSON}" "${LATEST_REPORT}"
}

inspect() {
    local repo="$1"
    local review_id="$2"
    shift 2
    set_review_paths "${repo}" "${review_id}"
    validate_file "${REVIEW_JSON}" "${review_id}" >/dev/null
    printf 'review_json: %s\nlatest_report: %s\nevent_json: %s\njournal_json: %s\n' \
        "${REVIEW_JSON}" "${LATEST_REPORT}" "${EVENT_JSON}" "${JOURNAL_JSON}"
    printf '%s\n' 'workflow:'
    python3 "${STATE_SCRIPT}" state < "${REVIEW_JSON}"
    printf 'review_sha256: %s\n' "$(sha256_file "${REVIEW_JSON}")"
    local lock_status
    lock_status="$(
        python3 "${LOCK_SCRIPT}" status \
            --repo "${REPO_ROOT}" --review-file "${REVIEW_JSON}"
    )"
    printf '%s\n' 'operation:'
    python3 "${PUBLISH_SCRIPT}" operation \
        --review "${REVIEW_JSON}" \
        --event "${EVENT_JSON}" \
        --report "${LATEST_REPORT}" \
        --journal "${JOURNAL_JSON}" \
        --state-script "${STATE_SCRIPT}" \
        --lock-json "${lock_status}"
    printf '%s\n' 'source_snapshot:'
    snapshot "${REPO_ROOT}" "$@"
}

template() {
    local repo="$1"
    local review_id="$2"
    local token="$3"
    local kind="$4"
    set_review_paths "${repo}" "${review_id}"
    validate_file "${REVIEW_JSON}" "${review_id}" >/dev/null
    python3 "${LOCK_SCRIPT}" verify \
        --repo "${REPO_ROOT}" --review-file "${REVIEW_JSON}" --token "${token}" >/dev/null
    if [[ -e "${EVENT_JSON}" || -L "${EVENT_JSON}" ]]; then
        printf 'event file already exists: %s\n' "${EVENT_JSON}" >&2
        return 1
    fi
    if [[ -e "${JOURNAL_JSON}" || -L "${JOURNAL_JSON}" ]]; then
        printf 'publication recovery is required first: %s\n' "${JOURNAL_JSON}" >&2
        return 1
    fi
    mkdir -p "${REVIEW_DIR}"
    local next_event
    next_event="$(secure_temp "${EVENT_JSON}")"
    if ! python3 "${STATE_SCRIPT}" template "${kind}" > "${next_event}"; then
        rm -f "${next_event}"
        return 1
    fi
    mv "${next_event}" "${EVENT_JSON}"
    printf '%s\n' "${EVENT_JSON}"
}

lock() {
    local action="$1"
    local repo="$2"
    local review_id="$3"
    shift 3
    set_review_paths "${repo}" "${review_id}"
    case "${action}" in
        acquire)
            validate_file "${REVIEW_JSON}" "${review_id}" >/dev/null
            python3 "${LOCK_SCRIPT}" acquire \
                --repo "${REPO_ROOT}" --review-file "${REVIEW_JSON}"
            ;;
        status)
            python3 "${LOCK_SCRIPT}" "${action}" \
                --repo "${REPO_ROOT}" --review-file "${REVIEW_JSON}"
            ;;
        release)
            [[ "$#" -eq 1 ]] || return 2
            python3 "${LOCK_SCRIPT}" release \
                --repo "${REPO_ROOT}" --review-file "${REVIEW_JSON}" --token "$1"
            ;;
        *)
            return 2
            ;;
    esac
}

guard() {
    local repo="$1"
    local review_id="$2"
    local token="$3"
    local expected_review_sha="$4"
    local expected_source_fingerprint="$5"
    shift 5
    set_review_paths "${repo}" "${review_id}"
    validate_file "${REVIEW_JSON}" "${review_id}" >/dev/null

    python3 "${LOCK_SCRIPT}" verify \
        --repo "${REPO_ROOT}" --review-file "${REVIEW_JSON}" --token "${token}" >/dev/null

    local actual_review_sha
    actual_review_sha="$(sha256_file "${REVIEW_JSON}")"
    if [[ "${actual_review_sha}" != "${expected_review_sha}" ]]; then
        printf 'review JSON changed: expected %s, found %s\n' \
            "${expected_review_sha}" "${actual_review_sha}" >&2
        return 1
    fi

    local snapshot_json
    local actual_source_fingerprint
    snapshot_json="$(snapshot "${REPO_ROOT}" "$@")"
    actual_source_fingerprint="$(printf '%s\n' "${snapshot_json}" | source_fingerprint)"
    if [[ "${actual_source_fingerprint}" != "${expected_source_fingerprint}" ]]; then
        printf 'source changed: expected %s, found %s\n' \
            "${expected_source_fingerprint}" "${actual_source_fingerprint}" >&2
        return 1
    fi
}

publish() {
    local repo="$1"
    local review_id="$2"
    local token="$3"
    local expected_review_sha="$4"
    local expected_source_fingerprint="$5"
    shift 5
    set_review_paths "${repo}" "${review_id}"

    guard "${REPO_ROOT}" "${review_id}" "${token}" \
        "${expected_review_sha}" "${expected_source_fingerprint}" "$@"
    validate_event_file "${EVENT_JSON}" >/dev/null

    local event_snapshot
    event_snapshot="$(
        python3 "${STATE_SCRIPT}" source-snapshot < "${EVENT_JSON}"
    )"
    if [[ "${event_snapshot}" != "null" ]]; then
        local current_snapshot
        current_snapshot="$(snapshot "${REPO_ROOT}" "$@")"
        if ! EVENT_SNAPSHOT="${event_snapshot}" CURRENT_SNAPSHOT="${current_snapshot}" \
            python3 -c '
import json
import os
import sys

event = json.loads(os.environ["EVENT_SNAPSHOT"])
current = json.loads(os.environ["CURRENT_SNAPSHOT"])
keys = ("revision", "scope", "exclusions", "fingerprint")
matches = all(event.get(key) == current.get(key) for key in keys)
matches = matches and event.get("additional_inputs", []) == current.get("additional_inputs", [])
sys.exit(0 if matches else 1)
'; then
            printf '%s\n' \
                'event source revision, scope, or fingerprint does not match guarded source' >&2
            return 1
        fi
    fi

    python3 "${PUBLISH_SCRIPT}" publish \
        --repo "${REPO_ROOT}" \
        --review "${REVIEW_JSON}" \
        --event "${EVENT_JSON}" \
        --report "${LATEST_REPORT}" \
        --journal "${JOURNAL_JSON}" \
        --state-script "${STATE_SCRIPT}" \
        --lock-script "${LOCK_SCRIPT}" \
        --token "${token}"
}

recover_publish() {
    local repo="$1"
    local review_id="$2"
    local token="${3:-}"
    set_review_paths "${repo}" "${review_id}"
    local arguments=(
        recover
        --repo "${REPO_ROOT}"
        --review "${REVIEW_JSON}"
        --event "${EVENT_JSON}"
        --report "${LATEST_REPORT}"
        --journal "${JOURNAL_JSON}"
        --state-script "${STATE_SCRIPT}"
        --lock-script "${LOCK_SCRIPT}"
    )
    if [[ -n "${token}" ]]; then
        arguments+=(--token "${token}")
    fi
    python3 "${PUBLISH_SCRIPT}" "${arguments[@]}"
}

main() {
    require_python
    [[ "$#" -ge 1 ]] || {
        usage >&2
        return 2
    }
    local command="$1"
    shift
    case "${command}" in
        init)
            [[ "$#" -eq 2 ]] || { usage >&2; return 2; }
            init_review "$@"
            ;;
        validate)
            [[ "$#" -eq 2 ]] || { usage >&2; return 2; }
            set_review_paths "$1" "$2"
            validate_file "${REVIEW_JSON}" "$2"
            ;;
        validate-event)
            [[ "$#" -eq 2 ]] || { usage >&2; return 2; }
            set_review_paths "$1" "$2"
            validate_event_file "${EVENT_JSON}"
            ;;
        report)
            [[ "$#" -eq 3 ]] || { usage >&2; return 2; }
            set_review_paths "$1" "$2"
            validate_file "${REVIEW_JSON}" "$2" >/dev/null
            python3 "${LOCK_SCRIPT}" verify \
                --repo "${REPO_ROOT}" --review-file "${REVIEW_JSON}" --token "$3" >/dev/null
            render_report "${REVIEW_JSON}" "${LATEST_REPORT}"
            ;;
        inspect)
            [[ "$#" -ge 3 ]] || { usage >&2; return 2; }
            inspect "$@"
            ;;
        template)
            [[ "$#" -eq 4 ]] || { usage >&2; return 2; }
            template "$@"
            ;;
        snapshot)
            [[ "$#" -ge 2 ]] || { usage >&2; return 2; }
            snapshot "$@"
            ;;
        lock)
            [[ "$#" -ge 3 ]] || { usage >&2; return 2; }
            lock "$@"
            ;;
        publish)
            [[ "$#" -ge 6 ]] || { usage >&2; return 2; }
            publish "$@"
            ;;
        recover-publish)
            [[ "$#" -ge 2 && "$#" -le 3 ]] || { usage >&2; return 2; }
            recover_publish "$@"
            ;;
        *)
            usage >&2
            return 2
            ;;
    esac
}

main "$@"
