#!/usr/bin/env python3
"""Install a user unit for the existing factory; never grant campaign authority."""

import argparse
import os
from pathlib import Path
import sys


def unit_quote(value):
    """Quote one systemd value, preserving literal specifiers and whitespace."""
    value = str(value)
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ValueError("Control characters are not valid service arguments")
    return '"' + value.replace('\\', '\\\\').replace('"', '\\"').replace('%', '%%') + '"'


def install(args):
    checkout = Path(args.checkout).resolve()
    target = Path(args.target_repo).resolve()
    state = Path(args.state_dir).resolve()
    python = Path(args.python).absolute()
    if not (checkout / "src/howlplane/control_plane/cli.py").is_file() and not (checkout / "src/control_plane/cli.py").is_file():
        raise ValueError("Checkout must contain src/howlplane/control_plane/cli.py")
    if target == checkout or checkout in target.parents or target in checkout.parents:
        raise ValueError("Use an isolated target checkout outside the service controller")
    if not (target / ".git").exists():
        raise ValueError("Target must be a Git checkout or worktree")
    if not python.is_file() or not os.access(python, os.X_OK):
        raise ValueError("Python must be an executable absolute path")
    command = [str(python), "-m", "howlplane.control_plane.cli", "factory", "run",
               "--state-dir", str(state), "--target-repo", str(target), "--resume-stopped"]
    queue_file = getattr(args, "queue_file", None)
    mission_contract = getattr(args, "mission_contract", None)
    if bool(queue_file) != bool(mission_contract):
        raise ValueError("finite mission service requires both --queue-file and --mission-contract")
    if mission_contract:
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
        from howlplane.control_plane.factory.mission_binding import MissionBinding
        from howlplane.control_plane.factory.task_queue import load_queue
        binding = MissionBinding.load(Path(mission_contract), Path(queue_file))
        if binding.workspace != state:
            raise ValueError("mission workspace must equal service --state-dir")
        tasks = load_queue(Path(queue_file).resolve(), target, document=binding.queue_document)
        if not tasks or set(t.id for t in tasks) != set(binding.contract["tasks"]):
            raise ValueError("finite service tasks must exactly match the pinned mission mapping")
        command = [str(python), "-m", "howlplane.control_plane.cli", "factory", "queue",
                   str(Path(queue_file).resolve()), "--repo", str(target),
                   "--mission-contract", str(Path(mission_contract).resolve()),
                   "--max-tasks", str(len(tasks)), "--stop-on-failure", "--json"]
        for task in tasks:
            command.extend(["--retry", task.id])
    # WorkingDirectory is a path directive, not an Exec argument list: quotes
    # are literal there and turn an absolute path into an invalid relative one.
    unit_quote(checkout)
    unit = "\n".join([
        "[Unit]", "Description=HowlPlane persistent factory",
        "StartLimitIntervalSec=300", "StartLimitBurst=5", "",
        "[Service]", "Type=simple",
        "WorkingDirectory=" + str(checkout).replace('%', '%%'),
        "Environment=" + unit_quote(
            "PYTHONPATH=" + os.pathsep.join([str(checkout / "src"), str(checkout)])
        ),
        "Environment=" + unit_quote("PATH=" + args.worker_path),
        "Environment=PYTHONUNBUFFERED=1",
        "Environment=HOWL_FORBID_LOCAL_INFERENCE=1",
        "ExecStart=:" + " ".join(unit_quote(arg) for arg in command),
        "Restart=on-failure", "RestartPreventExitStatus=1 78" if mission_contract else "RestartPreventExitStatus=",
        "RestartSec=30", "KillMode=mixed",
        "TimeoutStopSec=300", "UMask=0077",
        "StandardOutput=journal", "StandardError=journal",
        "SyslogIdentifier=howlplane-factory",
        "LogRateLimitIntervalSec=30", "LogRateLimitBurst=200",
        "", "[Install]", "WantedBy=default.target", "",
    ])
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    try:
        with output.open("x", encoding="utf-8") as stream:
            stream.write(unit)
    except FileExistsError:
        if output.read_text(encoding="utf-8") != unit:
            raise ValueError(f"Refusing to overwrite a different unit: {output}")
    return output


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--checkout", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--python", type=Path, default=Path(sys.executable))
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--target-repo", type=Path, required=True)
    parser.add_argument("--queue-file", type=Path, help="Finite existing-mission native queue")
    parser.add_argument("--mission-contract", type=Path, help="Reviewed pinned mission binding")
    config = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    parser.add_argument("--output", type=Path,
                        default=config / "systemd/user/howlplane-factory.service")
    parser.add_argument("--worker-path", default=os.environ.get("PATH", "/usr/bin:/bin"))
    try:
        output = install(parser.parse_args())
    except (OSError, ValueError) as exc:
        parser.exit(1, f"Service installation failed: {exc}\n")
    print(f"Installed {output}")
    print("Run systemctl --user daemon-reload, then systemctl --user enable --now howlplane-factory")


if __name__ == "__main__":
    main()
