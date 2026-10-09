"""Worker-declared status is evidence (howl-cubs-dogfood R002).

DOG-042: Codex ended three implementation attempts with "IMPLEMENTATION_STATUS: INCOMPLETE" and a
reason; each was recorded SUCCEEDED and sent to review, which spent both rework rounds rediscovering
what the implementer had already said.
"""

import pytest

from howlplane.control_plane import orchestration as module
from tests.test_orchestration import arguments, repository
from tests.test_orchestration_capability_recovery import accepted, install_all
from tests.test_orchestration_handoff_recovery import only


pytestmark = pytest.mark.contract

INCOMPLETE = ("**IMPLEMENTATION_STATUS: INCOMPLETE.** I did not fetch the data: the repository documents a "
              "usage restriction and no authorization was given.")


def run(tmp_path, monkeypatch, incomplete_agents, **overrides):
    repo = repository(tmp_path)
    install_all(tmp_path, monkeypatch, models=("m1",))
    assigned = []

    def execute(doc, role, agent, model, cwd):
        assigned.append((role, agent))
        outcome = accepted(agent, role)
        if role == "implementation":
            (cwd / "README").write_text(f"partial by {agent}\n" if agent in incomplete_agents else "done\n")
            if agent in incomplete_agents:
                outcome.stdout = "Wrote a skeleton.\n" + INCOMPLETE
        return outcome

    monkeypatch.setattr(module, "execute_assignment", execute)
    values = dict(input="Fetch and analyze public data", orchestrator="codex",
                  policy="PLAN + EXECUTE + INDEPENDENT AUDIT", verify=["true"])
    values.update(overrides)
    code = module.command(arguments(repo, **values))
    doc = module.active_sessions(module.state_root(), repo, include_terminal=True)[0]
    return code, assigned, doc


def test_declared_incomplete_is_not_success_and_the_next_implementer_continues(tmp_path, monkeypatch, capsys):
    code, assigned, doc = run(tmp_path, monkeypatch, {"codex"}, **only("codex", "cursor", "claude_code"))
    out, err = capsys.readouterr()

    first = next(a for a in doc["attempts"] if a["stage"] == "implementation")
    assert (first["agent"], first["state"], first["failure"]) == ("codex", "REVOKED", "IMPLEMENTATION_INCOMPLETE")
    assert first["partial_changes"] is True and "no authorization was given" in first["verdict_excerpt"]
    assert "reported it could not finish; partial changes kept" in err
    # The work was not reviewed as Codex's: another implementer finished it first.
    implementers = [agent for role, agent in assigned if role == "implementation"]
    assert implementers[0] == "codex" and implementers[1] != "codex"
    assert assigned.index(("implementation", implementers[1])) < next(
        i for i, (role, _) in enumerate(assigned) if role == "review")
    assert code == 0 and "Status: COMPLETE" in out


def test_when_every_implementer_declares_incomplete_the_session_hands_off_with_the_reason(tmp_path, monkeypatch, capsys):
    code, assigned, doc = run(tmp_path, monkeypatch, {"codex", "cursor"}, **only("codex", "cursor"))
    out, _ = capsys.readouterr()

    assert ("review", "cursor") not in assigned and ("review", "codex") not in assigned
    assert code == 2 and "Status: HANDOFF REQUIRED" in out
    assert "(implementation, IMPLEMENTATION_INCOMPLETE)" in out
    assert "no authorization was given" in out


@pytest.mark.parametrize("text, declared", [
    ("IMPLEMENTATION_STATUS: INCOMPLETE - blocked", True),
    ("**IMPLEMENTATION_STATUS: INCOMPLETE.** reason", True),
    ("Notes\n  implementation_status: incomplete", True),
    ("The task said to report IMPLEMENTATION_STATUS: INCOMPLETE if blocked; it is complete.", False),
    ("IMPLEMENTATION_STATUS: COMPLETE", False),
])
def test_only_a_status_line_counts_as_a_declaration(text, declared):
    assert bool(module.IMPLEMENTATION_INCOMPLETE_LINE.search(text)) is declared
