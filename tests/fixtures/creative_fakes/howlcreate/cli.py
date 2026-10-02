import json
import os
import sys
from pathlib import Path

args = sys.argv[1:]


def opt(name):
    return args[args.index(name) + 1]


counter = Path(os.environ["FAKE_COUNTER_DIR"]) / f"create-{args[0]}"
counter.write_text(str(int(counter.read_text()) + 1 if counter.exists() else 1))
if args[0] in {"develop", "scaffold"}:
    package = json.loads(Path(opt("--from-writer")).read_text())
    Path(opt("--output")).write_text(json.dumps({
        "development_id": "dev-fake",
        "lineage": {"writer_proposal_id": package["writer_proposal_id"]},
        "provenance": {"contribution": {"operation": "DESIGNED", "inference_occurred": args[0] == "develop"}},
    }))
elif args[0] == "materialize":
    if os.environ.get("FAKE_FAIL_STAGE") == "materialize":
        sys.stderr.write("Materialization denied\n")
        raise SystemExit(3)
    out = Path(opt("--output-dir"))
    out.mkdir(parents=True, exist_ok=True)
    (out / "index.html").write_text("<!doctype html>")
    (out / "create-artifact-manifest.json").write_text(json.dumps({
        "materialization_id": "mat-fake", "create_run_id": "dev-fake",
        "source_writer_ids": ["wp-000000000001"],
        "artifacts": [{"artifact_id": "art-1", "path": "index.html"}],
        "contribution": {"component": "howlcreate", "operation": "MATERIALIZED"},
    }))
