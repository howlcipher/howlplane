import json
import os
import sys
from pathlib import Path

args = sys.argv[1:]
assert args[0] == "native"
assert os.environ.get("HOWL_FORBID_LOCAL_INFERENCE") == "1"


def opt(name):
    return args[args.index(name) + 1]


if args[1] == "request":
    candidate = json.loads(Path(opt("--from-dream")).read_text())
    Path(opt("--out")).write_text(json.dumps({"request_id": "wreq-000000000001",
                                              "source_idea_id": candidate["candidate_id"]}))
elif args[1] == "write":
    if os.environ.get("FAKE_FAIL_STAGE") == "writer_write":
        Path(opt("--failure-out")).write_text(json.dumps({"status": "FAILED", "failure": {"category": "RATE_LIMIT"}}))
        sys.stderr.write("error: provider call failed\n")
        raise SystemExit(3)
    request = json.loads(Path(opt("--request")).read_text())
    proposals = []
    if os.environ.get("FAKE_REVIEW_ITEM"):
        proposals = [{"item_id": "hero", "factual_status": "FACTUALLY_PRESERVED", "fidelity": {"findings": []}},
                     {"item_id": os.environ["FAKE_REVIEW_ITEM"], "factual_status": "FACTUAL_REVIEW_REQUIRED",
                      "fidelity": {"findings": [{"message": '"8" counts check; the source does not say what that number counts.'}]}}]
    Path(opt("--out")).write_text(json.dumps({
        "proposals": proposals,
        "writer_proposal_id": "wp-000000000001", "request_id": request["request_id"],
        "source_idea_id": request["source_idea_id"], "factual_status": "FACTUALLY_PRESERVED",
        "execution": {"model": "fake-model"},
        "contribution": {"component": "howlwriter", "operation": "TRANSFORMED_COPY",
                         "inference_occurred": True, "proposals_from_model": 2},
    }))
