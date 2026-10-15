"""Read-only binding of a finite native queue to an existing engineering mission.

The operator supplies a reviewed contract with exact input digests. This is a
conservation guard, never an authority envelope or an acceptance writer. Native
sessions retain their own execution/claim/recovery protocol.
"""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
from typing import Any, Iterator


def _integer(value: Any, label: str) -> int:
    if type(value) is not int or value < 0:
        raise ValueError(f'{label} must be a nonnegative integer')
    return value


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class MissionBinding:
    """A pinned external mission, with a host flock shared with its old controller."""

    @classmethod
    def load(cls, contract: Path, queue: Path) -> MissionBinding:
        try:
            return cls._load(contract, queue)
        except (KeyError, TypeError, AttributeError) as exc:
            raise ValueError('malformed existing-mission binding or canonical inputs') from exc

    @classmethod
    def _load(cls, contract: Path, queue: Path) -> MissionBinding:
        instance = cls()
        instance.path = contract.resolve()
        contract_bytes = instance.path.read_bytes()
        instance.contract_digest = hashlib.sha256(contract_bytes).hexdigest()
        instance.contract = json.loads(contract_bytes)
        if instance.contract.get('schema') != 'howlplane.mission_binding/v1':
            raise ValueError('unsupported mission binding schema')
        instance.queue_path = queue.resolve()
        instance.workspace = Path(instance.contract['workspace']).resolve(strict=True)
        instance.ledger_path = Path(instance.contract['native_ledger']).resolve()
        if not instance.ledger_path.is_relative_to(instance.workspace):
            raise ValueError('native mission ledger must be inside its exclusively owned workspace')
        if not instance.workspace.is_dir():
            raise ValueError('mission workspace must be a directory')
        instance.validate()
        return instance

    def validate(self) -> None:
        c = self.contract
        if _digest(self.path) != self.contract_digest:
            raise ValueError('mission contract changed; explicit revalidation required')
        queue_bytes = self.queue_path.read_bytes()
        if hashlib.sha256(queue_bytes).hexdigest() != c['queue_sha256']:
            raise ValueError('native queue changed; explicit mission revalidation required')
        self.queue_document = json.loads(queue_bytes)
        if not isinstance(self.queue_document, dict):
            raise ValueError('native queue must be an object')
        sources = c['sources']
        if not isinstance(sources, dict) or not {'state.json', 'queue.json', c['accepted_ledger']} <= sources.keys():
            raise ValueError('mission sources must pin state, queue and accepted ledger')
        raw = {}
        for name, digest in sources.items():
            path = (self.workspace / name).resolve(strict=True)
            if not path.is_relative_to(self.workspace):
                raise ValueError('mission source must remain inside workspace')
            content = path.read_bytes()
            if hashlib.sha256(content).hexdigest() != digest:
                raise ValueError(f'mission source changed: {name}')
            raw[name] = content
        reserved = [(self.workspace / n).resolve() for n in sources]
        reserved += [self.path, self.queue_path, self.workspace / 'controller.lock']
        for destination in [self.ledger_path, self.ledger_path.with_suffix('.lock')]:
            for source in reserved:
                if destination == source or (destination.exists() and source.exists() and destination.samefile(source)):
                    raise ValueError('native ledger or its lock collides with canonical input or controller lock')
        state = json.loads(raw['state.json'])
        queue = json.loads(raw['queue.json'])
        if state['mission'] != c['mission_id']:
            raise ValueError('mission identity mismatch')
        accepted = _integer(state['budget']['accepted_items'], 'accepted count')
        maximum = _integer(state['budget']['maximum'], 'maximum')
        if maximum != _integer(c['maximum'], 'bound maximum') or accepted > maximum:
            raise ValueError('mission ceiling mismatch or exceeded')
        ids = c['accepted_ids']
        if (not isinstance(ids, list) or not all(isinstance(i, str) and i for i in ids)
                or len(ids) != len(set(ids))):
            raise ValueError('accepted identities must be nonempty unique strings')
        completed = {r['task_id'] for line in raw[c['accepted_ledger']].splitlines()
                     if (r := json.loads(line)).get('action') == 'task_completed'
                     and r.get('metadata', {}).get('counts_as_accepted_item') is not False}
        if len(ids) != accepted or not set(ids) <= completed or c['mission_id'] in ids:
            raise ValueError('accepted lineage does not match canonical count and evidence')
        limit = _integer(c['repair_limit'], 'repair limit')
        if not 0 < limit <= 3:
            raise ValueError('mission binding cannot raise the three-attempt repair limit')
        attempts = state['root_cause_attempts']
        for value in attempts.values():
            _integer(value, 'root cause attempts')
        for value in state['secondary_workflow_cycles'].values():
            if _integer(value['current_cycle'], 'cycle') > _integer(value['ceiling'], 'cycle ceiling'):
                raise ValueError('secondary cycle ceiling exceeded')
        if not isinstance(queue, list) or len({q['id'] for q in queue}) != len(queue):
            raise ValueError('canonical queue must contain unique identities')
        canonical = {q['id']: q for q in queue}
        tasks = c['tasks']
        if not isinstance(tasks, dict) or not tasks:
            raise ValueError('mission binding needs explicit task mappings')
        if len({m['queue_id'] for m in tasks.values()}) != len(tasks):
            raise ValueError('canonical queue identities cannot have duplicate native aliases')
        for task_id, mapping in tasks.items():
            if not isinstance(task_id, str) or not task_id or mapping['queue_id'] not in canonical:
                raise ValueError('native task must map to an existing canonical queue item')
            if mapping['purpose'] not in {'engineering', 'verification'}:
                raise ValueError('task purpose must be engineering or verification')
            _integer(canonical[mapping['queue_id']].get('attempts', 0), 'queue attempts')
            if mapping['purpose'] == 'engineering':
                if mapping.get('root_cause') not in attempts:
                    raise ValueError('engineering task must map to an existing root-cause counter')
                workflow = mapping.get('secondary_workflow')
                if workflow is not None and workflow not in state['secondary_workflow_cycles']:
                    raise ValueError('engineering task names an unknown secondary workflow')
        self.state, self.canonical_queue = state, canonical

    def projection(self) -> dict[str, Any]:
        self.validate()
        return {
            'mission_id': self.state['mission'],
            **self.state['budget'],
            'accepted_ids': self.contract['accepted_ids'],
            'root_cause_attempts': self.state['root_cause_attempts'],
            'secondary_workflow_cycles': self.state['secondary_workflow_cycles'],
            'pending_decisions': self.state.get('pending_decisions', []),
            'canonical_queue': list(self.canonical_queue.values()),
            'status': self.state.get('status'),
            'stop_reason': self.state.get('stop_reason'),
            'source_contract_sha256': self.contract_digest,
            'authority_granted': False,
        }

    def admit(self, task_id: str, records: list[dict[str, Any]]) -> str | None:
        self.validate()
        mapping = self.contract['tasks'].get(task_id)
        if mapping is None:
            return 'task is not in the pinned mission mapping'
        if self.state.get('status') in {'STOPPED_BY_OWNER', 'CANCELLED'}:
            return 'mission stopped by owner'
        if self.state.get('status', '').startswith('STOPPED') and mapping['purpose'] != 'verification':
            return 'canonical mission is stopped'
        starts = [r for r in records if r['event'] == 'started'
                  and (r['task_id'] == task_id or r.get('canonical_queue_id') == mapping['queue_id'])]
        legacy = self.canonical_queue[mapping['queue_id']].get('attempts', 0)
        if legacy + len(starts) >= self.contract['repair_limit']:
            return 'cumulative repair limit reached; revision changes do not reset attempts'
        if mapping['purpose'] == 'engineering':
            root_attempts = self.state['root_cause_attempts'][mapping['root_cause']]
            engineering_starts = [r for r in records if r['event'] == 'started'
                                  and r.get('purpose', self.contract['tasks'].get(r['task_id'], {}).get('purpose')) == 'engineering']
            root_starts = [r for r in engineering_starts
                           if r.get('root_cause', self.contract['tasks'].get(r['task_id'], {}).get('root_cause')) == mapping['root_cause']]
            if root_attempts + len(root_starts) >= self.contract['repair_limit']:
                return 'applicable root-cause repair limit reached'
            workflow = mapping.get('secondary_workflow')
            if workflow is not None:
                cycle = self.state['secondary_workflow_cycles'][workflow]
                cycle_starts = [r for r in engineering_starts
                                if r.get('secondary_workflow', self.contract['tasks'].get(r['task_id'], {}).get('secondary_workflow')) == workflow]
                # A native engineering dispatch includes repair and retest, so
                # reserve one significant workflow cycle even if it later fails.
                if cycle['current_cycle'] + len(cycle_starts) >= cycle['ceiling']:
                    return 'applicable secondary workflow cycle limit reached'
            if self.canonical_queue[mapping['queue_id']].get('status', '').startswith('DONE'):
                return 'completed canonical engineering work cannot be dispatched again'
            if task_id in self.contract['accepted_ids'] or mapping['queue_id'] in self.contract['accepted_ids']:
                return 'accepted engineering work cannot be dispatched again'
            reserved = {r.get('canonical_queue_id', self.contract['tasks'].get(r['task_id'], {}).get('queue_id', r['task_id']))
                        for r in records if r['event'] == 'started'
                        and r.get('purpose', self.contract['tasks'].get(r['task_id'], {}).get('purpose')) == 'engineering'}
            remaining = self.state['budget']['maximum'] - self.state['budget']['accepted_items']
            if mapping['queue_id'] not in reserved and len(reserved) >= remaining:
                return 'accepted-item ceiling reached or remaining slots already reserved'
        return None

    @contextmanager
    def exclusive(self) -> Iterator[None]:
        try:
            import fcntl
        except ImportError as exc:
            raise ValueError('existing mission ownership requires host flock support') from exc
        # Never truncate/unlink: all contenders must lock the same inode.
        with (self.workspace / 'controller.lock').open('a+') as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise ValueError('existing mission is owned by another controller') from exc
            try:
                self.validate()
                yield
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)
