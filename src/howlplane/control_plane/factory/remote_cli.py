"""`howlplane factory snapshot` and `factory pending`: read-only remote-observation commands.

Split out of cli.py as the first step of breaking that module up. Handlers take
the parsed argparse namespace and return an exit code, like every other command.
"""

import argparse
import json
from pathlib import Path


def cmd_factory_pending(args: argparse.Namespace) -> int:
    """Read-only projection of the admit path's Pending rows. Admits nothing, takes no lock."""
    from howlplane.control_plane import backlog_source
    repo = Path(getattr(args, "repo", None) or ".").expanduser().resolve()
    if args.validate:
        doc = backlog_source.validate_pending_row(repo, args.validate, getattr(args, 'recorded_by', None))
        ok = bool(doc["admittable"])
    else:
        doc = backlog_source.pending_projection(repo)
        ok = True
    if getattr(args, "json", False):
        print(json.dumps(doc, indent=2))
    elif args.validate:
        print(f"Row {doc['item_id']}: {'admittable' if ok else 'not admittable'}"
              + ("" if ok else f" ({doc['reason']})"))
        if doc["task_id"]:
            print(f"Task id: {doc['task_id']} ({doc['source_file']})")
    else:
        for row in doc["rows"]:
            if row["eligible"]:
                print(f"{row['rank']:>3}. {row['item_id']} {row['title']} ({row['source_file']})")
            else:
                print(f"  - {row['item_id']} not admittable: {row['reason']}")
        if not doc["rows"]:
            print("No Pending backlog rows.")
    return 0 if ok else 1


def cmd_factory_snapshot(args: argparse.Namespace) -> int:
    """Read-only: report a published snapshot's freshness. Never touches supervisor state."""
    from howlplane.control_plane.factory import status_publish
    repo = Path(getattr(args, "repo", None) or ".").expanduser().resolve()
    path = Path(args.path) if args.path else status_publish.default_publish_path(repo)
    result = status_publish.read_snapshot(
        path, repo_root=repo,
        fresh_seconds=args.fresh_seconds or status_publish.DEFAULT_FRESH_SECONDS)
    if getattr(args, "json", False):
        print(json.dumps(result, indent=2, default=str))
    else:
        print(f"Remote status snapshot: {result['freshness']}")
        if result["freshness"] == status_publish.SNAPSHOT_ABSENT:
            print("No snapshot has been published. Status is unknown, not healthy.")
            print("Publish one on the Factory host: howlplane factory status --publish")
        elif result["freshness"] == status_publish.SNAPSHOT_INVALID:
            print("The snapshot could not be read or has an unsupported schema.")
        else:
            print(f"Age: {result['age_seconds']}s (fresh window {result['fresh_seconds']}s)")
            print(f"State: {result['snapshot'].get('state')}")
            if result["tip_sha"]:
                print(f"Tip: {result['tip_sha'][:12]} ({result['path']})")
            if result["freshness"] == status_publish.STALE:
                print("Stale: the Factory host has not published recently. Do not assume it is running.")
    return 0 if result["freshness"] == status_publish.FRESH else 1
