"""A failed verification is a finding for the implementer, not an immediate handoff (DOG-026).

Run-032: `npm test` failed one case after implementation, the session stopped at
HANDOFF REQUIRED without rework, the stored output was the last 1000 characters
(TAP prints the failing case long before the summary), and the report said
`Failures: []`. These tests pin the class: any failing verification command
drives the bounded rework loop with failure-first evidence, a persistent failure
hands off with the reason and output, and a command that cannot start or never
finishes is reported instead of crashing the session.
"""

import pytest

from howlplane.control_plane import orchestration as module
from tests.test_orchestration import arguments, repository
from tests.test_orchestration_capability_recovery import accepted, install_all
from tests.test_orchestration_handoff_recovery import only


pytestmark = pytest.mark.contract


def session(tmp_path, monkeypatch, verify, fixed_on_attempt=None):
    """Run a PLAN + EXECUTE + AUDIT session whose implementation writes README, fixed from attempt N on."""
    repo = repository(tmp_path)
    install_all(tmp_path, monkeypatch, models=("m1",))
    implementations = []

    def execute(doc, role, agent, model, cwd):
        if role == "implementation":
            implementations.append(dict(doc.get("rework") or {}))
            fixed = fixed_on_attempt is not None and len(implementations) >= fixed_on_attempt
            (cwd / "README").write_text(f"{'FIXED' if fixed else 'BROKEN'} attempt {len(implementations)}\n")
        return accepted(agent, role)

    monkeypatch.setattr(module, "execute_assignment", execute)
    args = arguments(repo, input="Fix the README", orchestrator="codex", policy="PLAN + EXECUTE + INDEPENDENT AUDIT",
                     verify=verify(repo), **only("codex", "claude_code"))
    return repo, args, implementations


def failing_check(repo):
    script = repo / "check.sh"
    # Like TAP: the failing case comes first, then a long passing tail that used to push it out.
    script.write_text("#!/bin/sh\nif grep -q FIXED README; then echo ok; exit 0; fi\n"
                      "echo 'not ok 18 - CLI rejects invalid outDir'\necho '  error: 0 !== 2'\n"
                      "i=0; while [ $i -lt 400 ]; do echo \"ok $i - passing case\"; i=$((i+1)); done\nexit 1\n")
    script.chmod(0o755)
    return [str(script)]


def test_failed_verification_goes_back_to_implementation_with_the_failing_case(tmp_path, monkeypatch, capsys):
    repo, args, implementations = session(tmp_path, monkeypatch, failing_check, fixed_on_attempt=2)

    assert module.command(args) == 0
    out, err = capsys.readouterr()
    assert "Status: COMPLETE" in out
    assert "Rework rounds: 1 of 2" in out
    assert "REWORK" in err and "failed verification back to implementation" in err
    assert len(implementations) == 2
    rework = implementations[1]
    assert rework["source"] == "verification"
    assert "check.sh` on the implemented tree and it exited 1" in rework["findings"]
    assert "not ok 18 - CLI rejects invalid outDir" in rework["findings"]
    assert "0 !== 2" in rework["findings"]


def test_persistent_failure_hands_off_with_reason_output_and_next_step(tmp_path, monkeypatch, capsys):
    repo, args, implementations = session(tmp_path, monkeypatch, failing_check)

    assert module.command(args) == 2
    out, err = capsys.readouterr()
    assert len(implementations) == 1 + module.MAX_REWORK_ROUNDS
    assert "Status: HANDOFF REQUIRED" in out and "Resumable: yes" in out
    assert f"exited 1 after {module.MAX_REWORK_ROUNDS} of {module.MAX_REWORK_ROUNDS} rework round(s)" in out
    assert "Blocked by: Configured validation failed" in out
    assert "not ok 18 - CLI rejects invalid outDir" in out
    assert "howlplane orchestrate resume --repo" in out
    assert "HANDOFF REQUIRED Configured validation failed:" in err

    assert module.command(arguments(repo, input="inspect", json=False)) == 0
    assert "Blocked by: Configured validation failed" in capsys.readouterr().out

    # The rework budget is spent: resuming an unchanged repository re-runs the check and stops again, as the report says.
    assert module.command(arguments(repo, input="resume", orchestrator=None)) == 2
    assert "Blocked by: Configured validation failed" in capsys.readouterr().out
    assert len(implementations) == 1 + module.MAX_REWORK_ROUNDS

    # The user fixes it; resume re-runs the check on the fix and finishes without another implementation.
    (repo / "README").write_text("FIXED by the user\n")
    assert module.command(arguments(repo, input="resume", orchestrator=None)) == 0
    out = capsys.readouterr().out
    assert "Status: COMPLETE" in out and "Blocked by" not in out
    assert len(implementations) == 1 + module.MAX_REWORK_ROUNDS


@pytest.mark.parametrize("verify, reason", [
    (lambda repo: [str(repo / "no-such-test-runner")], "Blocked by: Configured validation could not run"),
    (lambda repo: ["sleep", "5"], "`sleep 5` exited 124"),
])
def test_verification_that_cannot_run_or_finish_is_reported_not_raised(tmp_path, monkeypatch, capsys, verify, reason):
    monkeypatch.setattr(module, "VERIFY_TIMEOUT_SECONDS", 0.5)
    repo, args, implementations = session(tmp_path, monkeypatch, verify)

    assert module.command(args) == 2
    out, _ = capsys.readouterr()
    assert "Status: HANDOFF REQUIRED" in out
    assert reason in out
    # A missing runner is not the implementer's to fix; a hang may be, within the rework budget.
    assert len(implementations) == (1 if "could not run" in reason else 1 + module.MAX_REWORK_ROUNDS)


def test_test_output_excerpt_keeps_failures_and_the_summary():
    short = "ok 1\nok 2\n"
    assert module.test_output_excerpt(short) == short

    long = "\n".join(["ok %d - fine" % i for i in range(30)] + ["not ok 31 - breaks", "  error: boom"]
                     + ["ok %d - fine" % i for i in range(32, 600)] + ["# pass 598", "# fail 1"])
    excerpt = module.test_output_excerpt(long)
    assert len(excerpt) <= module.TEST_OUTPUT_CHARS + 200
    assert "not ok 31 - breaks" in excerpt and "error: boom" in excerpt
    assert excerpt.rstrip().endswith("# fail 1")

    no_failure_lines = "\n".join("ok %d - fine" % i for i in range(600))
    assert module.test_output_excerpt(no_failure_lines).endswith("ok 599 - fine")


def parsed_verify(*argv):
    from howlplane.control_plane.cli import build_parser

    return build_parser().parse_args(["orchestrate", "Fix the README", *argv]).verify


def test_quoted_verification_command_keeps_its_options_and_runs_split(tmp_path, monkeypatch, capsys):
    """DOG-035: `--verify python3 -m unittest` (howl's documented example) died in argparse on `-m`."""
    quoted = parsed_verify("--verify", "sh -c 'grep -q FIXED README'")
    assert quoted == ["sh -c 'grep -q FIXED README'"]
    repo, args, implementations = session(tmp_path, monkeypatch, lambda repo: quoted, fixed_on_attempt=2)

    assert module.command(args) == 0
    out, err = capsys.readouterr()
    # The first tree fails the real grep and is reworked; a command run unsplit could not have started at all.
    assert "Status: COMPLETE" in out and "Rework rounds: 1 of 2" in out
    assert "`sh -c 'grep -q FIXED README'` on the implemented tree and it exited 1" in implementations[1]["findings"]


@pytest.mark.parametrize("value, expected", [
    (None, None),
    (["pytest"], ["pytest"]),
    (["bash", "scripts/test.sh"], ["bash", "scripts/test.sh"]),
    (["python3 -m pytest -q"], ["python3", "-m", "pytest", "-q"]),
    (["'/opt/my tests/run.sh' --fast"], ["/opt/my tests/run.sh", "--fast"]),
])
def test_verification_command_splits_only_a_single_quoted_command(value, expected):
    assert module.verification_command(value) == expected


def test_verification_command_rejects_a_blank_command():
    with pytest.raises(ValueError, match="--verify needs a command"):
        module.verification_command(["   "])


def test_unquoted_verification_options_fail_with_a_quoting_hint(capsys):
    from howlplane.control_plane.cli import main

    with pytest.raises(SystemExit) as stop:
        main(["orchestrate", "Goal", "--verify", "python3", "-m", "unittest"])
    assert stop.value.code == 2
    err = capsys.readouterr().err
    assert "unrecognized arguments: -m unittest" in err
    assert 'quote a verification command that has options, for example --verify "python3 -m pytest -q"' in err

    with pytest.raises(SystemExit):
        main(["orchestrate", "Goal", "--no-such-flag"])
    assert "quote a verification command" not in capsys.readouterr().err
