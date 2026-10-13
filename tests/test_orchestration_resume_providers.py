"""Resume routing contracts; no provider completion or acceptance is simulated."""

import pytest

from howlplane.control_plane import orchestration as module
from howlplane.control_plane.cli import build_parser
from howlplane.control_plane.presentation.errors import OperatorFailure
from tests.test_orchestration import arguments, repository
from tests.test_orchestration_capability_recovery import install_all, persist


def session(tmp_path, monkeypatch):
    repo = repository(tmp_path)
    install_all(tmp_path, monkeypatch)
    monkeypatch.setattr(module.agent_readiness, "cached_summaries", lambda: {})
    doc = module.setup(arguments(repo, claude_code="UNAVAILABLE"), repo)
    doc["status"] = "INTERRUPTED"
    path = persist(doc)
    return repo, doc, path


def resume_args(repo, *flags):
    return build_parser().parse_args(["orchestrate", "resume", "--repo", str(repo), *flags])


def test_resume_literal_provider_options_precede_orchestrator_selection(tmp_path, monkeypatch):
    repo, _, path = session(tmp_path, monkeypatch)
    routes = []
    discovered = []
    def models(agent):
        discovered.append(agent)
        return ["m1", "m2"]
    monkeypatch.setattr(module, "discover_models", models)

    def intercept_dispatch(document, role, agent, _model, _repo):
        routes.append((role, agent))
        assert document["orchestrator"] == "claude_code"
        assert document["agents"]["codex"]["state"] == "UNAVAILABLE"
        raise KeyboardInterrupt

    monkeypatch.setattr(module, "execute_assignment", intercept_dispatch)
    assert module.command(resume_args(repo, "--orchestrator", "claude_code",
                                     "--codex", "UNAVAILABLE", "--claude-code", "AUTO")) == 130
    assert routes == [("planning", "claude_code")]
    assert discovered == ["claude_code"]
    assert path.exists()


def test_resume_without_options_preserves_unavailable_provider(tmp_path, monkeypatch):
    repo, _, _ = session(tmp_path, monkeypatch)
    def checkpoint(document, *_args):
        assert document["agents"]["claude_code"]["state"] == "UNAVAILABLE"
        assert document["orchestrator"] == "codex"
        return 2
    monkeypatch.setattr(module, "run", checkpoint)
    assert module.command(resume_args(repo)) == 2


@pytest.mark.parametrize("negative", ["capability", "authentication", "quota", "trust"])
def test_resume_auto_cannot_revive_fresh_negative_readiness(tmp_path, monkeypatch, negative):
    repo, _, path = session(tmp_path, monkeypatch)
    before = path.read_bytes()
    record = {
        "unattended_execution": False if negative == "capability" else True,
        "unattended_source": "negative cache", "last_verified_at": module.now(),
        "authenticated": negative != "authentication",
        "capacity": {"state": "QUOTA_EXHAUSTED" if negative == "quota" else "UNKNOWN", "limits": []},
        "live_smoke": {"status": "PASS", "verified_at": module.now()},
    }
    monkeypatch.setattr(module.agent_readiness, "cached_summaries", lambda: {"claude_code": record})
    if negative == "trust":
        monkeypatch.setattr(module.workspace_trust, "check", lambda *_args: {
            "state": module.workspace_trust.TRUST_REQUIRED, "workspace": str(repo),
            "checked_at": module.now(),
        })
        monkeypatch.setattr(module.workspace_trust, "effective", lambda *_args: {
            "effective_state": module.workspace_trust.TRUST_REQUIRED, "mechanism": "strict",
        })
    monkeypatch.setattr(module, "run", lambda *_args: pytest.fail("negative readiness must not dispatch"))
    with pytest.raises((OperatorFailure, module.OrchestrateRequestError)):
        module.command(resume_args(repo, "--orchestrator", "claude_code", "--claude-code", "AUTO"))
    assert path.read_bytes() == before


def test_resume_auto_retains_session_hard_failures_and_capacity(tmp_path, monkeypatch):
    repo, doc, _ = session(tmp_path, monkeypatch)
    module.record_failure(doc, "claude_code", "implementation", "m1", "EXECUTION_PERMISSION_REQUIRED")
    module.record_failure(doc, "codex", "planning", "m1", "AUTHENTICATION_REQUIRED")
    module.record_failure(doc, "agy", "implementation", "m1", "SESSION_LIMIT")
    module.record_failure(doc, "agy", "implementation", "m2", "QUOTA_EXHAUSTED")
    module.record_failure(doc, "cursor", "implementation", "UNKNOWN", "QUOTA_EXHAUSTED")
    doc["attempts"].append({"stage": "planning", "agent": "codex", "model": "m1",
                            "state": "REVOKED", "failure": "AUTHENTICATION_REQUIRED"})
    persist(doc)
    def checkpoint(document, *_args):
        assert document["agents"]["claude_code"]["capabilities"]["unattended_execution"] is False
        assert document["agents"]["codex"]["state"] == "UNAVAILABLE"
        assert document["agents"]["cursor"]["state"] == "UNAVAILABLE"
        assert document["agents"]["agy"]["capacity"]["implementation"]["reason"] == "SESSION_LIMIT"
        assert document["model_states"]["agy:m2"] == "EXHAUSTED"
        assert not any(agent in {"claude_code", "codex", "agy", "cursor"}
                       for agent, _ in module.candidates(document, "implementation"))
        return 2
    monkeypatch.setattr(module, "run", checkpoint)
    assert module.command(resume_args(repo, "--claude-code", "AUTO", "--codex", "AUTO",
                                     "--agy", "AUTO", "--cursor", "AUTO", "--orchestrator", "AUTO")) == 2


def test_resume_explicit_unavailable_orchestrator_fails_without_state_write(tmp_path, monkeypatch):
    repo, _, path = session(tmp_path, monkeypatch)
    before = path.read_bytes()
    monkeypatch.setattr(module, "run", lambda *_args: pytest.fail("invalid lead must not dispatch"))
    with pytest.raises(OperatorFailure):
        module.command(resume_args(repo, "--orchestrator", "codex", "--codex", "UNAVAILABLE"))
    assert path.read_bytes() == before


@pytest.mark.parametrize("failure", ["AUTHENTICATION_REQUIRED", "QUOTA_EXHAUSTED"])
def test_reservation_cannot_mask_retained_unavailable_failure(tmp_path, monkeypatch, failure):
    repo, doc, _ = session(tmp_path, monkeypatch)
    module.record_failure(doc, "claude_code", "planning", "UNKNOWN", failure)
    doc["attempts"].append({"stage": "planning", "agent": "claude_code", "model": "UNKNOWN",
                            "state": "REVOKED", "failure": failure})
    persist(doc)
    monkeypatch.setattr(module, "run", lambda *_args: 2)
    assert module.command(resume_args(repo, "--claude-code", "RESERVED")) == 2
    monkeypatch.setattr(module, "run", lambda *_args: pytest.fail("reservation cannot clear hard failures"))
    with pytest.raises(OperatorFailure):
        module.command(resume_args(repo, "--claude-code", "AUTO", "--orchestrator", "claude_code"))


def test_resume_auto_acceptance_retains_usable_lead_past_unavailable_first_provider(tmp_path, monkeypatch):
    repo, doc, _ = session(tmp_path, monkeypatch)
    doc.update({"policy": "PLAN + EXECUTE", "stage": "acceptance", "implementer": "external"})
    persist(doc)
    calls = []
    def intercept_dispatch(document, role, agent, _model, _repo):
        calls.append((role, agent))
        assert document["requested_orchestrator"] == "AUTO"
        raise KeyboardInterrupt
    monkeypatch.setattr(module, "execute_assignment", intercept_dispatch)
    assert module.command(resume_args(repo, "--orchestrator", "AUTO", "--claude-code", "UNAVAILABLE",
                                     "--codex", "AUTO")) == 130
    assert calls == [("acceptance", "codex")]


def test_resume_auto_cannot_shop_recorded_acceptance_rejection(tmp_path, monkeypatch):
    repo, doc, _ = session(tmp_path, monkeypatch)
    doc.update({"policy": "PLAN + EXECUTE", "stage": "acceptance", "implementer": "external"})
    module.record_failure(doc, "codex", "acceptance", "m1", "ACCEPTANCE_REJECTED_OR_UNCONFIRMED")
    persist(doc)
    monkeypatch.setattr(module, "execute_assignment", lambda *_args: pytest.fail("rejection must not be shopped"))
    assert module.command(resume_args(repo, "--orchestrator", "AUTO", "--codex", "AUTO",
                                     "--claude-code", "AUTO")) == 2
