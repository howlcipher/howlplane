"""
test_canonical_cli.py

Tests verifying the consolidation of HowlPlane CLIs into the canonical entrypoint
howlplane.control_plane.cli (HOWL-CANON-006).
"""

from pathlib import Path
from types import SimpleNamespace
import pytest

from howlplane.control_plane import cli, launcher


@pytest.mark.unit
def test_all_overlapping_subcommands_parse_in_cli():
    """All subcommands previously handled in launcher.py must parse cleanly in cli.py."""
    parser = cli.build_parser()
    
    # 1. work
    parsed = parser.parse_args(["work", "fix a bug", "--task-id", "T-01", "--risk", "low"])
    assert parsed.subcommand == "work"
    assert parsed.objective == "fix a bug"
    assert parsed.task_id == "T-01"
    assert parsed.risk == "low"

    # 2. route
    parsed = parser.parse_args(["route", "investigate issue", "--json"])
    assert parsed.subcommand == "route"
    assert parsed.objective == "investigate issue"
    assert parsed.json is True

    # 3. providers
    parsed = parser.parse_args(["providers", "--json"])
    assert parsed.subcommand == "providers"
    assert parsed.json is True

    # 4. status
    parsed = parser.parse_args(["status", "--repo", "."])
    assert parsed.subcommand == "status"
    assert parsed.repo == "."

    # 5. doctor
    parsed = parser.parse_args(["doctor", "--repo", "."])
    assert parsed.subcommand == "doctor"
    assert parsed.repo == "."

    # 6. verify
    parsed = parser.parse_args(["verify", "--project-dir", "."])
    assert parsed.subcommand == "verify"
    assert parsed.project_dir == "."

    # 7. howlframe-audit
    parsed = parser.parse_args(["howlframe-audit", "--repo", "."])
    assert parsed.subcommand == "howlframe-audit"
    assert parsed.repo == "."

    # 8. approve
    parsed = parser.parse_args(["approve", "T-01", "--reason", "LGTM"])
    assert parsed.subcommand == "approve"
    assert parsed.task_id == "T-01"
    assert parsed.reason == "LGTM"

    # 9. reject
    parsed = parser.parse_args(["reject", "T-01", "--reason", "Needs fixes"])
    assert parsed.subcommand == "reject"
    assert parsed.task_id == "T-01"
    assert parsed.reason == "Needs fixes"

    # 10. resume
    parsed = parser.parse_args(["resume", "T-01"])
    assert parsed.subcommand == "resume"
    assert parsed.task_id == "T-01"

    # 11. cancel
    parsed = parser.parse_args(["cancel", "T-01", "--reason", "Aborted"])
    assert parsed.subcommand == "cancel"
    assert parsed.task_id == "T-01"
    assert parsed.reason == "Aborted"

    # 12. unlock
    parsed = parser.parse_args(["unlock", "T-01"])
    assert parsed.subcommand == "unlock"
    assert parsed.task_id == "T-01"


@pytest.mark.unit
def test_launcher_reexports_match_cli():
    """launcher.py must re-export all dispatch targets and main entry points."""
    assert launcher.main is cli.main
    assert launcher.legacy_main is cli.legacy_main
    assert launcher.build_parser is cli.build_parser
    assert launcher.ACTIONS is cli.HANDLERS
    assert launcher.HANDLERS is cli.HANDLERS
    assert launcher.cmd_work is cli.cmd_work
    assert launcher.cmd_route is cli.cmd_route
    assert launcher.cmd_providers is cli.cmd_providers
    assert launcher.cmd_doctor is cli.cmd_doctor
    assert launcher.cmd_status is cli.cmd_status
    assert launcher.cmd_unlock is cli.cmd_unlock


@pytest.mark.unit
def test_cli_dispatches_cleanly(monkeypatch):
    """Calling cli.main with a valid subcommand invokes its handler."""
    invoked = []

    def mock_cmd_status(args):
        invoked.append(args.subcommand)
        return 0

    monkeypatch.setitem(cli.HANDLERS, "status", mock_cmd_status)
    res = cli.main(["status", "--repo", "."])
    assert res == 0
    assert invoked == ["status"]
