shopcalc/pricing.py has no tests, and we need to refactor it next quarter. Before anyone touches it, add a characterization test suite that pins down its current behaviour exactly.

Requirements:
- Do not change shopcalc/pricing.py at all. This task is tests and documentation only.
- Use the standard library `unittest` (no new dependencies), runnable with `python3 -m unittest discover -s tests -t .` from the repository root.
- Cover every public function and the rules they encode: line rounding, tier boundaries, promo codes (case and whitespace handling, date windows including the boundary days, unknown codes), how tier and promo discounts combine, shipping methods and the free-shipping threshold, tax by state including unlisted states, and the totals that `price_order` returns. Include error cases.
- Tests must be deterministic: never depend on today's date.
- Where current behaviour looks like a bug or is surprising, do not fix it. Pin the current behaviour in a test, and list each such case with a short explanation in TESTING_NOTES.md so the team can decide later.
- Update README.md with how to run the tests.
