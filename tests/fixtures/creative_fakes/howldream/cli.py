import json
import os
import sys
from pathlib import Path

args = sys.argv[1:]
if os.environ.get("FAKE_FAIL_STAGE") == "dream":
    sys.stderr.write("dream exploded\n")
    raise SystemExit(2)
command = args[0]
if command == "cluster":
    out = Path(args[args.index("--out") + 1])
    out.write_text(json.dumps({"schema": "howldream.external_discovery/v1", "units": [{"id": "ext-1/idea/1"}],
                               "ranking": [{"representative": "ext-1/idea/1"}]}))
elif command == "export":
    unit = args[args.index("--candidate-id") + 1]
    print(json.dumps({
        "schema_version": "howl.candidate/v1", "candidate_id": f"hdx-fake/{unit}",
        "source_run_id": "hdx-fake", "parent_request_id": "hdx-fake", "objective": "o",
        "text": "IDEA: x", "authority": {"type": "ADVISORY", "executable": False},
        "provenance": {"producer_component": "howldream", "participation": [
            {"operation": "CLUSTERED", "transforming_component": "howldream",
             "origin_component": "supervisor", "origin_model": "m"},
            {"operation": "SELECTED", "transforming_component": "howldream",
             "origin_component": "supervisor", "origin_model": "m"}]},
    }))
elif command == "explore":
    out = Path(args[args.index("--output") + 1])
    (out / "hd-fake").mkdir(parents=True, exist_ok=True)
    (out / "hd-fake" / "discovery.json").write_text(json.dumps({"units": [{"id": "hd-fake/c/idea/1"}],
                                                               "ranking": []}))
    print(json.dumps({"exploration_id": "hd-fake"}))
