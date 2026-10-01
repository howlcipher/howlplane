# Vendored HowlInstinct contract

This directory holds a copy of the HowlInstinct decision receipt schema, used
by HowlPlane to validate receipts before trusting any semantic judgment.

- Repository: howlcipher/howlInstinct
- Source path: schemas/decision-receipt.schema.json
- Pinned commit: 894df82eb6300f4636352e5cc0b8f4ea77013d74 (branch claude/howlinstinct-howlplane-integration-y2risp)
- Schema id: howlinstinct.decision_receipt/v1
- Vendored: 2026-10-01

Re-vendor steps: copy the file from the pinned upstream path over
howlinstinct.decision_receipt.v1.schema.json, update the commit above, and run
tests/test_semantic_recommendation.py. A new schema id upstream means a new file
here, never an in place edit. Do not modify the copy by hand.
