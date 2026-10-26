# Vendored HowlInstinct contract

This directory holds a copy of the HowlInstinct decision receipt schema, used
by HowlPlane to validate receipts before trusting any semantic judgment.

- Repository: howlcipher/howlInstinct
- Source path: schemas/decision-receipt.schema.json
- Pinned commit: cb211db310873d754482542578c5656afc8eca68 (main; the file is unchanged since 7d81034)
- Schema id: howlinstinct.decision_receipt/v1
- Vendored: 2026-10-01; re-pinned to main 2026-10-20

Re-vendor steps: copy the file from the pinned upstream path over
howlinstinct.decision_receipt.v1.schema.json, update the commit above, and run
tests/test_semantic_recommendation.py. A new schema id upstream means a new file
here, never an in place edit. Do not modify the copy by hand.
