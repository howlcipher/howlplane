# run-033 independent verification (operator, 2026-10-04)
Target dogfood-missions/run-033/invoicegen (clone of c03d40d + identical uncommitted WIP).
- User WIP: templates/footer.txt and notes/todo.md sha256 unchanged; nothing committed/stashed.
- `node --test`: 59 pass, 0 fail. `process.env` read only in src/config.js.
- Env-only compatibility (INVOICE_* cleared except those listed; original code from `git archive c03d40d` with the same footer): byte-identical stdout/stderr/exit for defaults, EUR/Initech/0.2, empty INVOICE_COMPANY, INVOICE_OUT_DIR (written files identical). Previously-invalid env values (tax `abc`, `1.5`, currency `eur`) now exit 2: required by the mission's validation clause and stated in README.
- Config file: discovered .invoicegen.json applied; env beats file; --config replaces discovery; relative outDir resolved against the file's dir (cfgdir/out); missing discovered file ignored; malformed discovered file exit 2.
- Validation (all exit 2, empty stdout, no output dir created): taxRate 2, currency usd, company "", unknown key, malformed JSON, non-object, unreadable --config. `{"outDir":42}` alone -> exit 2; with INVOICE_OUT_DIR overriding it -> accepted (final-configuration rule, documented). 
- README accurate and explicit about precedence, empty env values, and the validation behavior change.
- Leak scan: none.
- Operator note: a first env-compat comparison using `env -i` was invalid (host `node` is a distrobox shim needing env); redone with `env -u`.
Verdict: CLEAN.
