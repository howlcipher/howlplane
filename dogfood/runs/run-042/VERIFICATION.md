# run-042 independent verification (operator)
`howlplane creative run` exit 0 in 2.5 min; all 7 stages COMPLETED. Run dir dogfood-missions/run-042/runs/c1; site in dogfood-missions/run-042/site (index.html, styles.css, copy.json, DESIGN.md, manifest).
- Static: 0 <script> tags, no framework.
- Copy is factually faithful to the spec's evidence (exit codes 0/1/2/130, retry policy incl. 4xx never retried, 200 ms backoff, ordering); no invented features.
- Writer (claude-opus-5-5 via the reviewed remote profile, inference_occurred true) rewrote all 5 items. Its fidelity check marked the subhead FACTUAL_REVIEW_REQUIRED ("8" / "2" not tied to units), although the evidence says "-workers N (default 8) concurrent checks": conservative, safe-direction false positive (HowlWriter, P3, not pursued).
- Create marked that item usable=false and kept the original placeholder subhead. Correct and safe, but the CLI said only COMPLETED -> DOG-033.
- contribution-audit.json: lineage_intact true; Dream candidate -> Writer request/proposal -> Create development -> materialized artifacts all match.
- Copy addresses the facts but not the requested audience (documentation teams); quality modest.
Verdict: the ecosystem handoff works end to end with intact provenance; NOT CLEAN (DOG-033), and reachable only after DOG-031's repair plus DOG-032's manual workaround.
