# TUI-00 feasibility probes

Probes for the interactive terminal feasibility gate. They are evidence, not a
product: nothing here is wired into the `howlplane` CLI. The findings and the
recommendation are in
[`documentation/journals/2026-11-22_tui00_feasibility_gate.md`](../../../documentation/journals/2026-11-22_tui00_feasibility_gate.md).

| File | Purpose |
| --- | --- |
| `tui_core.howl` | pure module: objective validation, inventory and route summaries, failure explanation, view lines |
| `tui_reducer.howl` | one `howlplane.tui_step/v0` step on stdin, one `howlplane.tui_step_result/v0` document on stdout; needs no capability |
| `exec_boundary_probe.howl` | calls an execution boundary through `exec` under exact argv grants |
| `fake_howlplane.sh` | deterministic fake of `providers`, `route` and `work`; never contacts a provider; logs every argv |
| `fixtures/` | documents captured from HowlPlane `e175e67` in a sandbox (`work_result_*` come from the real serializer fed a stub result) |
| `reproducers/` | minimal reproducers for HowlFrame defects HF-D1 to HF-D4 |

Run the checks serially:

```bash
HOWLFRAME_BIN=/path/to/howlframe pytest tests/test_tui00_feasibility.py -p no:randomly
```

Tests build bytecode into a temporary directory; no `.hfbc` is committed here.
