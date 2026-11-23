#!/bin/sh
# Deterministic fake of the HowlPlane CLI boundary for TUI-00 probes.
# It never contacts an AI provider. It mirrors the argv shapes of the real
# commands (providers --json, route OBJECTIVE --json, work OBJECTIVE --execute
# --json --progress never) and answers with fixtures captured from HowlPlane.
#
# HowlFrame exec children start with an empty environment and no PATH, so this
# script uses shell builtins only. The scenario is read from the file
# "scenario" beside the script; every invocation's argv is appended to
# "invocations.log" so tests can prove what crossed the boundary.
here=${0%/*}
fixtures="$here/fixtures"
scenario=ok
if [ -f "$here/scenario" ]; then
    IFS= read -r scenario < "$here/scenario"
fi
for arg in "$@"; do
    printf '[%s]' "$arg" >> "$here/invocations.log"
done
printf '\n' >> "$here/invocations.log"

emit() {
    while IFS= read -r line || [ -n "$line" ]; do
        printf '%s\n' "$line"
    done < "$1"
}

case "$scenario" in
    stderr_noise)
        # The real CLI writes progress and errors to stderr; exec merges streams.
        printf '[HowlPlane] ROUTING\n' >&2
        ;;
    fail)
        emit "$fixtures/error_not_a_repo.json" >&2
        exit 1
        ;;
    malformed)
        printf '{"schema": "howlplane.ai_resources/v1", "resources": [\n'
        exit 0
        ;;
    hang)
        while :; do :; done
        ;;
    flood)
        while :; do printf 'xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx\n'; done
        ;;
esac

case "$1" in
    providers)
        if [ "$scenario" = "provider_down" ]; then
            emit "$fixtures/ai_resources_codex_missing.json"
        else
            emit "$fixtures/ai_resources_codex_ready.json"
        fi
        ;;
    route)
        if [ "$scenario" = "provider_down" ]; then
            emit "$fixtures/route_blocked_local_only.json"
            exit 3
        fi
        emit "$fixtures/route_selected_unprobed.json"
        ;;
    work)
        emit "$fixtures/work_result_complete.json"
        ;;
    *)
        printf '{"schema": "howlplane.error/v1", "code": "FAKE_UNKNOWN_COMMAND", "message": "unknown fake command", "why": "The fake boundary only implements providers, route and work.", "next_action": "Use a supported command."}\n' >&2
        exit 2
        ;;
esac
