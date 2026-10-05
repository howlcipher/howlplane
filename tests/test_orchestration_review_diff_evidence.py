"""Read-only roles get the session's diff from HowlPlane (DOG-027).

Runs 030-033: Claude reviewed with Read/Glob/Grep only (review roles hold no shell
by contract), so it could not see what changed or the original lines, and said it
could not verify that existing tests were unmodified or that output stayed
byte-identical. HowlPlane now computes the diff itself and puts it in the review
and acceptance prompts, keeping changes the user already had uncommitted apart
from the implementer's work, and without running repository-configured diff programs.
"""

import subprocess

import pytest

from howlplane.control_plane import orchestration as module
from tests.test_orchestration import arguments, repository
from tests.test_orchestration_capability_recovery import accepted, install_all
from tests.test_orchestration_handoff_recovery import only


pytestmark = pytest.mark.contract


class RecordingBackend:
    """Stands in for every agent CLI: records the prompt each role received; implementation edits files."""

    def __init__(self, implement):
        self.prompts: dict[str, str] = {}
        self.implement = implement

    def execute(self, task, repo, role="implementation", prompt_override=None, **_):
        self.prompts[role] = prompt_override or ""
        if role == "implementation":
            self.implement(repo)
        return accepted("codex", role)


def run_session(tmp_path, monkeypatch, implement, prepare=None):
    repo = repository(tmp_path)
    install_all(tmp_path, monkeypatch, models=("m1",))
    if prepare:
        prepare(repo)
    backend = RecordingBackend(implement)
    monkeypatch.setattr(module.AgentBackendRegistry, "get_backend", staticmethod(lambda agent: backend))
    status = module.command(arguments(repo, input="Change the greeting", orchestrator="codex",
                                      policy="PLAN + EXECUTE + INDEPENDENT AUDIT", verify=["true"],
                                      **only("codex", "claude_code")))
    return repo, status, backend.prompts


def commit(repo, name, text):
    (repo / name).write_text(text)
    subprocess.run(["git", "-C", str(repo), "add", name], check=True)
    subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@example.invalid",
                    "commit", "-qm", name], check=True)


def test_review_and_acceptance_see_the_session_diff_apart_from_user_wip(tmp_path, monkeypatch, capsys):
    def prepare(repo):
        commit(repo, "greet.py", "def greet():\n    return 'hello'\n")
        commit(repo, "footer.txt", "Payment due in 30 days.\n")
        commit(repo, "shared.txt", "line one\n")
        (repo / "footer.txt").write_text("Payment due in 14 days.\n")   # user's WIP, left alone
        (repo / "shared.txt").write_text("line one\nuser line\n")       # user's WIP the implementer also edits
        (repo / "notes.md").write_text("my notes\n")                    # user's untracked WIP

    def implement(repo):
        (repo / "greet.py").write_text("def greet():\n    return 'hi there'\n")
        (repo / "shared.txt").write_text("line one\nuser line\nimplementer line\n")
        (repo / "test_greet.py").write_text("from greet import greet\n")

    _, status, prompts = run_session(tmp_path, monkeypatch, implement, prepare)

    assert status == 0, capsys.readouterr().out
    for role in ("review", "acceptance"):
        prompt = prompts[role]
        assert "-    return 'hello'" in prompt and "+    return 'hi there'" in prompt
        assert "New files (not in HEAD; Read them): test_greet.py" in prompt
        assert "untouched by it (not the implementer's work; do not judge them): footer.txt, notes.md" in prompt
        assert "Payment due in 14 days" not in prompt
        assert "their diff below includes those user changes as well: shared.txt" in prompt


def test_repository_configured_programs_never_run_during_a_session(tmp_path, monkeypatch, capsys):
    """DOG-028: an agent that writes .git/config must not get HowlPlane itself to run its program."""
    marker = tmp_path / "textconv-ran"

    def prepare(repo):
        commit(repo, "greet.py", "old\n")
        commit(repo, ".gitattributes", "* diff=evil\n")
        hook = repo.parent / "fsmonitor.sh"
        hook.write_text(f"#!/bin/sh\ntouch {marker}\n")
        hook.chmod(0o755)
        for key, value in (("diff.evil.textconv", f"touch {marker}; cat"), ("diff.external", f"touch {marker}"),
                           ("core.fsmonitor", str(hook))):
            subprocess.run(["git", "-C", str(repo), "config", key, value], check=True)

    _, status, prompts = run_session(tmp_path, monkeypatch, lambda repo: (repo / "greet.py").write_text("new\n"), prepare)

    assert status == 0, capsys.readouterr().out
    assert "+new" in prompts["review"]
    assert not marker.exists()


def test_large_diff_is_bounded_and_points_at_the_files(tmp_path, monkeypatch, capsys):
    def prepare(repo):
        commit(repo, "big.txt", "".join(f"old {i}\n" for i in range(5000)))

    def implement(repo):
        (repo / "big.txt").write_text("".join(f"new {i}\n" for i in range(5000)))

    _, status, prompts = run_session(tmp_path, monkeypatch, implement, prepare)

    assert status == 0, capsys.readouterr().out
    evidence = prompts["review"].split("--- diff of changed tracked files against HEAD ---", 1)[1]
    assert len(evidence) < module.REVIEW_DIFF_CHARS + 500
    assert "diff truncated by HowlPlane; Read the files for the rest" in evidence


def test_repository_without_commits_lists_new_files(tmp_path):
    repo = tmp_path / "empty"
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    (repo / "app.py").write_text("print('hi')\n")

    evidence = module.session_diff_evidence({"base_dirty": {}}, repo)

    assert "New files (not in HEAD; Read them): app.py" in evidence
    assert "--- diff of changed tracked files" not in evidence


def test_dirty_paths_keeps_names_intact_for_every_status_shape(tmp_path):
    repo = tmp_path / "r"
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    for name in ("modified.txt", "deleted.txt", "renamed.txt"):
        commit(repo, name, f"{name}\n")
    (repo / "modified.txt").write_text("changed\n")
    (repo / "deleted.txt").unlink()
    subprocess.run(["git", "-C", str(repo), "mv", "renamed.txt", "now named.txt"], check=True)
    (repo / "untracked.txt").write_text("u\n")

    paths = module.dirty_paths(repo)

    assert set(paths) == {"modified.txt", "deleted.txt", "now named.txt", "untracked.txt"}
    assert paths["deleted.txt"] is None and paths["modified.txt"]


def test_reviewers_see_the_repository_text_not_a_redacted_version(tmp_path, monkeypatch, capsys):
    """DOG-030: credential-looking examples in the diff reach the reviewer exactly as the files hold them."""
    def prepare(repo):
        commit(repo, "README.md", "# app\n")

    def implement(repo):
        (repo / "README.md").write_text("# app\n\n    API_TOKEN='paste-the-printed-token-here'\n    password=example123\n")

    _, status, prompts = run_session(tmp_path, monkeypatch, implement, prepare)

    assert status == 0, capsys.readouterr().out
    assert "+    API_TOKEN='paste-the-printed-token-here'" in prompts["review"]
    assert "+    password=example123" in prompts["review"]
