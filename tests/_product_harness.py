"""Shared harness for product-level tests: a fresh repository on a fake machine.

Only the operating system boundary is faked (spawning and killing the background
process, and the installed AI CLIs). The supervisor is ticked in-process with a
fake worker; everything above that is the real product, driven through `cli.main`.
"""

import os
import socket
from datetime import datetime, timezone

import pytest

from howlplane.control_plane import agent_readiness, cli
from howlplane.control_plane.factory import service
from howlplane.control_plane.factory.campaign import resolve_campaign
from howlplane.control_plane.factory.oplog import OperationalLog
from howlplane.control_plane.factory.work_item import WorkItemState
from howlplane.control_plane.locking import get_process_create_time
from tests._factory_test_helpers import make_git_repo, make_supervisor, ready_work_item, set_xdg_paths

WORKERS = [
    {"agent": "codex", "name": "Codex", "installed": True, "authenticated": True, "unattended_execution": True,
     "mutation_capable": True, "capacity": {"state": "UNKNOWN", "limits": []}, "live_smoke": {"status": "NOT_RUN"}},
    {"agent": "claude", "name": "Claude Code", "installed": True, "authenticated": True,
     "unattended_execution": True, "mutation_capable": True, "capacity": {"state": "UNKNOWN", "limits": []},
     "live_smoke": {"status": "NOT_RUN"}},
]


class Product:
    """The environment around the product: a fresh repository and a fake machine."""

    def __init__(self, monkeypatch, tmp_path, capsys):
        self.capsys, self.monkeypatch = capsys, monkeypatch
        self.repo = make_git_repo(tmp_path, "grocery-optimizer")
        set_xdg_paths(monkeypatch, tmp_path)
        monkeypatch.chdir(self.repo)
        self.trust = "READY"
        monkeypatch.setattr(agent_readiness, "evaluate", lambda *a, **k: WORKERS)
        monkeypatch.setattr(agent_readiness, "workspace_report", self._workspace_report)
        monkeypatch.setattr("howlplane.control_plane.factory.prepare.command", self._prepare)
        monkeypatch.setattr(service, "start_process", self._start_process)
        monkeypatch.setattr(service, "stop_process", self._stop_process)
        self.worker_ran = []
        self.started_with = []

    # Fake machine -----------------------------------------------------------
    def _workspace_report(self, *args, **kwargs):
        agents = {w["agent"]: {"effective_state": self.trust, "state": self.trust} for w in WORKERS}
        return {"workspace": str(self.repo), "authorized": self.trust == "READY", "agents": agents}

    def _prepare(self, args):
        self.trust = "READY"
        return 0

    def _campaign(self):
        return resolve_campaign(self.repo, prefer_active=True)

    def _supervisor(self):
        campaign = self._campaign()
        supervisor, _now, _sleeps = make_supervisor(campaign.state_dir, state_dir=campaign.state_dir)
        product = self

        class FakeWorker:
            def execute_factory_work_item(self, item, files_changed=None, dispatch_id=None):
                product.worker_ran.append(item.work_item_id)
                return True, {"provider": "codex"}

        from howlplane.control_plane.factory.dispatcher import MarathonDispatcherAdapter
        supervisor.dispatcher = MarathonDispatcherAdapter(lambda: FakeWorker())
        return supervisor

    def _start_process(self, campaign, authority_profile, objective, max_work_items=None):
        self.started_with.append({"objective": objective, "authority": authority_profile})
        existing = service.load_process(campaign)
        if existing and service._active(campaign, existing):
            return False, existing
        record = service.FactoryProcessRecord(
            os.getpid(), get_process_create_time(os.getpid()), socket.gethostname(), "process", ["fake-supervisor"],
            datetime.now(timezone.utc).isoformat(), str(campaign.state_dir / "logs" / "factory.log"))
        service._save_process(campaign, record)
        OperationalLog(campaign.state_dir, {"campaign_id": campaign.repository.campaign_id}).emit(
            "process.started", "Factory process started (process)", backend="process")
        self._supervisor().resume()  # the real run loop starts with --resume-stopped
        return True, record

    def _stop_process(self, campaign, timeout_seconds=30.0):
        record = service.load_process(campaign)
        if record is not None:
            record.status = "stopped"
            service._save_process(campaign, record)
        return "Factory stopped."

    def tick(self):
        self._supervisor().tick()

    def seed_owner_decision(self):
        """Work that needs the owner, exactly as the supervisor parks it."""
        store = self._supervisor().work_item_store
        item = ready_work_item(store, repository="grocery-optimizer", title="Add Kroger search adapter", key="kroger")
        item.transition_to(WorkItemState.AWAITING_OWNER, reason="needs owner approval")
        store.save_object(item)
        return item

    def seed_proposal(self, proposal_id="RP-17", name="kroger-adapter"):
        from howlplane.control_plane.factory.repo_proposal import RepoProposalStore
        store = RepoProposalStore(self._campaign().state_dir / "repo_proposals")
        store.propose(proposal_id, name, "propose_new_repository", "reusable adapter",
                      ["fp-b", "fp-a"], {"capability_id": "kroger"})
        return store

    def state_record(self):
        return self._supervisor().state_store.load(reconcile_restart=False)

    # The product, as a user types it ----------------------------------------
    def run(self, *argv):
        code = cli.main(list(argv))
        out = self.capsys.readouterr()
        return code, out.out, out.err


@pytest.fixture
def product(monkeypatch, tmp_path, capsys):
    return Product(monkeypatch, tmp_path, capsys)
