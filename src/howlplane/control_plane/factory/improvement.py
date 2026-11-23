"""Explicit owner-gated intake and opt-in queue execution observations."""

import json
from importlib import import_module

from howlplane.control_plane.factory.work_item import (
    WorkItemOrigin, WorkItemStore,
)

from howlplane.control_plane.stage_observation import observe_dispatch

REPOSITORIES = {
    "writer": "howlcipher/howlwriter",
    "plane": "howlcipher/howlplane",
    "frame": "howlcipher/howlframe",
}


def command(args):
    try:
        cli = import_module("howlloop.cli")
        analysis = import_module("howlloop.analysis")
        lifecycle = import_module("howlloop.lifecycle")
    except ImportError as exc:
        raise ValueError("Install HowlLoop in the HowlPlane environment") from exc
    data = analysis.validate_candidate(cli.read_json(args.candidate))
    store_api = import_module("howlloop.store")
    try:
        lifecycle.validate_evidence(data, store_api.Store(args.store).events())
    except store_api.StorageError as exc:
        raise ValueError("HowlLoop evidence store is unavailable") from exc
    store = WorkItemStore(args.state_dir / "work_items")
    origin = WorkItemOrigin.INFERRED_IMPROVEMENT
    repository = REPOSITORIES[data["project"]]
    keys = [data["candidate_id"]]
    item = store.admit_evidence(
        origin=origin,
        repository=repository,
        title=f"Investigate {data['project']} {data['operation']} {data['problem']}",
        identity_keys=keys,
        evidence_refs=[f"sha256:{value}" for value in data["evidence"]],
        evidence_fingerprints=data["evidence"],
        trusted_provenance=False,
        create_only=True,
        source_ref={"schema": data["schema"], "candidate_id": data["candidate_id"]},
    )
    print(json.dumps({
        "schema": "howlplane.improvement_intake/v1",
        "work_item_id": item.work_item_id,
        "state": item.state,
        "candidate_id": data["candidate_id"],
        "work_item_evidence": item.evidence_fingerprints,
        "authority": "none",
    }))
    return 0


def observe_launch(launch, dispatch, args):
    operation = "recovery" if dispatch.action == "resume" else "factory_task"
    with observe_dispatch(operation, dispatch.task.fingerprint) as observation:
        outcome = launch(dispatch, args)
        if observation is None:
            return outcome
        observation.result = {
            "COMPLETE": "ok",
            "COMPLETE WITH WARNINGS": "incomplete",
            "INTERRUPTED": "interrupted",
            "HANDOFF REQUIRED": "incomplete",
        }.get(outcome["status"], "failed")
        return outcome
