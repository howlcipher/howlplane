# Project Manifest Specification (`.ai-project.toml`, schema_version 1)

This document is the canonical, human-readable specification for the portable
project manifest. It is the reference a project owner reads when writing or
reviewing a `.ai-project.toml` file.

The architectural vision for the manifest lives in
`documentation/AI_FRAMEWORK_BLUEPRINT.md` sections 7, 7.1, 7.2, and 10.1. This
document narrows that vision to a single artifact: the file format and the rules
a manifest must satisfy to validate.

Machine-readable counterparts:

| Artifact | Path |
| --- | --- |
| Manifest JSON Schema | `schemas/ai-project.schema.json` |
| Capability enum JSON Schema | `schemas/capability.schema.json` |
| Go parser and validator | `internal/project/manifest.go` |
| Validation command | `howlplane project validate [path]` (`pkg/cli`) |
| Example manifests | `examples/manifests/*.toml` |

If this document and `schemas/*.json` ever disagree, the schemas are
authoritative and this document is a bug.

---

## 1. Purpose

A project becomes "adopted" by the framework by committing one file to its
repository root. That file declares, in a provider-neutral way:

- what the project is (name, type keywords),
- which framework skills apply to it,
- which files belong in agent context and which must never be read,
- the canonical commands for building, testing, and linting it,
- the security capabilities the project grants to agents working in it,
- the project's provider preferences per task type.

The manifest is deliberately declarative. It contains no executable logic, no
absolute paths, and no machine-local details, so the same file works on every
machine and with every supported agent CLI.

## 2. File location and lifecycle

- **Path:** `.ai-project.toml`, at the repository root.
- **Format:** TOML.
- **Committed:** yes. The manifest is reviewable project source, not generated
  state. The framework may infer an initial manifest during adoption, but the
  committed file remains the reviewed source of truth.
- **Discovery:** tooling walks upward from the working directory to locate the
  nearest ancestor containing `.ai-project.toml`; that directory is the
  repository root, and every relative path in the manifest resolves against it.
- **Not the manifest:** generated links, agent-specific command shims, indexes,
  and caches produced during adoption are separate artifacts. They live outside
  the repository or are Git-ignored, and repositories must not commit absolute
  symlinks into a local checkout of the framework.

---

## 3. Field reference

Field types and requiredness below match `schemas/ai-project.schema.json`
exactly.

### 3.1 Top level

| Field | Type | Required | Semantics |
| --- | --- | --- | --- |
| `schema_version` | integer, constant `1` | **Yes** | Version of this manifest contract. Any value other than `1` fails validation. |
| `name` | string | **Yes** | The project's name. Used to label runs, scope context indexes, and namespace state. Must be non-empty. |
| `project_type` | array of strings | No | Free-form keywords classifying the project, for example `["go", "service", "backend"]`. Used as routing and skill-selection hints; there is no closed vocabulary. |
| `skills` | array of strings | No | Names of global framework skills relevant to this project, matching directory names under `.agents/skills/`, for example `["software_development", "network_engineering"]`. |
| `context` | table | No | Context inclusion rules. See 3.2. |
| `commands` | table | No | Named project commands. See 3.3. |
| `security` | table | No | Capability grants. See 3.4. |
| `routing` | table | No | Provider preferences per task type. See 3.5. |

Unknown top-level fields are **allowed**. The schema sets
`additionalProperties: true` at the top level and the parser does not reject
unrecognized keys, so a manifest written against a later revision still loads
under schema_version 1 tooling. Unknown keys are ignored, not acted upon.

### 3.2 `[context]`

| Field | Type | Required | Semantics |
| --- | --- | --- | --- |
| `include` | array of strings | No | Glob patterns, relative to the repository root, naming files eligible for agent context. |
| `exclude` | array of strings | No | Glob patterns, relative to the repository root, naming files that must never enter context. |

`[context]` is a closed table: `additionalProperties` is `false`, so any key
other than `include` and `exclude` fails validation.

Semantics:

- `exclude` is a deny rule and wins over `include`. A file matched by both is
  excluded.
- Omitting `include` does not mean "include everything"; it means the project
  states no project-level preference and framework defaults apply.
- Secret-bearing files are excluded explicitly here or by global defaults.
  Listing `.env`, `*.db`, credential directories, and log directories in
  `exclude` is the expected practice.
- Patterns are matched against repository-relative paths. `**` matches across
  directory separators; `*` matches within a single path segment.

### 3.3 `[commands]`

A table whose keys are command names chosen by the project and whose values are
arrays of strings.

| Field | Type | Required | Semantics |
| --- | --- | --- | --- |
| *(any key)* | array of strings | No | One command, expressed as an argv array: program first, then each argument as its own element. |

Conventional keys are `test`, `build`, `lint`, and `typecheck`, but the key set
is open — a project may define any name.

Commands are argv arrays, never shell strings, because they are executed
directly without an intervening shell. `["go", "test", "./..."]` is valid;
`"go test ./..."` is not. This is a security property, not a style preference:
no shell means no word splitting, no glob expansion, and no operator
interpretation of model-produced arguments.

### 3.4 `[security]`

| Field | Type | Required | Semantics |
| --- | --- | --- | --- |
| `capabilities` | array of strings | No | Capabilities the project grants, each drawn from the enum in `schemas/capability.schema.json`. See section 4. |

`[security]` is a closed table: `additionalProperties` is `false`.

Semantics:

- Capabilities are grants, not requests. Anything not listed is not granted.
- Omitting `[security]` entirely grants no project-level capabilities; the
  agent operates under framework defaults only.
- A project may **tighten** global security policy. It may not silently weaken
  a global policy that is marked non-overridable.
- No model may add capabilities to this list on its own behalf. Changes are
  human edits to a committed file.

### 3.5 `[routing]`

A table whose keys are task types and whose values are ordered arrays of
provider names.

| Field | Type | Required | Semantics |
| --- | --- | --- | --- |
| *(any key)* | array of strings | No | Ordered provider preference for that task type; earlier entries are preferred. |

Conventional task-type keys are `implementation`, `review`, and `research`. The
key set is open.

Entries are **preferences, not executable definitions**. They name providers the
framework already knows how to run; the manifest never specifies binaries,
flags, model IDs, or credentials. A named provider that is unavailable is
skipped in favor of the next entry.

---

## 4. Capability vocabulary

Every string in `security.capabilities` must appear in the enum in
`schemas/capability.schema.json`. The vocabulary follows a `family:level`
pattern, where levels ascend from `none` (nothing granted) through progressively
broader grants, with `user_approved` meaning "not granted by policy; permitted
only after an explicit human approval at the moment of use".

### filesystem

| Capability | Grants |
| --- | --- |
| `filesystem:none` | No file access at all. |
| `filesystem:repository` | Read and write within the repository root only; paths escaping the root are refused. |
| `filesystem:explicit_paths` | Access limited to paths the project or invocation names explicitly, rather than the whole repository. |
| `filesystem:user_approved` | Access outside the above scopes only after the user approves the specific path at the time of access. |

### network

| Capability | Grants |
| --- | --- |
| `network:none` | No outbound network access. |
| `network:public` | Outbound requests to public internet hosts, subject to host and redirect validation. |
| `network:allowlist` | Outbound requests only to a configured set of allowed hosts. |
| `network:user_approved` | Outbound requests only after the user approves the specific host at the time of the request. |

### process

| Capability | Grants |
| --- | --- |
| `process:none` | No subprocess execution. |
| `process:test_only` | Execution restricted to the project's test command. |
| `process:project_commands` | Execution restricted to the commands declared in `[commands]`, as argv arrays. |
| `process:user_approved` | Execution of a command outside `[commands]` only after explicit user approval of that command. |

### browser

| Capability | Grants |
| --- | --- |
| `browser:none` | No browser automation. |
| `browser:read_only` | Navigation and page reading; no clicks, form fills, or state-changing interaction. |
| `browser:project` | Full browser interaction scoped to the project's own hosts and flows. |
| `browser:user_approved` | Browser interaction beyond the project scope only after explicit user approval. |

### git

The `git` family is enumerated by operation rather than by scope level; grants
are cumulative in practice, so a project granting `git:commit` normally also
grants `git:read` and `git:edit`.

| Capability | Grants |
| --- | --- |
| `git:read` | Inspect history, status, diffs, and branches. |
| `git:edit` | Modify the working tree and stage changes. |
| `git:commit` | Create commits. Requires explicit intent; edit mode additionally requires a clean worktree unless explicitly overridden. |
| `git:push` | Publish commits to a remote. The highest-risk git grant; requires explicit intent. |

### database

| Capability | Grants |
| --- | --- |
| `database:none` | No database access. |
| `database:project` | Access limited to the project's own configured databases. |
| `database:user_approved` | Access to any other database only after explicit user approval. |

### secrets

| Capability | Grants |
| --- | --- |
| `secrets:none` | No access to secret material. |
| `secrets:named_reference` | Reference a secret by name so the runtime can inject it; the secret's value is never exposed to the model and is redacted from diagnostics. |

There is no capability that reveals raw secret values to a model. That is
intentional and is not an omission to be filled in a later schema version.

---

## 5. Validation rules

A manifest is valid when all of the following hold. Rules 1 through 3 are
enforced by both the JSON Schema and the Go validator; rules 4 through 6 are
structural rules enforced by the Go validator in `internal/project/manifest.go`,
which the schema alone cannot express.

1. **`schema_version` must equal `1`.** The field is required and must be the
   TOML integer `1`. A string (`"1"`), a fractional float, or any other integer
   fails validation. (`1.0` is accepted at the JSON Schema layer, which treats a
   zero-fraction float as an integer; the TOML decoder rejects it when binding
   to the integer field.)
2. **`name` is required and must be a non-empty string.** An absent `name`, or
   `name = ""`, fails validation.
3. **Every capability must be from the known enum.** Each string in
   `security.capabilities` must appear verbatim in
   `schemas/capability.schema.json`. Unknown capabilities fail validation —
   they are never ignored, downgraded, or treated as `none`. This makes typos
   like `filesystem:repo` a hard error instead of a silent loss of access.
4. **`context.include` and `context.exclude` entries must be relative,
   repository-contained paths.** An entry fails validation if it:
   - begins with `/` or `\` (absolute or root-anchored path),
   - begins with a Windows drive letter such as `C:\` or `C:/` — recognized
     regardless of the validating host's OS, since manifests are portable,
   - begins with `~` (home-directory expansion), or
   - escapes the repository root through `..` traversal, whether leading
     (`../secrets/**`) or embedded (`src/../../etc/**`).

   Traversal is judged on the resolved path, so a pattern containing `..` that
   still resolves inside the repository is acceptable, while any pattern
   resolving outside it is not. This keeps the manifest portable across
   machines and enforces the repository boundary at configuration time rather
   than at access time.
5. **`commands` entries must be non-empty argv arrays.** Each value must be an
   array of at least one string; the first element is the program. A single
   shell string such as `test = "go test ./..."` fails validation, as does an
   empty array and any individual empty-string argument.
6. **Command arguments must not contain shell metacharacters.** Because
   commands run without a shell, metacharacters would be passed through as
   literal argument text rather than interpreted — silently doing the wrong
   thing — and their presence signals a command that was written expecting shell
   semantics. Arguments containing `;`, `|`, `&&`, `||`, a backtick, or `$(`
   fail validation. A project that genuinely needs shell composition should
   move that logic into a checked-in script and invoke the script as the argv
   array, for example `["bash", "scripts/ci.sh"]`.
7. **Unknown top-level fields are allowed.** The parser does not reject them.
   This is deliberate forward compatibility: a manifest that adds fields from a
   future schema revision still loads and validates under schema_version 1
   tooling, which ignores what it does not recognize. Note that this tolerance
   is top-level only — `[context]` and `[security]` are closed tables and
   reject unknown keys.

### 5.1 Running validation

```bash
howlplane project validate            # validate the manifest for the current repository
howlplane project validate ./some/dir # discover the root upward from a given directory
```

The command discovers the repository root, loads `.ai-project.toml`, applies
every rule above, and exits non-zero with a diagnostic on the first failure.

---

## 6. Annotated example

The following is `examples/manifests/go-service.toml` with commentary. Every
other file under `examples/manifests/` — `generic-ai.toml`, `python-tool.toml`,
`typescript-web.toml` — is a valid manifest as well and shows the same fields
shaped for a different stack.

```toml
# Required. Must be the integer 1 for this contract.
schema_version = 1

# Required, non-empty. Labels runs and scopes this project's context index.
name = "go-microservice"

# Optional classification keywords. Free-form; hints for skill and provider selection.
project_type = ["go", "service", "backend"]

# Optional. Names of global framework skills under .agents/skills/ that apply here.
skills = ["software_development", "network_engineering"]

[context]
# Repository-relative globs eligible for agent context. No leading "/", no "~",
# no drive letters, and nothing that resolves outside the repository root.
include = ["cmd/**/*.go", "internal/**/*.go", "pkg/**/*.go", "go.mod"]

# Deny rules; they win over include. Vendored and build output carry no
# reviewable signal, so keeping them out preserves the context budget.
# Secret-bearing paths belong here too.
exclude = ["vendor/**", "bin/**"]

[commands]
# Argv arrays, never shell strings: the program first, then one element per
# argument. These run without a shell, so no metacharacters are permitted.
test = ["go", "test", "./..."]
build = ["go", "build", "-o", "bin/service", "./cmd/service"]

[security]
# Grants, not requests. Anything absent is not granted.
#   filesystem:repository     -> read/write inside the repo root only
#   network:public            -> outbound requests to public hosts (module downloads)
#   process:project_commands  -> may run only the [commands] entries above
# Note what is absent: no git:commit, no git:push, no secrets:*, no browser:*.
capabilities = ["filesystem:repository", "network:public", "process:project_commands"]

[routing]
# Ordered provider preferences per task type. Preferences, not executables:
# unavailable providers are skipped in favor of the next entry.
implementation = ["codex", "claude"]
```

---

## 7. Non-goals for v1

This specification covers the `.ai-project.toml` file format and the rules a
manifest must satisfy to validate. It deliberately does not cover:

- **`ai adopt` generation behavior** — how an initial manifest is inferred, and
  which shims, links, indexes, and caches adoption writes. Section 7.2 of the
  blueprint sketches the intent; the behavior is specified separately.
- **Provider execution** — how a provider named in `[routing]` is discovered,
  launched, health-checked, or degraded, and how its failures are classified.
- **Routing resolution logic** — how a `[routing]` preference list combines with
  global configuration, availability, and cost or capability constraints to
  select a provider for a given run.
- **Capability enforcement at runtime** — how a granted capability is mediated,
  when an approval prompt is raised, and how approvals are audited. This
  document defines what each capability string means; the enforcement mechanism
  is out of scope.

Those behaviors may constrain future schema revisions, but they do not change
what a valid schema_version 1 manifest looks like.
