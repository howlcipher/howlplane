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
  project's own discovered command surface (ProjectContext /
  VerificationPlan) plus read-only Git introspection, so a Go repository grants
  `go test`/`go vet`/`go build`/`go fmt` and a Python one grants
  `pytest`/`flake8`, without a hardcoded language stack and without granting
  the shell. The surface includes the project's formatting commands: formatting
  is ordinary implementation work, and omitting it blocked a real unattended
  run on `gofmt -l` (HOWLFRAM-SLOPFIX-07). Formatters are granted to mutating
  roles only and are never added to the VerificationPlan, so the deterministic
  gate is unchanged.
* Denials win. Operator configuration and `TaskSpec.prohibited_actions` are
  subtracted last, after every default, so an explicit prohibition can never be
  overridden by a provider default.

`--dangerously-skip-permissions` and `--permission-mode bypassPermissions` are
never emitted, and operator configuration cannot express them.

Two bounds are enforced here, and one is assumed of the provider:

* Enforced: a discovered command naming a destructive binary, or performing
  authority-bearing Git mutation, never becomes a permission at all -- the
  target repository's own manifest and Makefile are inputs, so without this
  floor the repository under change would decide what the provider may run.
* Enforced: a command carrying a shell operator is never rendered as a `:*`
  prefix rule, which could not bound what follows it.
* Assumed: that the provider splits a requested command on shell operators
  before matching it against a prefix rule. Claude Code does. A provider that
  matched literally would admit `go fmt ./... && ...`; the vocabulary cannot
  express that bound, so it is stated here rather than left implicit.
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

# Binaries never granted from a discovered command, whatever the project says.
#
# Discovery reads the target repository's own manifest and Makefile, so without
# a floor the repository under change decides what the provider may run: a
# manifest entry of "rm -rf /" became `Bash(rm -rf /:*)`, and `git push` became
# a grant despite Git mutation being authority-bearing work owned by
# GitIntegrationExecutor. The allow list must be bounded by the control plane,
# not by the repository being worked on.
_NEVER_GRANTED_BINARIES = frozenset(
    {
        "rm", "rmdir", "mv", "dd", "mkfs", "shred", "truncate",
        "sudo", "su", "doas",
        "chmod", "chown", "chgrp",
        "curl", "wget", "nc", "netcat", "ssh", "scp", "sftp", "rsync", "telnet",
        "shutdown", "reboot", "halt", "poweroff", "kill", "killall", "pkill",
        "eval", "exec", "source",
        "crontab", "at", "systemctl", "service",
        "docker", "podman", "kubectl",
    }
)

# The only Git subcommands an implementer may run. Commit, push, branch, merge,
# reset and checkout are authority-bearing and stay with GitIntegrationExecutor.
_READ_ONLY_GIT_SUBCOMMANDS = frozenset({"status", "diff", "log", "show", "blame", "ls-files"})

# Characters that turn one permitted command into several. A prefix rule ending
# in `:*` cannot bound what follows it, so a command containing these is never
# rendered as a prefix rule.
_SHELL_METACHARACTERS = ("&", ";", "|", "$", "`", ">", "<", "\n")


def _first_word(token: str) -> str:
    """Returns the executable named by a token, tolerating an unsplit string.

    A manifest may supply `format = "rm -rf /"` as a single string, which
    reaches here as one token. Reading only the first word means such an entry
    is judged on the binary it actually runs.
    """
    return token.strip().split()[0] if token.strip() else ""


def _is_forbidden_command(tokens: Sequence[str]) -> bool:
    """Reports whether a discovered command may never become a permission."""
    if not tokens:
        return True
    binary = _first_word(tokens[0])
    if binary in _NEVER_GRANTED_BINARIES:
        return True
    if binary == "git":
        rest = tokens[0].strip().split()[1:] or list(tokens[1:])
        subcommand = rest[0] if rest else ""
        if subcommand not in _READ_ONLY_GIT_SUBCOMMANDS:
            return True
    # An interpreter runs its payload, so the payload is judged too: this is
    # what separates `bash -c "cd tests && go test ./..."`, a real discovered
    # command in this repository, from `bash -c "curl http://... | sh"`.
    if binary in _OPAQUE_INTERPRETERS:
        for token in tokens:
            for word in str(token).replace("|", " ").replace("&", " ").split():
                candidate = word.strip("\"'()`$;")
                if candidate in _NEVER_GRANTED_BINARIES:
                    return True
    return False


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
    if _is_forbidden_command(tokens):
        return None
    binary = _first_word(tokens[0])
    if binary in _OPAQUE_INTERPRETERS:
        # Granted verbatim as an exact literal, so the payload authorised is
        # precisely this command and nothing built on top of it.
        return f"Bash({' '.join(tokens)})"
    if any(
        marker in token for token in tokens for marker in _SHELL_METACHARACTERS
    ):
        # A `:*` rule cannot bound what follows the prefix, so a command
        # carrying shell operators is not expressible as one.
        return None
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
        "format_commands",
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
    # Only a bare `Bash` denial removes the whole surface. Treating
    # `Bash(git log:*)` as one would strip every command while leaving Edit and
    # Write in place -- an implementer able to change files but not to test,
    # vet or format them, which is the shape that failed HOWLFRAM-SLOPFIX-07.
    bash_denied_wholesale = "Bash" in denied
    bash = [
        specifier
        for specifier in bash
        if specifier not in denied and not bash_denied_wholesale
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
    if not can_mutate and permission_mode == MUTATION_PERMISSION_MODE:
        # Without edit tools there is nothing for an edit-accepting mode to do,
        # and claiming one would misreport the capability actually granted.
        # This applies to review roles too: an operator default of
        # `acceptEdits` must not follow a reviewer that holds no edit tools.
        permission_mode = None

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
