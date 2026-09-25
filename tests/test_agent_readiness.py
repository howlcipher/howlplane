"""Provider-neutral agent readiness (`howlplane agents doctor`) against fake CLIs.

Each fake replays the output format observed from the real installed CLI
(Cursor's ANSI colors, Devin's family headers, AGY's "Fetching" banner, Claude's
JSON auth status and result envelope) and logs its argv, so the tests can prove
what the doctor did and did not invoke.
"""

import argparse
import json
from datetime import timedelta
from pathlib import Path

import pytest

from howlplane.control_plane import agent_readiness as readiness


pytestmark = pytest.mark.contract

AGENTS = readiness.AGENT_ORDER
ANSI_CYAN, ANSI_DIM, ANSI_RESET = r"\033[36m", r"\033[2m", r"\033[39m"
ENVELOPE = '{"type":"result","subtype":"success","is_error":false,"result":"%s","permission_denials":%s,"session_id":"s"}'
WINDOWS = ('{"type":"token_count","rate_limits":{"primary":{"used_percent":7,"window_minutes":300,"resets_at":1790000000},'
           '"secondary":{"used_percent":4,"window_minutes":10080}}}')

# first argument -> (exit code, stdout) for the level-1 probes each CLI supports.
LEVEL1 = {
    "codex": {"--version": (0, "codex-cli 0.156.1"),
              "login": (0, "Logged in using ChatGPT"),
              "debug": (0, '{"models":[{"slug":"gpt-6-luna"},{"slug":"gpt-5.5"}]}')},
    "claude_code": {"--version": (0, "2.1.282 (Claude Code)"),
                    "auth": (0, '{"loggedIn": true, "authMethod": "claude.ai", "email": "person@example.com", "orgId": "org-123"}')},
    "cursor": {"--version": (0, "2026.09.23-86fc751"),
               "status": (0, r"\033[32m✓ Logged in as person@example.com\033[39m"),
               "--list-models": (0, rf"{ANSI_DIM}Available models\033[22m\n\n{ANSI_CYAN}auto{ANSI_RESET} - Auto (default)\n"
                                    rf"{ANSI_CYAN}gpt-5.2{ANSI_RESET} {ANSI_DIM}- GPT-5.2\033[22m\n"
                                    rf"{ANSI_CYAN}composer-2.5{ANSI_RESET} {ANSI_DIM}- Composer 2.5\033[22m")},
    "agy": {"--version": (0, "1.2.10"),
            "models": (0, r"Fetching available models...\ngemini-3.8-flash-high\tGemini 3.8 Flash (High)\n"
                          r"claude-sonnet-4-6\tClaude Sonnet 4.6 (Thinking)")},
    "devin_cli": {"version": (0, "devin 3000.11.3 (9c803229faa4)"),
                  "auth": (0, r"Logged in (via Devin).\n\nUser:\n  Name:              Person Example"),
                  "models": (0, r"Available models (2 families)\n\nAdaptive (adaptive)\n"
                                r"  adaptive                 Adaptive  [\$0.5 / 1M Input]\n\n"
                                r"SWE-2 (swe-2)\n  aliases: swe\n  swe-2-high               SWE-2 High  [262K context, Free]")},
}
EXPECTED_MODELS = {"codex": ["gpt-6-luna", "gpt-5.5"], "claude_code": [], "cursor": ["gpt-5.2", "composer-2.5"],
                   "agy": ["gemini-3.8-flash-high", "claude-sonnet-4-6"], "devin_cli": ["adaptive", "swe-2-high"]}
SMOKE_ARG = {"codex": "exec", "claude_code": "-p", "cursor": "-p", "agy": "-p", "devin_cli": "-p"}


def smoke_pass(agent, extra=""):
    if agent == "claude_code":
        return f"printf '%s\\n' '{ENVELOPE % (readiness.SMOKE_TOKEN, '[]')}'"
    return f"printf '%s\\n' {extra and repr(extra)} 'model: fake-model-1' '{readiness.SMOKE_TOKEN}'"


DENIED_ENVELOPE = ENVELOPE % ("I need approval to continue", '[{"tool_name":"Bash"}]')


def smoke_permission(agent):
    if agent == "claude_code":
        return f"printf '%s\\n' '{DENIED_ENVELOPE}'"
    return "echo 'Error: this action requires approval' >&2; exit 1"


SMOKES = {
    "pass": smoke_pass,
    "permission": smoke_permission,
    "failure": lambda agent: "echo 'unexpected internal failure' >&2; exit 1",
    "quota": lambda agent: "echo 'Error: quota exhausted for this account' >&2; exit 1",
    "rate": lambda agent: "echo 'HTTP 429 Too Many Requests' >&2; exit 1",
    "hang": lambda agent: "sleep 5",
    "untrusted": lambda agent: ("echo 'Error: Refusing to run in an untrusted workspace: /tmp/x' >&2; exit 1"
                                if agent != "cursor" else
                                "printf '%s\\n' '⚠ Workspace Trust Required' 'Pass --trust, --yolo, or -f if you trust this directory' >&2; exit 1"),
}


def install(tmp_path, monkeypatch, agents=AGENTS, smoke="pass", overrides=None):
    """Put fake CLIs for `agents` alone on PATH; return the argv log path."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    log = tmp_path / "argv.log"
    # Only the fakes and the base system tools (sleep) are reachable.
    monkeypatch.setenv("PATH", f"{bin_dir}:/usr/bin:/bin")
    monkeypatch.setenv("FAKE_LOG", str(log))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex-home"))
    monkeypatch.setattr(readiness, "hosted_probes_allowed", lambda: True)
    for agent in agents:
        branches = dict(LEVEL1[agent])
        branches.update((overrides or {}).get(agent, {}))
        cases = "\n".join(f"  {arg}) printf '{out}\\n'; exit {code} ;;" for arg, (code, out) in branches.items())
        body = smoke if smoke not in SMOKES else SMOKES[smoke](agent)
        script = bin_dir / readiness.SPECS[agent].binary
        script.write_text(f'#!/bin/sh\necho "$*" >> "$FAKE_LOG"\ncase "$1" in\n{cases}\n  {SMOKE_ARG[agent]}) {body} ;;\n'
                          f'  *) echo "unexpected: $*" >&2; exit 2 ;;\nesac\n')
        script.chmod(0o700)
    return log


def invocations(log):
    return log.read_text().splitlines() if log.exists() else []


def smokes(log):
    return [line for line in invocations(log) if readiness.SMOKE_TOKEN in line]


def by_agent(summaries):
    return {summary["agent"]: summary for summary in summaries}


# Level 1: local, free


@pytest.mark.parametrize("agent", AGENTS)
def test_present_cli_reports_version_auth_and_models_without_sending_a_prompt(tmp_path, monkeypatch, agent):
    log = install(tmp_path, monkeypatch, agents=[agent])
    summary = by_agent(readiness.evaluate([agent]))[agent]
    assert summary["installed"] and summary["executable"].endswith(readiness.SPECS[agent].binary)
    assert summary["version"] in {"0.156.1", "2.1.282", "2026.09.23-86fc751", "1.2.10", "3000.11.3"}
    assert summary["authenticated"] is True
    assert summary["models"]["items"] == EXPECTED_MODELS[agent]
    assert summary["models"]["status"] == ("UNSUPPORTED" if agent == "claude_code" else "LISTED")
    assert summary["unattended_execution"] is None
    assert summary["live_smoke"]["status"] == "NOT_RUN"
    assert summary["capacity"]["state"] == "UNKNOWN" and summary["capacity"]["visibility"] == "UNKNOWN"
    assert smokes(log) == []


@pytest.mark.parametrize("agent", AGENTS)
def test_missing_cli_is_reported_and_never_invoked(tmp_path, monkeypatch, agent):
    log = install(tmp_path, monkeypatch, agents=[])
    summary = by_agent(readiness.evaluate([agent], live=True))[agent]
    assert not summary["installed"] and summary["authenticated"] is None
    assert summary["models"]["status"] == "NOT_INSTALLED"
    assert summary["live_smoke"]["status"] == "NOT_RUN"
    assert invocations(log) == []


NOT_LOGGED_IN = {"codex": {"login": (1, "Not logged in")}, "claude_code": {"auth": (1, '{"loggedIn": false}')},
                 "cursor": {"status": (1, "Not logged in")}, "agy": {"models": (1, "Error: please log in first")},
                 "devin_cli": {"auth": (1, "Not logged in. Run devin auth login")}}
INCONCLUSIVE = {"codex": {"login": (3, "???")}, "claude_code": {"auth": (1, "unexpected")},
                "cursor": {"status": (2, "")}, "agy": {"models": (1, "internal error")},
                "devin_cli": {"auth": (4, "")}}


@pytest.mark.parametrize("agent", AGENTS)
def test_logged_out_cli_is_not_authenticated_and_gets_no_smoke(tmp_path, monkeypatch, agent):
    log = install(tmp_path, monkeypatch, agents=[agent], overrides={agent: NOT_LOGGED_IN[agent]})
    summary = by_agent(readiness.evaluate([agent], live=True))[agent]
    assert summary["authenticated"] is False
    assert smokes(log) == []


@pytest.mark.parametrize("agent", AGENTS)
def test_inconclusive_auth_stays_unknown(tmp_path, monkeypatch, agent):
    install(tmp_path, monkeypatch, agents=[agent], overrides={agent: INCONCLUSIVE[agent]})
    assert by_agent(readiness.evaluate([agent]))[agent]["authenticated"] is None


@pytest.mark.parametrize("agent", [agent for agent in AGENTS if readiness.SPECS[agent].models_argv])
def test_model_listing_failure_is_reported_not_guessed(tmp_path, monkeypatch, agent):
    arg = readiness.SPECS[agent].models_argv[1]
    install(tmp_path, monkeypatch, agents=[agent], overrides={agent: {arg: (1, "listing unavailable")}})
    models = by_agent(readiness.evaluate([agent]))[agent]["models"]
    assert (models["status"], models["items"]) == ("FAILED", [])


def test_codex_default_model_comes_from_its_config(tmp_path, monkeypatch):
    install(tmp_path, monkeypatch, agents=["codex"])
    assert by_agent(readiness.evaluate(["codex"]))["codex"]["models"]["default"] is None
    home = tmp_path / "codex-home"
    home.mkdir()
    (home / "config.toml").write_text('model = "gpt-6-luna"\n')
    models = by_agent(readiness.evaluate(["codex"], refresh=True))["codex"]["models"]
    assert (models["default"], models["default_source"]) == ("gpt-6-luna", "codex config.toml")


# Level 2: bounded live smoke


@pytest.mark.parametrize("agent", AGENTS)
def test_live_smoke_pass_verifies_unattended_execution(tmp_path, monkeypatch, agent):
    log = install(tmp_path, monkeypatch, agents=[agent])
    summary = by_agent(readiness.evaluate([agent], live=True, smoke_timeout=10))[agent]
    assert summary["live_smoke"]["status"] == "PASS"
    assert summary["live_smoke"]["exit_code"] == 0 and summary["live_smoke"]["latency_seconds"] >= 0
    assert (summary["unattended_execution"], summary["unattended_source"]) == (True, "live smoke")
    assert len(smokes(log)) == 1
    if agent != "claude_code":
        assert summary["live_smoke"]["model_reported"] == "fake-model-1"


@pytest.mark.parametrize("agent", AGENTS)
def test_permission_prompt_marks_the_cli_interactive_only(tmp_path, monkeypatch, agent):
    install(tmp_path, monkeypatch, agents=[agent], smoke="permission")
    summary = by_agent(readiness.evaluate([agent], live=True, smoke_timeout=10))[agent]
    assert summary["live_smoke"]["status"] == "BLOCKED_PERMISSION"
    assert summary["unattended_execution"] is False
    assert "BLOCKED — permission required" in readiness.render([summary])
    assert "INTERACTIVE_ONLY" in readiness.render([summary])


@pytest.mark.parametrize("agent", AGENTS)
def test_generic_smoke_failure_is_failed_without_capacity_claim(tmp_path, monkeypatch, agent):
    install(tmp_path, monkeypatch, agents=[agent], smoke="failure")
    summary = by_agent(readiness.evaluate([agent], live=True, smoke_timeout=10))[agent]
    assert (summary["live_smoke"]["status"], summary["live_smoke"]["failure_class"]) == ("FAIL", "ENGINEERING_FAILURE")
    assert summary["unattended_execution"] is None
    assert summary["capacity"]["state"] == "UNKNOWN" and summary["capacity"]["limits"] == []


@pytest.mark.parametrize("agent", AGENTS)
def test_local_smoke_timeout_is_a_timeout_never_exhaustion(tmp_path, monkeypatch, agent):
    install(tmp_path, monkeypatch, agents=[agent], smoke="hang")
    summary = by_agent(readiness.evaluate([agent], live=True, smoke_timeout=1))[agent]
    assert summary["live_smoke"]["status"] == "TIMED_OUT"
    assert summary["live_smoke"]["failure_class"] == "EXECUTION_BUDGET_EXCEEDED"
    assert summary["capacity"]["state"] == "UNKNOWN" and summary["capacity"]["limits"] == []
    assert "QUOTA" not in json.dumps(summary) and "EXHAUSTED" not in json.dumps(summary)


@pytest.mark.parametrize("agent", AGENTS)
@pytest.mark.parametrize("smoke,state", [("quota", "QUOTA_EXHAUSTED"), ("rate", "RATE_LIMITED")])
def test_provider_limits_are_recorded_with_scope_and_expiry(tmp_path, monkeypatch, agent, smoke, state):
    install(tmp_path, monkeypatch, agents=[agent], smoke=smoke)
    summary = by_agent(readiness.evaluate([agent], live=True, smoke_timeout=10))[agent]
    # No model was reported, so the evidence is agent-wide -- and it expires.
    assert summary["capacity"]["state"] == state
    [limit] = summary["capacity"]["limits"]
    assert (limit["state"], limit["scope"], limit["source"]) == (state, "agent", "live smoke")
    expires = readiness.parse_time(limit["expires_at"])
    assert expires - readiness.parse_time(limit["observed_at"]) == timedelta(seconds=readiness.LIMIT_SECONDS[state])
    assert readiness.factory_readiness([summary])["status"] == "BLOCKED"


def test_model_scoped_limit_does_not_blacklist_the_agent():
    readiness.record_session_outcome("agy", "gemini-3.8-flash-high", "QUOTA_EXHAUSTED")
    summary = readiness.cached_summaries()["agy"]
    assert summary["capacity"]["state"] == "UNKNOWN"
    assert [(item["scope"], item["model"]) for item in summary["capacity"]["limits"]] == [("model", "gemini-3.8-flash-high")]


def test_expired_limits_are_dropped_rather_than_permanent():
    readiness.record_session_outcome("codex", "UNKNOWN", "SESSION_LIMIT")
    later = readiness.now() + timedelta(seconds=readiness.LIMIT_SECONDS["SESSION_EXHAUSTED"] + 1)
    cache = readiness.load_cache()
    assert readiness.summarize("codex", cache["codex"])["capacity"]["state"] == "SESSION_EXHAUSTED"
    assert readiness.summarize("codex", cache["codex"], later)["capacity"]["state"] == "UNKNOWN"


# Level 3: capacity only as reported


def test_capacity_windows_are_populated_only_from_structured_cli_output(tmp_path, monkeypatch):
    install(tmp_path, monkeypatch, agents=["codex"], smoke=smoke_pass("codex", WINDOWS))
    capacity = by_agent(readiness.evaluate(["codex"], live=True, smoke_timeout=10))["codex"]["capacity"]
    assert (capacity["visibility"], capacity["source"], capacity["state"]) == ("VISIBLE", "cli", "AVAILABLE")
    assert (capacity["five_hour_remaining_percent"], capacity["weekly_remaining_percent"]) == (93.0, 96.0)
    assert capacity["reset_at"].startswith("2026-09-")


@pytest.mark.parametrize("agent", AGENTS)
def test_unexposed_capacity_stays_unknown_with_no_fabricated_numbers(tmp_path, monkeypatch, agent):
    install(tmp_path, monkeypatch, agents=[agent])
    capacity = by_agent(readiness.evaluate([agent], live=True, smoke_timeout=10))[agent]["capacity"]
    assert capacity["state"] == "UNKNOWN" and capacity["visibility"] == "UNKNOWN"
    assert capacity["five_hour_remaining_percent"] is None and capacity["weekly_remaining_percent"] is None
    assert capacity["reset_at"] is None
    assert "UNKNOWN — CLI does not expose remaining allowance" in readiness.render(
        [by_agent(readiness.evaluate([agent]))[agent]])


# Doctor contracts


def test_zero_cost_doctor_never_invokes_a_model(tmp_path, monkeypatch):
    log = install(tmp_path, monkeypatch)
    readiness.evaluate(refresh=True)
    assert invocations(log) and smokes(log) == []


def test_live_doctor_runs_at_most_one_smoke_per_eligible_agent_and_reuses_a_fresh_pass(tmp_path, monkeypatch):
    log = install(tmp_path, monkeypatch)
    readiness.evaluate(live=True, smoke_timeout=10)
    first = smokes(log)
    assert sorted(line.split()[0] for line in first) == sorted(["exec", "-p", "-p", "-p", "-p"])
    readiness.evaluate(live=True, smoke_timeout=10)
    assert smokes(log) == first, "a fresh PASS must satisfy readiness without another call"


def test_stale_cache_refreshes_and_refresh_forces_a_probe(tmp_path, monkeypatch):
    log = install(tmp_path, monkeypatch, agents=["agy"])
    readiness.evaluate(["agy"], live=True, smoke_timeout=10)
    cache = readiness.load_cache()
    stale = readiness.iso(readiness.now() - timedelta(seconds=readiness.TTL_SECONDS["live_smoke"] + 5))
    cache["agy"]["live_smoke"]["observed_at"] = stale
    readiness.save_cache(cache)
    assert readiness.cached_summaries()["agy"]["live_smoke"]["status"] == "STALE"
    readiness.evaluate(["agy"], live=True, smoke_timeout=10)
    assert len(smokes(log)) == 2
    probes = len(invocations(log))
    readiness.evaluate(["agy"], refresh=True)
    assert len(invocations(log)) > probes and len(smokes(log)) == 2


def test_cached_routing_view_never_probes(tmp_path, monkeypatch):
    log = install(tmp_path, monkeypatch)
    assert readiness.cached_summaries() == {}
    assert invocations(log) == []


def test_secrets_and_identities_are_redacted_from_cache_and_output(tmp_path, monkeypatch, capsys):
    install(tmp_path, monkeypatch, overrides={"codex": {"debug": (1, "error token=sk-abcdefghijklmnop for person@example.com")}})
    args = argparse.Namespace(agent=None, live=False, refresh=True, smoke_timeout=10, json=True)
    assert readiness.command(args) == 0
    output = capsys.readouterr().out
    stored = Path(readiness.cache_path()).read_text()
    for secret in ("person@example.com", "org-123", "Person Example", "sk-abcdefghijklmnop"):
        assert secret not in output and secret not in stored
    assert oct(Path(readiness.cache_path()).stat().st_mode & 0o777) == "0o600"


def test_json_output_is_valid_and_schema_tagged(tmp_path, monkeypatch, capsys):
    install(tmp_path, monkeypatch)
    args = argparse.Namespace(agent=["agy", "codex"], live=False, refresh=False, smoke_timeout=10, json=True)
    assert readiness.command(args) == 0
    document = json.loads(capsys.readouterr().out)
    assert document["schema"] == readiness.SCHEMA
    assert [item["agent"] for item in document["agents"]] == ["codex", "agy"]
    for item in document["agents"]:
        assert {"installed", "authenticated", "unattended_execution", "live_smoke", "models", "capacity"} <= item.keys()


def test_human_report_names_the_cursor_backend_and_unknown_capacity(tmp_path, monkeypatch, capsys):
    install(tmp_path, monkeypatch)
    args = argparse.Namespace(agent=None, live=False, refresh=False, smoke_timeout=10, json=False)
    assert readiness.command(args) == 0
    text = capsys.readouterr().out
    assert text.startswith("HOWLPLANE AGENT DOCTOR")
    cursor = text.split("\nCursor\n")[1].split("\n\n")[0]
    assert "Backend:        agent" in cursor and "CLI:            AVAILABLE" in cursor
    assert text.count("Capacity:       UNKNOWN — CLI does not expose remaining allowance") == 5


# Factory preflight


def summary(name, **changes):
    base = {"agent": name, "name": name, "installed": True, "authenticated": True, "unattended_execution": True,
            "mutation_capable": True, "live_smoke": {"status": "PASS"},
            "capacity": {"state": "UNKNOWN", "limits": []}}
    base.update(changes)
    return base


def test_factory_readiness_requires_an_autonomous_implementer_and_an_independent_auditor():
    ready = readiness.factory_readiness([summary("Codex"), summary("AGY")])
    assert ready["status"] == "READY" and ready["independent_audit_available"]
    assert ready["capacity_unknown"] == ["Codex", "AGY"], "UNKNOWN capacity is reported, not treated as unavailable"

    solo = readiness.factory_readiness([summary("Codex"), summary("Claude", unattended_execution=False)])
    assert solo["status"] == "DEGRADED" and not solo["independent_audit_available"]
    assert solo["interactive_only"] == ["Claude"]

    unverified = readiness.factory_readiness([summary("Codex"), summary("Devin", unattended_execution=None)])
    assert unverified["status"] == "DEGRADED" and unverified["unverified"] == ["Devin"]
    assert "Devin" in unverified["implementation_capable"], "unverified is not unavailable"

    blocked = readiness.factory_readiness([
        summary("Codex", capacity={"state": "SESSION_EXHAUSTED", "limits": []}),
        summary("Cursor", installed=False), summary("Claude", unattended_execution=False)])
    assert blocked["status"] == "BLOCKED" and blocked["unavailable"] == ["Cursor"]


def factory_args(**values):
    base = dict(state_dir=None, target_repo=None, live=False, json=True, preflight="require")
    base.update(values)
    return argparse.Namespace(**base)


def test_factory_doctor_json_reports_readiness_and_fails_when_blocked(tmp_path, monkeypatch, capsys):
    from howlplane.control_plane import cli
    install(tmp_path, monkeypatch, agents=[])
    assert cli.cmd_factory_doctor(factory_args()) == 1
    report = json.loads(capsys.readouterr().out)
    assert report["readiness"]["status"] == "BLOCKED"
    assert report["execution_budget"]["implementation"] == 300

    install(tmp_path, monkeypatch)
    assert cli.cmd_factory_doctor(factory_args()) == 0
    report = json.loads(capsys.readouterr().out)
    # Nothing has proven unattended execution yet: usable, but not READY.
    assert report["readiness"]["status"] == "DEGRADED"
    assert set(report["readiness"]["unverified"]) == {"Codex", "Claude Code", "Cursor", "AGY", "Devin"}


def test_required_preflight_refuses_to_start_a_blocked_factory(tmp_path, monkeypatch, capsys):
    from howlplane.control_plane import cli
    install(tmp_path, monkeypatch, agents=[])
    monkeypatch.setattr(cli, "_resolve_factory_campaign", lambda *a, **k: pytest.fail("campaign must not start"))
    assert cli.cmd_factory_run(factory_args()) == 1
    assert cli.cmd_factory_start(factory_args()) == 1
    assert "FACTORY READINESS: BLOCKED" in capsys.readouterr().err


def test_preflight_off_does_not_probe(tmp_path, monkeypatch):
    from howlplane.control_plane import cli
    log = install(tmp_path, monkeypatch)
    assert cli._factory_preflight(factory_args(preflight="off")) is None
    assert invocations(log) == []


def test_auto_routing_pins_listed_models_only_where_it_always_did(tmp_path, monkeypatch):
    from howlplane.control_plane import orchestration
    install(tmp_path, monkeypatch)
    routed = {agent: orchestration.discover_models(agent) for agent in AGENTS}
    # Codex, Claude, and Cursor run on the CLI's own default; the list is reported, not pinned.
    assert routed == {"codex": [], "claude_code": [], "cursor": [],
                      "agy": EXPECTED_MODELS["agy"], "devin_cli": EXPECTED_MODELS["devin_cli"]}


def test_local_only_mode_sends_no_hosted_probe_and_refuses_live(tmp_path, monkeypatch, capsys):
    log = install(tmp_path, monkeypatch)
    monkeypatch.setattr(readiness, "hosted_probes_allowed", lambda: False)
    args = argparse.Namespace(agent=None, live=True, refresh=True, smoke_timeout=10, json=True)
    assert readiness.command(args) == 0
    captured = capsys.readouterr()
    assert "local_only" in captured.err
    # Only `--version` (a local call) ran; no auth, model listing, or smoke.
    assert invocations(log) and all(line in {"--version", "version"} for line in invocations(log))
    for item in json.loads(captured.out)["agents"]:
        assert item["installed"] and item["authenticated"] is None
        assert item["models"]["status"] == "NOT_PROBED" and item["live_smoke"]["status"] == "NOT_RUN"


@pytest.mark.parametrize("agent", ["cursor", "devin_cli"])
def test_untrusted_smoke_workspace_is_not_agent_wide_interactive_only(tmp_path, monkeypatch, agent):
    """Observed live: Cursor and Devin refuse the smoke's fresh temporary directory until it is trusted."""
    install(tmp_path, monkeypatch, agents=[agent], smoke="untrusted")
    summary = by_agent(readiness.evaluate([agent], live=True, smoke_timeout=10))[agent]
    assert summary["live_smoke"]["status"] == "WORKSPACE_TRUST_REQUIRED"
    assert summary["live_smoke"]["failure_class"] == "EXECUTION_PERMISSION_REQUIRED"
    assert "untrusted workspace" in summary["live_smoke"]["error"].lower() or "trust" in summary["live_smoke"]["error"].lower()
    assert summary["unattended_execution"] is None, "a directory's trust state must not mark the agent interactive-only"
    assert "workspace trust required" in readiness.render([summary])
    assert readiness.factory_readiness([summary])["workspace_trust_required"] == [summary["name"]]


def test_orchestrate_trust_refusal_is_not_fed_back_as_agent_capability():
    readiness.record_session_outcome("devin_cli", "m1", "EXECUTION_PERMISSION_REQUIRED",
                                     detail="Error: Refusing to run in an untrusted workspace: /repo")
    assert "devin_cli" not in readiness.load_cache()
    readiness.record_session_outcome("devin_cli", "m1", "EXECUTION_PERMISSION_REQUIRED", detail="requires approval")
    assert readiness.cached_summaries()["devin_cli"]["unattended_execution"] is False
