"""Failed worker remediation must not be reported as completed work."""
import json

import pytest

from src.control_plane.agent_execution import FakeAgentBackend
from src.control_plane.orchestrator import GovernedTaskOrchestrator, OrchestrationConfig
from src.control_plane.task_spec import TaskSpec
from src.control_plane.synthesis.provider_pool import ProviderPoolManager
from tests.test_closed_loop_orchestrator import _init_test_git_repo


@pytest.mark.parametrize('timeout', [False, True])
@pytest.mark.parametrize('with_pool', [False, True])
@pytest.mark.parametrize('failure_cycle', [1, 2])
def test_failed_remediation_preserves_evidence_restores_baseline_and_stops(
    tmp_path, timeout, with_pool, failure_cycle,
):
    repo = _init_test_git_repo(tmp_path / 'repo')
    original = (repo / 'src/auth.py').read_text()

    class Backend(FakeAgentBackend):
        remediation_calls = 0

        def execute(self, task, cwd, role='implementation', **kwargs):
            result = super().execute(task, cwd, role=role, **kwargs)
            path = cwd / 'src/auth.py'
            path.write_text(path.read_text() + f'\n# {role} partial change\n')
            if role == 'remediation':
                self.remediation_calls += 1
            if role == 'remediation' and self.remediation_calls == failure_cycle:
                result.success = False
                result.exit_code = 0 if timeout else 1
                result.timed_out = timeout
                result.error_message = 'unfinished remediation'
                if timeout:
                    result.metadata['timeout_source'] = 'harness'
            return result

    backend = Backend(agent_id='claude_code')
    spec = TaskSpec(task_id='REM-FAIL', repository='fixture', objective='Fix a real defect')
    orchestrator = GovernedTaskOrchestrator(repo, config=OrchestrationConfig(
        custom_backend=backend,
        provider_pool=ProviderPoolManager(probe_on_start=False) if with_pool else None,
        custom_reviewer_fn=lambda role, diff, task: 'findings:\n  - id: F1\n    title: Still broken\n    severity: high\n    category: correctness\n',
        max_remediation_cycles=3,
    ))
    result = orchestrator.run(spec)
    assert result.final_state == 'failed'
    assert result.exit_code != 0
    if timeout:
        assert result.failure_class == 'EXECUTION_BUDGET_EXCEEDED'
    assert len(result.review_cycles) == failure_cycle
    assert [c['role'] for c in backend.executed_calls].count('remediation') == failure_cycle
    run = repo / '.task_runs/REM-FAIL'
    cycle = run / f'remediation/cycle-{failure_cycle:02d}'
    saved = json.loads((cycle / 'result.json').read_text())
    assert saved['success'] is False
    assert '# remediation partial change' in (cycle / 'diff.patch').read_text()
    assert (cycle / 'diff.patch').read_text().count('+# remediation partial change') == failure_cycle
    assert '# implementation partial change' in (cycle / 'diff.patch').read_text()
    assert (repo / 'src/auth.py').read_text() == original
    checkpoint = json.loads((run / f'checkpoints/remediating_{failure_cycle:02d}.json').read_text())
    assert checkpoint['status'] == 'failed'
    disposition = json.loads((cycle / 'failure.json').read_text())
    assert disposition['rollback']['restored'] is True
