"""provider_execution_profile.py

Translates the capability HowlPlane has already granted a task into the
per-invocation tool permissions a headless provider CLI needs to act on it.

This is not a second authority system. HowlPlane's authority layer decides
*whether* a task may change a repository; this module only expresses that
decision in the vocabulary of a provider's command line, per invocation, so an
unattended run is not silently reduced to reasoning with no repository delta.

Motivation: in the HOWLFRAM-SLOPFIX-04 external canary the Claude Code
invocation was a bare `claude -p <prompt>`. It correctly diagnosed the defect,
then reported that every Bash attempt "requires approval", edited nothing, and
exited 0 -- which the orchestrator recorded as a successful implementation.

Two properties are load-bearing:

* Bash is bounded, never blanket. Permitted commands are derived from the
  project's own discovered verification surface (ProjectContext /
  VerificationPlan) plus read-only Git introspection, so a Go repository grants
  `go test`/`go vet`/`go build` and a Python one grants `pytest`/`flake8`,
  without a hardcoded language stack and without granting the shell.
* Denials win. Operator configuration and `TaskSpec.prohibited_actions` are
  subtracted last, after every default, so an explicit prohibition can never be
  overridden by a provider default.

`--dangerously-skip-permissions` and `--permission-mode bypassPermissions` are
never emitted, and operator configuration cannot express them.
"""

from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

# Tool names a provider needs to read a repository without changing it.
READ_ONLY_TOOLS: Tuple[str, ...] = ("Read", "Glob", "Grep")

# Additional tools a provider needs to produce a repository delta.
MUTATION_TOOLS: Tuple[str, ...] = ("Edit", "Write")

# Git introspection an implementer needs to understand its own diff. Commit,
# push, branch, and merge are deliberately absent: those are authority-bearing
# actions owned by GitIntegrationExecutor under the authority envelope.
READ_ONLY_GIT_SPECIFIERS: Tuple[str, ...] = (
    "Bash(git status:*)",
    "Bash(git diff:*)",
    "Bash(git log:*)",
    "Bash(git show:*)",
)

# Permission modes an operator may select. `bypassPermissions` is intentionally
# absent -- HowlPlane keeps explicit capability boundaries.
ALLOWED_PERMISSION_MODES: Tuple[str, ...] = ("acceptEdits", "plan", "manual", "dontAsk")

# The mode that lets a headless run apply edits without an interactive prompt.
MUTATION_PERMISSION_MODE = "acceptEdits"

# Roles that are allowed to change the repository.
MUTATING_ROLES: Tuple[str, ...] = ("implementation", "remediation")

# Interpreters whose first argument is a script or inline program rather than a
# subcommand. Reducing these to `Bash(<binary>:*)` would grant the shell, so
# their commands are granted as exact literals instead.
_OPAQUE_INTERPRETERS = frozenset({"bash", "sh", "zsh", "python", "python3", "make"})


def is_mutating_role(role: Optional[str]) -> bool:
    """Returns True when `role` is permitted to produce a repository delta."""
    if not role:
        return False
    if role.endswith("-reviewer") or role == "review":
        return False
    return role in MUTATING_ROLES


def command_to_bash_specifier(command: Sequence[str]) -> Optional[str]:
    """Renders one argv command as the narrowest safe Bash permission rule.

    `["go", "test", "./..."]` becomes `Bash(go test:*)`, permitting any
    arguments to that subcommand but nothing else. Commands whose first token
    is an interpreter are granted verbatim, because a prefix rule for `bash` or
    `make` would grant arbitrary execution.
    """
    tokens = [str(token) for token in command if str(token).strip()]
    if not tokens:
        return None
    binary = tokens[0]
    if binary in _OPAQUE_INTERPRETERS:
        return f"Bash({' '.join(tokens)})"
    if len(tokens) > 1 and not tokens[1].startswith("-"):
        return f"Bash({binary} {tokens[1]}:*)"
    return f"Bash({binary}:*)"


def _iter_project_commands(project_context: Any) -> Iterable[Sequence[str]]:
    """Yields every argv command a ProjectContext discovered for this project."""
    if project_context is None:
        return
    for attribute in (
        "test_commands",
        "build_commands",
        "lint_commands",
        "hygiene_commands",
    ):
        for command in getattr(project_context, attribute, None) or []:
            if isinstance(command, str):
                yield command.split()
            else:
                yield command


def _iter_verification_commands(verification_plan: Any) -> Iterable[Sequence[str]]:
    """Yields every argv command a VerificationPlan intends to execute."""
    if verification_plan is None:
        return
    for step in getattr(verification_plan, "steps", None) or []:
        command = getattr(step, "command", None)
        if not command:
            continue
        yield command.split() if isinstance(command, str) else command


@dataclass(frozen=True)
class ProviderExecutionProfile:
    """The bounded tool capability granted to one provider invocation."""

    role: str
    tools: Tuple[str, ...] = ()
    bash_specifiers: Tuple[str, ...] = ()
    disallowed_tools: Tuple[str, ...] = ()
    permission_mode: Optional[str] = None
    mutation_capable: bool = False
    derivation: Dict[str, Any] = field(default_factory=dict)

    def allowed_tools(self) -> Tuple[str, ...]:
        """Returns every entry for a provider's allow list, tools then commands."""
        return self.tools + self.bash_specifiers


def _operator_value(operator_settings: Any, name: str, default):
    """Reads one execution-profile override off an operator settings object."""
    profile = getattr(operator_settings, "execution_profile", None)
    if profile is None:
        return default
    value = getattr(profile, name, None)
    return default if value in (None, [], ()) else value


def build_execution_profile(
    role: str,
    task: Any = None,
    project_context: Any = None,
    verification_plan: Any = None,
    operator_settings: Any = None,
) -> ProviderExecutionProfile:
    """Derives the tool permissions for one provider invocation.

    Args:
        role: The role being executed. Only mutating roles receive edit tools.
        task: TaskSpec whose `prohibited_actions` subtract from the allow set.
        project_context: ProjectContext supplying discovered project commands.
        verification_plan: VerificationPlan supplying the commands the task will
            actually be verified against.
        operator_settings: ProviderResourceSettings carrying operator overrides.
    """
    mutating = is_mutating_role(role)

    tools: List[str] = list(READ_ONLY_TOOLS)
    bash: List[str] = []
    derived_from: List[str] = []

    if mutating:
        tools.extend(MUTATION_TOOLS)
        for command in _iter_project_commands(project_context):
            specifier = command_to_bash_specifier(command)
            if specifier and specifier not in bash:
                bash.append(specifier)
                derived_from.append("project_adapter")
        for command in _iter_verification_commands(verification_plan):
            specifier = command_to_bash_specifier(command)
            if specifier and specifier not in bash:
                bash.append(specifier)
                derived_from.append("verification_plan")
        for specifier in READ_ONLY_GIT_SPECIFIERS:
            if specifier not in bash:
                bash.append(specifier)
        for extra in _operator_value(operator_settings, "extra_allowed_bash", []):
            specifier = extra if extra.startswith("Bash(") else f"Bash({extra})"
            if specifier not in bash:
                bash.append(specifier)
                derived_from.append("operator")

    # Denials are applied last so nothing can override an explicit prohibition.
    denied = list(_operator_value(operator_settings, "disallowed_tools", []))
    for prohibited in getattr(task, "prohibited_actions", None) or []:
        if prohibited not in denied:
            denied.append(prohibited)

    denied_names = {entry.split("(", 1)[0] for entry in denied}
    tools = [tool for tool in tools if tool not in denied and tool not in denied_names]
    bash = [
        specifier
        for specifier in bash
        if specifier not in denied and "Bash" not in denied_names
    ]

    permission_mode = _operator_value(operator_settings, "permission_mode", None)
    if permission_mode is not None and permission_mode not in ALLOWED_PERMISSION_MODES:
        raise ValueError(
            f"Unsupported permission mode {permission_mode!r}; "
            f"HowlPlane permits {ALLOWED_PERMISSION_MODES}."
        )
    if permission_mode is None and mutating:
        permission_mode = MUTATION_PERMISSION_MODE

    can_mutate = any(tool in tools for tool in MUTATION_TOOLS)
    if not can_mutate:
        # Without edit tools there is nothing for an edit-accepting mode to do,
        # and claiming one would misreport the capability actually granted.
        permission_mode = None if mutating else permission_mode

    return ProviderExecutionProfile(
        role=role,
        tools=tuple(tools),
        bash_specifiers=tuple(bash),
        disallowed_tools=tuple(denied),
        permission_mode=permission_mode,
        mutation_capable=can_mutate,
        derivation={
            "role_is_mutating": mutating,
            "bash_sources": sorted(set(derived_from)),
            "denied_applied": tuple(denied),
        },
    )
