Hermetic stand-ins for the HowlDream, HowlWriter and HowlCreate CLIs used by
`tests/test_creative_pipeline.py`. They emit contract-shaped JSON so the
orchestrator is tested without the real components or any provider. The
real components are exercised by the native pipeline acceptance run.
`FAKE_FAIL_STAGE` makes the named stage exit non-zero.
