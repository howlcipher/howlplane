# run-034 independent verification (operator, 2026-10-04)
Target dogfood-missions/run-034/shopcalc, base ffd7bb4.
- shopcalc/pricing.py sha256 unchanged (hard constraint met). Changes: README.md (run command, link to notes), new TESTING_NOTES.md (15 entries), tests/__init__.py, tests/test_pricing.py (319 lines).
- `python3 -m unittest discover -s tests -t .`: 91 tests OK. No test reads the real clock (no date.today/now/time.time; the `today=None` fallback is exercised through a stub).
- Mutation testing (15 hand-written mutants of pricing.py: tier `>`->`>=`, line rounding mode, discount stacking, free-shipping comparison and base, fallback tax rate, promo window start/end boundaries, promo strip/upper normalisation, quantity guard, express price, tax base incl. shipping, tier threshold, discount sign): 15/15 killed.
- TESTING_NOTES.md: each entry is real current behaviour (spot-checked: exclusive tier boundaries, mixed rounding, non-stacking discounts, promo on original subtotal, discounts can re-add shipping, case-sensitive state with 6% fallback).
- Leak scan: none.
- DOG-027 live: the reviewer stated "shopcalc/pricing.py is not in the diff. Only README.md, TESTING_NOTES.md, tests/__init__.py and tests/test_pricing.py changed or were added" (previous reviewers could not see the diff).
Verdict: CLEAN.
