"""Acceptance-only recovery cannot reopen an already completed implementation."""
import sys

import pytest

from howlplane.control_plane import orchestration as module
from tests.test_orchestration import arguments, enable_fake_codex, enable_fake_review_pair, repository, result

pytestmark = pytest.mark.contract


@pytest.mark.parametrize('accepted', [True, False])
def test_resume_reruns_explicit_check_and_never_reimplements(tmp_path, monkeypatch, accepted):
    repo = repository(tmp_path)
    enable_fake_review_pair(tmp_path, monkeypatch)
    calls = []

    def initial(doc, role, agent, model, cwd):
        calls.append(role)
        response = result(agent, role)
        if role == 'implementation':
            (cwd / 'README').write_text('substantive preserved artifact\n')
        if role == 'acceptance':
            response.success = False
            response.exit_code = 1
            response.stderr = 'owned worker interrupted before acceptance'
        else:
            response.stdout = 'plan' if role == 'planning' else 'implementation complete'
        return response

    monkeypatch.setattr(module, 'execute_assignment', initial)
    assert module.command(arguments(repo, policy='PLAN + EXECUTE', claude_code='UNAVAILABLE')) != 0
    doc = module.active_sessions(module.state_root(), repo)[0]
    assert doc['stage'] == 'acceptance'
    assert any(a['stage'] == 'implementation' and a['state'] == 'SUCCEEDED' for a in doc['attempts'])
    before = module.evidence(repo)
    calls.clear()

    def accept(doc, role, agent, model, cwd):
        calls.append(role)
        response = result(agent, role)
        response.stdout = ('ACCEPTANCE_STATUS: ACCEPTED' if accepted
                           else 'Evidence is insufficient.\nACCEPTANCE_STATUS: REJECTED')
        return response

    monkeypatch.setattr(module, 'execute_assignment', accept)
    exit_code = module.command(arguments(repo, input='resume', verification_only=True,
                                         orchestrator='claude_code', claude_code='AUTO',
                                         verify=[sys.executable, '-c', 'print("actual bounded check")']))
    assert (exit_code == 0) is accepted
    assert calls and set(calls) == {'acceptance'}
    assert module.evidence(repo) == before
    saved = module.active_sessions(module.state_root(), repo, include_terminal=True)[0]
    assert any(t.get('exit_code') == 0 and 'actual bounded check' in t.get('output', '') for t in saved['tests'])


def test_verification_only_cannot_create_new_session(tmp_path, monkeypatch):
    repo = repository(tmp_path)
    enable_fake_codex(tmp_path, monkeypatch)
    with pytest.raises(module.OrchestrateRequestError, match='only resumes'):
        module.command(arguments(repo, verification_only=True))
    assert module.active_sessions(module.state_root(), repo, include_terminal=True) == []
