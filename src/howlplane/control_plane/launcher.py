#!/usr/bin/env python3
"""
launcher.py

Thin backward-compatibility launcher wrapper for the HowlPlane control plane.
Canonical CLI logic and entry points reside in howlplane.control_plane.cli.
"""

import os
import sys

from howlplane.control_plane import __version__
from howlplane.control_plane.evidence_ledger import EvidenceEntry, EvidenceLedger
from howlplane.control_plane.synthesis.provider_pool import ProviderPoolManager
from howlplane.control_plane.task_spec import TaskSpec
from howlplane.control_plane.cli import (
    ControlPlaneError,
    TargetRepositoryNotFoundError,
    ControlPlaneNotFoundError,
    DEFAULT_INSTRUCTION_BUDGET,
    find_git_repo_root,
    find_control_plane_root,
    infer_task_metadata,
    format_agent_launch_command,
    create_task_plan,
    cmd_work,
    cmd_route,
    cmd_providers,
    cmd_doctor,
    cmd_status,
    cmd_init_task,
    cmd_route_task,
    cmd_briefs,
    cmd_prepare_run,
    cmd_reconcile,
    cmd_verify,
    cmd_record,
    cmd_metrics,
    cmd_boundary,
    cmd_howlframe_audit,
    cmd_approve,
    cmd_reject,
    cmd_resume,
    cmd_cancel,
    cmd_unlock,
    cmd_create,
    cmd_run_product,
    cmd_dogfood,
    cmd_acceptance,
    cmd_marathon,
    cmd_authority,
    cmd_local,
    cmd_factory,
    cmd_explore,
    cmd_trace,
    register_exploration_subparsers,
    register_synthesis_subparsers,
    register_factory_subparsers,
    build_parser,
    HANDLERS,
    ACTIONS,
    main,
    legacy_main,
)

__all__ = [
    "__version__",
    "EvidenceEntry",
    "EvidenceLedger",
    "ProviderPoolManager",
    "TaskSpec",
    "ControlPlaneError",
    "TargetRepositoryNotFoundError",
    "ControlPlaneNotFoundError",
    "DEFAULT_INSTRUCTION_BUDGET",
    "find_git_repo_root",
    "find_control_plane_root",
    "infer_task_metadata",
    "format_agent_launch_command",
    "create_task_plan",
    "cmd_work",
    "cmd_route",
    "cmd_providers",
    "cmd_doctor",
    "cmd_status",
    "cmd_init_task",
    "cmd_route_task",
    "cmd_briefs",
    "cmd_prepare_run",
    "cmd_reconcile",
    "cmd_verify",
    "cmd_record",
    "cmd_metrics",
    "cmd_boundary",
    "cmd_howlframe_audit",
    "cmd_approve",
    "cmd_reject",
    "cmd_resume",
    "cmd_cancel",
    "cmd_unlock",
    "cmd_create",
    "cmd_run_product",
    "cmd_dogfood",
    "cmd_acceptance",
    "cmd_marathon",
    "cmd_authority",
    "cmd_local",
    "cmd_factory",
    "cmd_explore",
    "cmd_trace",
    "register_exploration_subparsers",
    "register_synthesis_subparsers",
    "register_factory_subparsers",
    "build_parser",
    "HANDLERS",
    "ACTIONS",
    "main",
    "legacy_main",
]

if __name__ == "__main__":
    if os.environ.get("HOWLPLANE_LEGACY_AI") == "1":
        sys.exit(legacy_main())
    sys.exit(main())
