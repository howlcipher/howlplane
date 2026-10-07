Ecosystem mission (HowlDream -> HowlWriter -> HowlCreate via `howlplane creative run`):
Objective: "A landing page for linkcheck, an open-source command-line link checker, aimed at documentation teams who want broken links caught in CI."
Constraints: no claims about features linkcheck does not have; static site only, no JavaScript framework.
Copy spec: copy-spec.json (hero, subhead, three feature cards, each with canonical evidence from linkcheck's README and factual constraints: defaults, exit codes, retry policy).
Provider: the user's reviewed remote profile remote.json (claude -p, no tools, safe mode).
Expected: a materialized static landing page in the sandbox whose copy is factually faithful to the evidence, plus contribution-audit.json tracing Dream -> Writer -> Create.
