#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(CDPATH='' cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
SNAPSHOT_SCRIPT="${SCRIPT_DIR}/source_snapshot.py"
LOCK_SCRIPT="${SCRIPT_DIR}/review_lock.py"
STATE_SCRIPT="${SCRIPT_DIR}/review_state.py"
PUBLISH_SCRIPT="${SCRIPT_DIR}/review_publish.py"
WORKFLOW_SCRIPT="${SCRIPT_DIR}/review_workflow.py"

usage() {
    cat <<'EOF'
Usage:
  review-json.sh init REPO NAME
  review-json.sh validate REPO REVIEW_ID
  review-json.sh validate-event REPO REVIEW_ID
  review-json.sh inspect REPO REVIEW_ID [--json] \
    [--exclude PATH] [--additional-input PATH] SCOPE...
  review-json.sh template REPO REVIEW_ID KIND
  review-json.sh threads REPO REVIEW_ID [--json]
  review-json.sh add-check REPO REVIEW_ID passed|failed CHECK \
    [BASIS PROVENANCE SANITIZED_RESULT [ARTIFACT_DIGEST]]
  review-json.sh add-gap REPO REVIEW_ID CHECK REASON [--material]
  review-json.sh evidence-template BASIS
  review-json.sh snapshot REPO [--exclude PATH] [--additional-input PATH] SCOPE...
  review-json.sh lock acquire|status REPO REVIEW_ID
  review-json.sh lock release REPO REVIEW_ID
  review-json.sh publish REPO REVIEW_ID
  review-json.sh abort-draft REPO REVIEW_ID
  review-json.sh recover-publish REPO REVIEW_ID
  review-json.sh regenerate-report REPO REVIEW_ID
  review-json.sh wait REPO REVIEW_ID [SECONDS]
  review-json.sh publish-timeout REPO REVIEW_ID --if-eligible
  review-json.sh start-follow-up REPO PRIOR_REVIEW_ID NAME

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
    LEASE_JSON="${REVIEW_DIR}/${review_id}.lease.json"
    GUARD_JSON="${REVIEW_DIR}/${review_id}.guard.json"
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
    local prior_review_id="${3:-}"
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
            && ! -e "${JOURNAL_JSON}" && ! -L "${JOURNAL_JSON}" \
            && ! -e "${LEASE_JSON}" && ! -L "${LEASE_JSON}" \
            && ! -e "${GUARD_JSON}" && ! -L "${GUARD_JSON}" ]]; then
            break
        fi
    done
    mkdir -p "${REVIEW_DIR}"
    local next_review
    local next_report
    next_review="$(secure_temp "${REVIEW_JSON}")"
    next_report="$(secure_temp "${LATEST_REPORT}")"
    local init_arguments=(init "${review_id}" "${name}")
    if [[ -n "${prior_review_id}" ]]; then
        init_arguments+=(--prior-review-id "${prior_review_id}")
    fi
    if ! python3 "${STATE_SCRIPT}" "${init_arguments[@]}" > "${next_review}"; then
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
    local output_json=false
    if [[ "${1:-}" == "--json" ]]; then
        output_json=true
        shift
    fi
    set_review_paths "${repo}" "${review_id}"
    validate_file "${REVIEW_JSON}" "${review_id}" >/dev/null
    local lock_status
    lock_status="$(
        python3 "${LOCK_SCRIPT}" status \
            --repo "${REPO_ROOT}" --review-file "${REVIEW_JSON}"
    )"
    local snapshot_json
    if [[ -f "${LEASE_JSON}" && ! -L "${LEASE_JSON}" ]]; then
        snapshot_json="$(
            python3 "${WORKFLOW_SCRIPT}" guard \
                --repo "${REPO_ROOT}" --review "${REVIEW_JSON}" \
                --lease "${LEASE_JSON}" --guard "${GUARD_JSON}" \
                --lock-script "${LOCK_SCRIPT}" --snapshot-script "${SNAPSHOT_SCRIPT}" \
                -- "$@"
        )"
    else
        snapshot_json="$(snapshot "${REPO_ROOT}" "$@")"
    fi
    if [[ "${output_json}" == false ]]; then
        printf '%s\n' 'workflow:'
        python3 "${STATE_SCRIPT}" state < "${REVIEW_JSON}"
        printf '%s\n' 'operation:'
    fi
    local operation_arguments=(
        operation
        --review "${REVIEW_JSON}"
        --event "${EVENT_JSON}"
        --report "${LATEST_REPORT}"
        --journal "${JOURNAL_JSON}"
        --state-script "${STATE_SCRIPT}"
        --lock-json "${lock_status}"
        --repo "${REPO_ROOT}"
        --review-id "${review_id}"
        --current-source-fingerprint "$(
            printf '%s' "${snapshot_json}" | python3 -c \
                'import json,sys; v=json.load(sys.stdin); print(v.get("source_snapshot", v)["fingerprint"])'
        )"
        --command-path "$0"
    )
    if [[ -f "${LEASE_JSON}" && ! -L "${LEASE_JSON}" ]]; then
        operation_arguments+=(--lease-present)
    fi
    if [[ "${output_json}" == true ]]; then
        operation_arguments+=(--json)
    fi
    python3 "${PUBLISH_SCRIPT}" "${operation_arguments[@]}"
    if [[ "${output_json}" == true ]]; then
        return
    fi
    printf 'review_json: %s\nlatest_report: %s\nevent_json: %s\njournal_json: %s\n' \
        "${REVIEW_JSON}" "${LATEST_REPORT}" "${EVENT_JSON}" "${JOURNAL_JSON}"
    printf 'review_sha256: %s\n' "$(sha256_file "${REVIEW_JSON}")"
    printf '%s\n' 'source_snapshot:'
    printf '%s\n' "${snapshot_json}"
}

template() {
    local repo="$1"
    local review_id="$2"
    local kind="$3"
    set_review_paths "${repo}" "${review_id}"
    validate_file "${REVIEW_JSON}" "${review_id}" >/dev/null
    python3 "${WORKFLOW_SCRIPT}" verify \
        --repo "${REPO_ROOT}" --review "${REVIEW_JSON}" \
        --lease "${LEASE_JSON}" --guard "${GUARD_JSON}" \
        --lock-script "${LOCK_SCRIPT}" >/dev/null
    [[ -f "${GUARD_JSON}" && ! -L "${GUARD_JSON}" ]] || {
        printf '%s\n' 'run inspect with the guarded scope before template' >&2
        return 1
    }
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
    if ! python3 "${STATE_SCRIPT}" context-template "${kind}" "${GUARD_JSON}" \
        < "${REVIEW_JSON}" > "${next_event}"; then
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
            python3 "${WORKFLOW_SCRIPT}" acquire \
                --repo "${REPO_ROOT}" --review "${REVIEW_JSON}" \
                --lease "${LEASE_JSON}" --guard "${GUARD_JSON}" \
                --lock-script "${LOCK_SCRIPT}"
            ;;
        status)
            python3 "${LOCK_SCRIPT}" "${action}" \
                --repo "${REPO_ROOT}" --review-file "${REVIEW_JSON}"
            ;;
        release)
            [[ "$#" -eq 0 ]] || return 2
            python3 "${WORKFLOW_SCRIPT}" release \
                --repo "${REPO_ROOT}" --review "${REVIEW_JSON}" \
                --lease "${LEASE_JSON}" --guard "${GUARD_JSON}" \
                --lock-script "${LOCK_SCRIPT}"
            ;;
        *)
            return 2
            ;;
    esac
}

publish() {
    local repo="$1"
    local review_id="$2"
    set_review_paths "${repo}" "${review_id}"

    python3 "${PUBLISH_SCRIPT}" publish \
        --repo "${REPO_ROOT}" \
        --review "${REVIEW_JSON}" \
        --event "${EVENT_JSON}" \
        --report "${LATEST_REPORT}" \
        --journal "${JOURNAL_JSON}" \
        --state-script "${STATE_SCRIPT}" \
        --lock-script "${LOCK_SCRIPT}" \
        --snapshot-script "${SNAPSHOT_SCRIPT}" \
        --lease "${LEASE_JSON}" \
        --guard "${GUARD_JSON}"
}

recover_publish() {
    local repo="$1"
    local review_id="$2"
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
        --lease "${LEASE_JSON}"
        --guard "${GUARD_JSON}"
    )
    python3 "${PUBLISH_SCRIPT}" "${arguments[@]}"
}

threads() {
    local repo="$1"
    local review_id="$2"
    local output="${3:-}"
    set_review_paths "${repo}" "${review_id}"
    validate_file "${REVIEW_JSON}" "${review_id}" >/dev/null
    if [[ "${output}" == "--json" ]]; then
        python3 "${STATE_SCRIPT}" threads --json < "${REVIEW_JSON}"
    else
        [[ -z "${output}" ]] || return 2
        python3 "${STATE_SCRIPT}" threads < "${REVIEW_JSON}"
    fi
}

abort_draft() {
    set_review_paths "$1" "$2"
    python3 "${WORKFLOW_SCRIPT}" abort-draft \
        --repo "${REPO_ROOT}" --review "${REVIEW_JSON}" \
        --lease "${LEASE_JSON}" --guard "${GUARD_JSON}" \
        --lock-script "${LOCK_SCRIPT}" --event "${EVENT_JSON}"
}

add_check() {
    local repo="$1"
    local review_id="$2"
    local result="$3"
    local check="$4"
    local basis="${5:-}"
    local provenance="${6:-}"
    local sanitized_result="${7:-}"
    local artifact_digest="${8:-}"
    set_review_paths "${repo}" "${review_id}"
    local arguments=(
        add-check --repo "${REPO_ROOT}" --review "${REVIEW_JSON}"
        --lease "${LEASE_JSON}" --guard "${GUARD_JSON}"
        --lock-script "${LOCK_SCRIPT}" --event "${EVENT_JSON}"
        --result "${result}" --check "${check}"
    )
    if [[ -n "${basis}" ]]; then
        arguments+=(
            --basis "${basis}" --provenance "${provenance}"
            --sanitized-result "${sanitized_result}"
        )
    fi
    if [[ -n "${artifact_digest}" ]]; then
        arguments+=(--artifact-digest "${artifact_digest}")
    fi
    python3 "${WORKFLOW_SCRIPT}" "${arguments[@]}"
}

add_gap() {
    local repo="$1"
    local review_id="$2"
    local check="$3"
    local reason="$4"
    local material="${5:-}"
    [[ -z "${material}" || "${material}" == "--material" ]] || return 2
    set_review_paths "${repo}" "${review_id}"
    local arguments=(
        add-gap --repo "${REPO_ROOT}" --review "${REVIEW_JSON}"
        --lease "${LEASE_JSON}" --guard "${GUARD_JSON}"
        --lock-script "${LOCK_SCRIPT}" --event "${EVENT_JSON}"
        --check "${check}" --reason "${reason}"
    )
    if [[ "${material}" == "--material" ]]; then
        arguments+=(--material)
    fi
    python3 "${WORKFLOW_SCRIPT}" "${arguments[@]}"
}

regenerate_report() {
    set_review_paths "$1" "$2"
    python3 "${WORKFLOW_SCRIPT}" verify \
        --repo "${REPO_ROOT}" --review "${REVIEW_JSON}" \
        --lease "${LEASE_JSON}" --guard "${GUARD_JSON}" \
        --lock-script "${LOCK_SCRIPT}" >/dev/null
    render_report "${REVIEW_JSON}" "${LATEST_REPORT}"
    printf '{"status":"report_regenerated"}\n'
}

wait_for_review() {
    set_review_paths "$1" "$2"
    python3 "${WORKFLOW_SCRIPT}" wait \
        --review "${REVIEW_JSON}" --timeout "${3:-300}"
}

publish_timeout() {
    local repo="$1"
    local review_id="$2"
    local condition="$3"
    [[ "${condition}" == "--if-eligible" ]] || return 2
    set_review_paths "${repo}" "${review_id}"
    local kind
    kind="$(python3 "${STATE_SCRIPT}" eligible-timeout < "${REVIEW_JSON}")"
    template "${REPO_ROOT}" "${review_id}" "${kind}"
    validate_event_file "${EVENT_JSON}" >/dev/null
    publish "${REPO_ROOT}" "${review_id}"
}

start_follow_up() {
    local repo="$1"
    local prior_id="$2"
    local name="$3"
    set_review_paths "${repo}" "${prior_id}"
    validate_file "${REVIEW_JSON}" "${prior_id}" >/dev/null
    python3 -c '
import json
import sys
document = json.load(open(sys.argv[1]))
if document["state"]["workflow"]["phase"] != "terminal":
    raise SystemExit("follow-up requires a terminal prior review")
' "${REVIEW_JSON}"
    init_review "${REPO_ROOT}" "${name}" "${prior_id}"
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
            [[ "$#" -eq 2 ]] || { usage >&2; return 2; }
            regenerate_report "$@"
            ;;
        inspect)
            [[ "$#" -ge 3 ]] || { usage >&2; return 2; }
            inspect "$@"
            ;;
        template)
            [[ "$#" -eq 3 ]] || { usage >&2; return 2; }
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
            [[ "$#" -eq 2 ]] || { usage >&2; return 2; }
            publish "$@"
            ;;
        recover-publish)
            [[ "$#" -eq 2 ]] || { usage >&2; return 2; }
            recover_publish "$@"
            ;;
        threads)
            [[ "$#" -ge 2 && "$#" -le 3 ]] || { usage >&2; return 2; }
            threads "$@"
            ;;
        abort-draft)
            [[ "$#" -eq 2 ]] || { usage >&2; return 2; }
            abort_draft "$@"
            ;;
        add-check)
            [[ "$#" -ge 4 && "$#" -le 8 ]] || { usage >&2; return 2; }
            add_check "$@"
            ;;
        add-gap)
            [[ "$#" -ge 4 && "$#" -le 5 ]] || { usage >&2; return 2; }
            add_gap "$@"
            ;;
        evidence-template)
            [[ "$#" -eq 1 ]] || { usage >&2; return 2; }
            python3 "${STATE_SCRIPT}" evidence-template "$1"
            ;;
        regenerate-report)
            [[ "$#" -eq 2 ]] || { usage >&2; return 2; }
            regenerate_report "$@"
            ;;
        wait)
            [[ "$#" -ge 2 && "$#" -le 3 ]] || { usage >&2; return 2; }
            wait_for_review "$@"
            ;;
        publish-timeout)
            [[ "$#" -eq 3 ]] || { usage >&2; return 2; }
            publish_timeout "$@"
            ;;
        start-follow-up)
            [[ "$#" -eq 3 ]] || { usage >&2; return 2; }
            start_follow_up "$@"
            ;;
        *)
            usage >&2
            return 2
            ;;
    esac
}

main "$@"
