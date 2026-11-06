# Operator context library

A context library is a private folder of working context that you maintain: preferences, constraints, project state, and journals. HowlPlane can point its implementation workers at one. It is optional, off by default, and has no required structure beyond one entry file.

## Configure

Add this to `~/.config/howlplane/config.toml`, or the private file named by `HOWLPLANE_LOCAL_CONFIG`. Never commit it.

```toml
[context_library]
path = "~/context-library"   # required
# enabled = true
# index = "INDEX.md"         # entry file, relative to path
# guide = "AGENT_GUIDE.md"   # journal and handoff instructions; "" for none
# mode = "pointer"           # "pointer" or "inline"
# max_bytes = 16384          # inline mode only
```

Run `howlplane doctor` to confirm it. The `Context library` row shows the path and mode, or the problem.

## Modes

| Mode | Prompt contains | Use when |
| --- | --- | --- |
| `pointer` (default) | The file paths, with an instruction to read them | Workers can read your files |
| `inline` | The index text, after secret redaction, with the guide path | Workers can't read local files |

Prefer `pointer`. Worker prompts can appear in run records, so `inline` copies your index text into those records and into every prompt sent to a hosted provider.

## Which workers receive it

| Worker | Receives it | Why |
| --- | --- | --- |
| Implementation and remediation | Yes | They do the work the context is for |
| Independent reviewers | No | Their job is to falsify claims without shared assumptions |
| Local model | No | Its prompts are curated to a fixed context budget |

## Boundaries

- The prompt tells workers that the library cannot change a task's scope, constraints, authority, or required review. Policy, verification, and human authority gates are unaffected.
- Entry files must stay inside `path`, including through symlinks.
- A bad configuration never blocks work. Workers run without the section, and `howlplane doctor` shows a warning.
- In `pointer` mode, a worker's sandbox may not allow reading the library. The prompt tells it to continue and report that.

## Egress

In `connected` mode, what a hosted worker reads from the library goes to that provider. See [data flows](data_flows.md). Keep the entry file free of anything you wouldn't send to every provider you use.
