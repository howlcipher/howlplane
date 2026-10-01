"""Product journey: a developer operates HowlPlane through the everyday commands only.

    howlplane -> setup -> start -> status -> logs -> (owner required) approve ID
    -> work continues and completes -> status -> stop -> start

Every step is `cli.main([...])` with the repository as the working directory. The
test never passes a state directory, edits state files or calls an internal module
on the normal path. Only the operating system boundary is faked: spawning and
killing the background process (the supervisor is ticked in-process with a fake
worker), and the installed AI CLIs. Everything above that is the real product.
"""

import re

import pytest

from howlplane.control_plane.factory.work_item import WorkItemState
from tests._product_harness import product  # noqa: F401  (fixture)

pytestmark = pytest.mark.acceptance


def test_a_new_user_gets_from_clone_to_completed_work_with_everyday_commands(product):
    product.trust = "TRUST_REQUIRED"  # a fresh clone: no workspace preparation yet
    # 1. Bare howlplane explains where things stand and what to do.
    code, out, _ = product.run()
    assert code == 0
    assert "NOT READY" in out and "howlplane setup" in out
    assert "howlplane factory" not in out

    # 2. Setup prepares the repository and points at start.
    code, out, _ = product.run("setup", "--yes")
    assert code == 0 and "Repository prepared." in out and "You're ready." in out
    assert "howlplane start" in out and "howlplane factory" not in out

    code, out, _ = product.run()
    assert code == 0 and "READY" in out and "howlplane start" in out

    # 3. Start, then status says it is healthy with nothing required.
    code, out, _ = product.run("start", "--authority", "safe")
    assert code == 0 and "STARTED" in out
    assert "howlplane status" in out and "howlplane factory" not in out
    product.tick()
    code, out, _ = product.run("status")
    assert code == 0 and "IDLE" in out and "Healthy" in out and "No action required." in out

    # 4. Logs show what happened.
    code, out, _ = product.run("logs")
    assert code == 0 and "Factory process started" in out

    # 5. Work needing the owner surfaces with the exact short command, no state directory.
    item = product.seed_owner_decision()
    product.tick()
    code, out, _ = product.run("status")
    assert code == 0 and "OWNER REQUIRED" in out
    command = re.search(r"howlplane approve (\S+)", out)
    assert command and command.group(1) == item.work_item_id
    assert "--state-dir" not in out and "howlplane reject " + item.work_item_id in out

    # 6. Approving by id alone works, whatever kind of thing the id names.
    code, out, _ = product.run("approve", item.work_item_id)
    assert code == 0 and "REQUEUED" in out

    # 7. Processing continues and completes through the normal governed path.
    product.tick()
    assert product.worker_ran == [item.work_item_id]
    store = product._supervisor().work_item_store
    assert store.load(item.work_item_id).state == WorkItemState.SHIPPED
    code, out, _ = product.run("status")
    assert code == 0 and "OWNER REQUIRED" not in out and "Completed  1" in out

    # 8. Stop pauses safely and says how to continue; start continues without a resume ceremony.
    code, out, _ = product.run("stop")
    assert code == 0 and "STOPPED" in out and "howlplane start" in out
    code, out, _ = product.run("status")
    assert "STOPPED" in out and "howlplane start" in out and "howlplane factory resume" not in out
    code, out, _ = product.run("start")
    assert code == 0 and "STARTED" in out
    product.tick()
    code, out, _ = product.run("status")
    assert code == 0 and "STOPPED" not in out
