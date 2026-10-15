"""Existing campaign guard: canonical state stays authoritative and unchanged."""
import hashlib
import json
from pathlib import Path

import pytest

from howlplane.control_plane.factory.mission_binding import MissionBinding

pytestmark = pytest.mark.contract


def write_json(path, value):
    path.write_text(json.dumps(value))


@pytest.fixture
def binding(tmp_path):
    workspace = tmp_path / 'mission'
    workspace.mkdir()
    write_json(workspace / 'state.json', {
        'mission': 'M-001', 'budget': {'accepted_items': 2, 'maximum': 3},
        'root_cause_attempts': {'old': 3, 'current': 0},
        'secondary_workflow_cycles': {'secondary': {'current_cycle': 2, 'ceiling': 2}},
        'pending_decisions': [{'status': 'PENDING', 'action': 'do not invent approval'}],
    })
    write_json(workspace / 'queue.json', [{'id': 'remaining', 'attempts': 1}])
    (workspace / 'accepted.jsonl').write_text('\n'.join(json.dumps({
        'task_id': task, 'action': 'task_completed'
    }) for task in ['M-001-A', 'M-001-B']) + '\n')
    queue = tmp_path / 'native-queue.json'
    write_json(queue, {'schema': 'howlplane.factory_queue/v1', 'tasks': []})
    contract = tmp_path / 'binding.json'
    write_json(contract, {
        'schema': 'howlplane.mission_binding/v1', 'workspace': str(workspace),
        'mission_id': 'M-001', 'maximum': 3, 'native_ledger': str(workspace / 'native.jsonl'),
        'accepted_ids': ['M-001-A', 'M-001-B'],
        'sources': {name: hashlib.sha256((workspace / name).read_bytes()).hexdigest()
                    for name in ['state.json', 'queue.json', 'accepted.jsonl']},
        'accepted_ledger': 'accepted.jsonl',
        'queue_sha256': hashlib.sha256(queue.read_bytes()).hexdigest(),
        'tasks': {'remaining': {'purpose': 'engineering', 'queue_id': 'remaining', 'root_cause': 'current'}},
        'repair_limit': 3,
    })
    return contract, queue, workspace


@pytest.fixture
def unit_args(binding, tmp_path):
    import argparse
    import sys
    contract, queue, workspace = binding
    target = tmp_path / 'target'
    (target / '.git').mkdir(parents=True)
    checkout = tmp_path / 'controller'
    package = checkout / 'src/howlplane/control_plane'
    package.mkdir(parents=True)
    (package / 'cli.py').write_text('# isolated source fixture')
    return argparse.Namespace(
        checkout=checkout, python=Path(sys.executable), state_dir=workspace,
        target_repo=target, output=tmp_path / 'candidate.service',
        worker_path='/usr/bin:/bin', queue_file=queue, mission_contract=contract,
    )


def test_read_consumes_lineage_limits_attempts_and_decisions_without_writes(binding):
    contract, queue, workspace = binding
    before = {p.name: p.read_bytes() for p in workspace.iterdir()}
    mission = MissionBinding.load(contract, queue)
    projection = mission.projection()
    assert projection['mission_id'] == 'M-001'
    assert projection['accepted_items'] == 2 and projection['maximum'] == 3
    assert projection['root_cause_attempts'] == {'old': 3, 'current': 0}
    assert projection['secondary_workflow_cycles']['secondary']['current_cycle'] == 2
    assert projection['pending_decisions'][0]['status'] == 'PENDING'
    assert {p.name: p.read_bytes() for p in workspace.iterdir()} == before


def test_conflict_fences_before_state_or_ledger_writes(binding):
    import fcntl
    contract, queue, workspace = binding
    mission = MissionBinding.load(contract, queue)
    with (workspace / 'controller.lock').open('a+') as owner:
        fcntl.flock(owner, fcntl.LOCK_EX | fcntl.LOCK_NB)
        before = {p.name: p.read_bytes() for p in workspace.iterdir()}
        with pytest.raises(ValueError, match='owned'):
            with mission.exclusive():
                pytest.fail('competing controller entered')
        assert {p.name: p.read_bytes() for p in workspace.iterdir()} == before


def test_changed_sources_or_queue_reject_before_dispatch(binding):
    contract, queue, workspace = binding
    mission = MissionBinding.load(contract, queue)
    (workspace / 'state.json').write_text('{}')
    with pytest.raises(ValueError, match='changed'):
        mission.admit('remaining', [])
    with pytest.raises(ValueError, match='changed'):
        MissionBinding.load(contract, queue)


def test_cumulative_attempts_cannot_reset_by_revision(binding):
    contract, queue, workspace = binding
    mission = MissionBinding.load(contract, queue)
    assert mission.admit('remaining', []) is None
    # One recorded legacy attempt plus two native launches exhausts the same task.
    records = [{'event': 'started', 'task_id': 'remaining', 'fingerprint': rev}
               for rev in ['old', 'new']]
    assert 'repair limit' in mission.admit('remaining', records)


def test_ceiling_accepted_identity_and_no_automatic_approval(binding):
    contract, queue, workspace = binding
    data = json.loads(contract.read_text())
    data['accepted_ids'] = ['M-001-A', 'M-001-A']
    write_json(contract, data)
    with pytest.raises(ValueError, match='unique'):
        MissionBinding.load(contract, queue)


def test_lock_released_on_failure_and_repeated_read_preserves_state(binding):
    contract, queue, workspace = binding
    mission = MissionBinding.load(contract, queue)
    before = (workspace / 'state.json').read_bytes()
    with pytest.raises(RuntimeError):
        with mission.exclusive():
            raise RuntimeError('worker failure')
    with MissionBinding.load(contract, queue).exclusive():
        assert (workspace / 'state.json').read_bytes() == before


def repin(contract, workspace, name):
    data = json.loads(contract.read_text())
    data['sources'][name] = hashlib.sha256((workspace / name).read_bytes()).hexdigest()
    write_json(contract, data)


def test_engineering_ceiling_reserves_distinct_dispatches_and_allows_explicit_verification(binding):
    contract, queue, workspace = binding
    data = json.loads(contract.read_text())
    data['tasks']['second'] = {'purpose': 'engineering', 'queue_id': 'second', 'root_cause': 'current'}
    data['tasks']['verify'] = {'purpose': 'verification', 'queue_id': 'verify'}
    write_json(contract, data)
    write_json(workspace / 'queue.json', [{'id': name, 'attempts': 1} for name in ['remaining', 'second', 'verify']])
    repin(contract, workspace, 'queue.json')
    mission = MissionBinding.load(contract, queue)
    records = [{'event': 'started', 'task_id': 'remaining'}]
    assert 'ceiling' in mission.admit('second', records)
    assert mission.admit('remaining', records) is None  # same slot, still within legacy retry limit
    assert mission.admit('verify', records) is None
    assert 'pinned mission mapping' in mission.admit('unrelated', [])


def test_canonical_completion_cannot_be_reopened_by_alias(binding):
    contract, queue, workspace = binding
    write_json(workspace / 'queue.json', [{'id': 'remaining', 'attempts': 1, 'status': 'DONE'}])
    repin(contract, workspace, 'queue.json')
    assert 'completed canonical' in MissionBinding.load(contract, queue).admit('remaining', [])


def test_native_cli_conflict_and_ledger_switch_are_read_only(binding, monkeypatch, capsys):
    import fcntl
    from howlplane.control_plane import cli
    contract, queue, workspace = binding
    with (workspace / 'controller.lock').open('a+') as owner:
        fcntl.flock(owner, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert cli.main(['factory', 'queue', str(queue), '--mission-contract', str(contract), '--json']) == 1
    assert not (workspace / 'native.jsonl').exists()
    assert 'owned by another controller' in capsys.readouterr().err
    assert cli.main(['factory', 'queue', str(queue), '--mission-contract', str(contract),
                     '--ledger', str(workspace / 'replacement.jsonl')]) == 1
    assert not (workspace / 'replacement.jsonl').exists()


def test_read_only_dry_run_reports_real_campaign_not_zero_native_history(binding, capsys):
    from howlplane.control_plane import cli
    contract, queue, workspace = binding
    before = {p.name: p.read_bytes() for p in workspace.iterdir()}
    assert cli.main(['factory', 'queue', str(queue), '--mission-contract', str(contract),
                     '--dry-run', '--json']) == 0
    report = json.loads(capsys.readouterr().out)
    assert report['mission']['accepted_items'] == 2
    assert report['mission']['maximum'] == 3
    assert report['mission']['authority_granted'] is False
    assert report['mission']['pending_decisions'][0]['status'] == 'PENDING'
    assert {p.name: p.read_bytes() for p in workspace.iterdir()} == before


def test_empty_native_queue_is_not_installable_as_productive_supervision(unit_args):
    from scripts.install_factory_service import install
    with pytest.raises(ValueError, match='exactly match'):
        install(unit_args)
    assert not unit_args.output.exists()


def native_task(binding, repo):
    contract, queue, _ = binding
    write_json(queue, {'schema': 'howlplane.factory_queue/v1', 'tasks': [{
        'id': 'remaining', 'goal': 'Verify preserved artifact', 'status': 'APPROVED',
        'repo': str(repo), 'orchestrator': 'codex', 'policy': 'PLAN ONLY',
    }]})
    data = json.loads(contract.read_text())
    data['queue_sha256'] = hashlib.sha256(queue.read_bytes()).hexdigest()
    write_json(contract, data)


def test_native_session_completion_and_repeated_invocation_do_not_recount(binding, tmp_path, monkeypatch, capsys):
    from howlplane.control_plane import cli, orchestration
    from tests.test_orchestration import enable_fake_codex
    from tests.test_factory_task_queue import repository, worker
    enable_fake_codex(tmp_path, monkeypatch)
    calls = []
    monkeypatch.setattr(orchestration, 'execute_assignment', worker(calls))
    repo = repository(tmp_path / 'source')
    native_task(binding, repo)
    contract, queue, workspace = binding
    before = (workspace / 'state.json').read_bytes()
    argv = ['factory', 'queue', str(queue), '--mission-contract', str(contract), '--json', '--no-progress']
    assert cli.main(argv) == 0
    first = json.loads(capsys.readouterr().out)
    assert first['mission']['accepted_items'] == 2
    assert first['results'][0]['status'] == 'COMPLETE'
    assert first['results'][0]['session_id']
    ledger = (workspace / 'native.jsonl').read_bytes()
    assert cli.main(argv) == 0
    second = json.loads(capsys.readouterr().out)
    assert second['results'] == [] and second['skipped']['remaining'] == 'already complete'
    assert (workspace / 'native.jsonl').read_bytes() == ledger
    assert (workspace / 'state.json').read_bytes() == before


def test_reviewed_finite_unit_preserves_native_session_retry_protocol(binding, unit_args):
    from scripts.install_factory_service import install
    native_task(binding, unit_args.target_repo)
    install(unit_args)
    unit = unit_args.output.read_text()
    assert '"factory" "queue"' in unit and '"--mission-contract"' in unit
    assert '"--retry" "remaining"' in unit and '"--max-tasks" "1"' in unit
    assert '--resume-stopped' not in unit and 'Environment=HOWL_FORBID_LOCAL_INFERENCE=1' in unit
    assert 'RestartPreventExitStatus=1 78' in unit


def test_verification_cannot_start_fresh_engineering_even_with_queue_approval(binding, tmp_path, monkeypatch, capsys):
    from howlplane.control_plane import cli
    from tests.test_orchestration import enable_fake_codex
    from tests.test_factory_task_queue import repository
    enable_fake_codex(tmp_path, monkeypatch)
    repo = repository(tmp_path / 'source')
    native_task(binding, repo)
    contract, queue, workspace = binding
    data = json.loads(contract.read_text())
    data['tasks']['remaining']['purpose'] = 'verification'
    write_json(contract, data)
    assert cli.main(['factory', 'queue', str(queue), '--mission-contract', str(contract), '--json']) == 78
    result = json.loads(capsys.readouterr().out)
    assert result['results'] == [] and 'fresh engineering is forbidden' in result['blocked']['remaining']
    assert not (workspace / 'native.jsonl').exists()


@pytest.mark.parametrize('boundary', ['root', 'cycle'])
def test_applicable_exhausted_limits_prevent_engineering(binding, boundary):
    contract, queue, _ = binding
    data = json.loads(contract.read_text())
    mapping = data['tasks']['remaining']
    if boundary == 'root':
        mapping['root_cause'] = 'old'
    else:
        mapping['secondary_workflow'] = 'secondary'
    write_json(contract, data)
    reason = MissionBinding.load(contract, queue).admit('remaining', [])
    assert ('root-cause' if boundary == 'root' else 'cycle') in reason


@pytest.mark.parametrize('destination', ['state.json', 'queue.json', 'accepted.jsonl', 'controller.lock', 'controller.jsonl'])
def test_native_ledger_and_lock_cannot_collide_with_canonical_files(binding, destination):
    contract, queue, workspace = binding
    data = json.loads(contract.read_text())
    data['native_ledger'] = str(workspace / destination)
    write_json(contract, data)
    with pytest.raises(ValueError, match='collides'):
        MissionBinding.load(contract, queue)


def test_accepted_canonical_identity_cannot_be_reopened_through_alias(binding):
    contract, queue, workspace = binding
    write_json(workspace / 'queue.json', [{'id': 'M-001-A', 'attempts': 0}])
    data = json.loads(contract.read_text())
    data['tasks'] = {'alias': {'purpose': 'engineering', 'queue_id': 'M-001-A', 'root_cause': 'current'}}
    write_json(contract, data)
    repin(contract, workspace, 'queue.json')
    assert 'accepted engineering' in MissionBinding.load(contract, queue).admit('alias', [])


@pytest.mark.parametrize('fail_verify', [False, True])
def test_bound_acceptance_recovery_preserves_artifact_and_uses_new_provider(binding, tmp_path, monkeypatch, capsys, fail_verify):
    import sys
    from howlplane.control_plane import cli, orchestration
    from tests.test_orchestration import enable_fake_codex, enable_fake_review_pair, result
    from tests.test_factory_task_queue import repository
    enable_fake_codex(tmp_path, monkeypatch)
    repo = repository(tmp_path / 'source')
    native_task(binding, repo)
    contract, queue, workspace = binding
    native = json.loads(queue.read_text())
    native['tasks'][0].update(
        policy='PLAN + EXECUTE', orchestrator='AUTO',
        agents={'codex': 'AUTO', 'claude_code': 'AUTO'}, verify=[
            sys.executable, '-c', 'import os,sys; print("bounded verification executed"); sys.exit(9 if os.getenv("MISSION_VERIFY_FAIL") else 0)'
        ],
    )
    write_json(queue, native)
    data = json.loads(contract.read_text())
    data['queue_sha256'] = hashlib.sha256(queue.read_bytes()).hexdigest()
    write_json(contract, data)
    calls = []

    def interrupted(doc, role, agent, model, cwd):
        calls.append((role, agent))
        response = result(agent, role)
        response.stdout = 'plan'
        if role == 'implementation':
            (cwd / 'README').write_text('substantive artifact preserved across interrupted acceptance\n')
        if role == 'acceptance':
            response.success, response.exit_code = False, 1
            response.stderr = 'simulated owned worker interruption'
        return response

    monkeypatch.setattr(orchestration, 'execute_assignment', interrupted)
    argv = ['factory', 'queue', str(queue), '--mission-contract', str(contract), '--json', '--no-progress']
    assert cli.main(argv) == 2
    first = json.loads(capsys.readouterr().out)
    session_id = first['results'][0]['session_id']
    saved = orchestration.safe_load_json(orchestration.path_for(orchestration.state_root(), session_id))
    assert saved['stage'] == 'acceptance'
    before = orchestration.evidence(repo)
    data['tasks']['remaining'].update(purpose='verification', artifact_fingerprint=orchestration.fingerprint(before))
    write_json(contract, data)
    enable_fake_review_pair(tmp_path, monkeypatch)
    calls.clear()
    if fail_verify:
        monkeypatch.setenv('MISSION_VERIFY_FAIL', '1')

    def resumed(doc, role, agent, model, cwd):
        calls.append((role, agent))
        response = result(agent, role)
        response.stdout = 'ACCEPTANCE_STATUS: ACCEPTED'
        return response

    monkeypatch.setattr(orchestration, 'execute_assignment', resumed)
    exit_code = cli.main([*argv, '--retry', 'remaining'])
    second = json.loads(capsys.readouterr().out)
    assert second['results'][0]['action'] == 'resume'
    assert second['results'][0]['session_id'] == session_id
    assert orchestration.evidence(repo) == before
    assert second['mission']['accepted_items'] == 2
    if fail_verify:
        assert exit_code == 2 and calls == []
        assert cli.main([*argv, '--retry', 'remaining']) == 78
        third = json.loads(capsys.readouterr().out)
        assert 'repair limit' in third['blocked']['remaining']
    else:
        assert exit_code == 0 and calls == [('acceptance', 'claude_code')]
        assert cli.main(argv) == 0
        assert json.loads(capsys.readouterr().out)['results'] == []


def test_dispatch_parses_the_pinned_queue_snapshot_even_if_path_temporarily_changes(binding, tmp_path, monkeypatch, capsys):
    from howlplane.control_plane import cli, orchestration
    from howlplane.control_plane.factory import task_queue
    from tests.test_orchestration import enable_fake_codex
    from tests.test_factory_task_queue import repository, worker
    enable_fake_codex(tmp_path, monkeypatch)
    repo = repository(tmp_path / 'source')
    native_task(binding, repo)
    contract, queue, _ = binding
    original = queue.read_bytes()
    actual_loader = task_queue.load_queue
    calls = []
    monkeypatch.setattr(orchestration, 'execute_assignment', worker(calls))

    def changed_path(path, default_repo, *, document=None):
        assert document is not None
        changed = json.loads(original)
        changed['tasks'][0]['goal'] = 'UNPINNED TASK MUST NEVER EXECUTE'
        write_json(queue, changed)
        try:
            return actual_loader(path, default_repo, document=document)
        finally:
            queue.write_bytes(original)

    monkeypatch.setattr(task_queue, 'load_queue', changed_path)
    assert cli.main(['factory', 'queue', str(queue), '--mission-contract', str(contract), '--json', '--no-progress']) == 0
    assert calls and all(goal == 'Verify preserved artifact' for goal, _ in calls)


@pytest.mark.parametrize('limit', ['root', 'cycle'])
def test_different_tasks_share_cumulative_applicable_limits(binding, limit):
    contract, queue, workspace = binding
    state = json.loads((workspace / 'state.json').read_text())
    state['budget']['maximum'] = 4
    state['root_cause_attempts']['current'] = 2 if limit == 'root' else 0
    state['secondary_workflow_cycles']['secondary']['current_cycle'] = 1
    write_json(workspace / 'state.json', state)
    write_json(workspace / 'queue.json', [{'id': name, 'attempts': 0} for name in ['remaining', 'second']])
    data = json.loads(contract.read_text())
    data['maximum'] = 4
    data['tasks']['second'] = {'purpose': 'engineering', 'queue_id': 'second', 'root_cause': 'current'}
    if limit == 'cycle':
        for mapping in data['tasks'].values():
            mapping['secondary_workflow'] = 'secondary'
    write_json(contract, data)
    repin(contract, workspace, 'state.json')
    repin(contract, workspace, 'queue.json')
    mission = MissionBinding.load(contract, queue)
    assert mission.admit('remaining', []) is None
    records = [{'event': 'started', 'task_id': 'previous-alias', 'canonical_queue_id': 'remaining',
                'purpose': 'engineering', 'root_cause': 'current', 'secondary_workflow': 'secondary'}]
    assert ('root-cause' if limit == 'root' else 'cycle') in mission.admit('second', records)


def test_service_runtime_default_repository_matches_validated_target(binding, unit_args):
    import shlex
    from scripts.install_factory_service import install
    from howlplane.control_plane import cli
    from howlplane.control_plane.factory.task_queue import load_queue
    contract, queue, _ = binding
    native_task(binding, unit_args.target_repo)
    native = json.loads(queue.read_text())
    del native['tasks'][0]['repo']
    write_json(queue, native)
    data = json.loads(contract.read_text())
    data['queue_sha256'] = hashlib.sha256(queue.read_bytes()).hexdigest()
    write_json(contract, data)
    install(unit_args)
    command = shlex.split(next(
        line.removeprefix('ExecStart=:') for line in unit_args.output.read_text().splitlines()
        if line.startswith('ExecStart=:')
    ))
    runtime = cli.build_parser().parse_args(command[3:])
    assert runtime.repo == str(unit_args.target_repo)
    assert load_queue(queue, Path(runtime.repo))[0].repo == unit_args.target_repo.resolve()
