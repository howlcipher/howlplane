# HowlProof result

HowlProof judges. It does not repair the artifact it is judging.

Artifact: incident-sift-operational-proof
Commit: 02f9a2c0bfeef5eb0a621324ebdf6eb884c00705
Branch: mission/incident-sift
Run: hp-20261028-152521-5f46c56f16ce
Profiles: secrets.scan, docs.claims
Completed: 2026-10-28T15:25:21.970565+00:00

## Verdict: REJECT

Evidence demonstrates that the artifact violates acceptance criteria, security or reliability requirements, policy, or another blocking condition.

Deciding rule: `blocking_evidence`

- SOURCE-SCAN violated: secrets.scan failed
- HIGH-FINDINGS violated: 2 any findings at or above HIGH, limit 0

## Acceptance criteria

0 of 2 satisfied.

| Criterion | Requirement | Outcome | Detail |
| --- | --- | --- | --- |
| SOURCE-SCAN | checks_pass | VIOLATED | secrets.scan failed |
| HIGH-FINDINGS | max_findings | VIOLATED | 2 any findings at or above HIGH, limit 0 |

## Checks

1 verified, 1 failed, 0 skipped, 0 unavailable, 0 not applicable, 0 errored.

| Check | Checker | Status | Mode | Note |
| --- | --- | --- | --- | --- |
| secrets.scan | secrets.scan/v1 | FAILED | REAL | 2 credential-shaped strings in 13 text files |
| docs.claims.absolutes | docs.claims/v1 | VERIFIED | REAL | the documentation makes no absolute claims |

## Validation modes

- **REAL**: secrets.scan, docs.claims.absolutes
- **SIMULATED**: none
- **SKIPPED**: none
- **UNAVAILABLE**: none
- **NOT_APPLICABLE**: none
- **ERROR**: none

## Findings

### HP-SEC-0001 — Credential-shaped openai_key in tests/test_cli.py

- Adversary: SECURITY
- Category: security
- Severity: BLOCKER
- Confidence: HIGH
- State: FOUND
- Blocking: no
- Location: tests/test_cli.py:122

A string matching the openai_key credential format appears at tests/test_cli.py:122. The path suggests a fixture, so this may be intentional test data.

Evidence:

```
, '[REDACTED]"', 'API-KEY hidden',
                   'sk-a...[25 chars redacted]', '[REDACTED]
```

Recommended remediation (for the artifact's owner, not for HowlProof): Remove the value from the tree, rotate it if it was ever real, and load it from the environment or a secret store instead.

Reproduction: Re-read the file and match the same pattern.

1. Search tests/test_cli.py for the openai_key pattern.

### HP-SEC-0002 — Credential-shaped aws_access_key in tests/test_cli.py

- Adversary: SECURITY
- Category: security
- Severity: BLOCKER
- Confidence: HIGH
- State: FOUND
- Blocking: no
- Location: tests/test_cli.py:123

A string matching the aws_access_key credential format appears at tests/test_cli.py:123. The path suggests a fixture, so this may be intentional test data.

Evidence:

```
pqrstuv', '[REDACTED]',
                   'AKIA...[20 chars redacted]']
        for secre
```

Recommended remediation (for the artifact's owner, not for HowlProof): Remove the value from the tree, rotate it if it was ever real, and load it from the environment or a secret store instead.

Reproduction: Re-read the file and match the same pattern.

1. Search tests/test_cli.py for the aws_access_key pattern.


## Unresolved risks

- HP-SEC-0001 (BLOCKER): A string matching the openai_key credential format appears at tests/test_cli.py:122. The path suggests a fixture, so this may be intentional test data.
- HP-SEC-0002 (BLOCKER): A string matching the aws_access_key credential format appears at tests/test_cli.py:123. The path suggests a fixture, so this may be intentional test data.

## Limitations

No skipped, unavailable or simulated checks in this run.

## Notes

- no service is declared, so live adversaries have nothing to attack

--------------------------------------------------------------------

This report is advisory evidence. HowlProof does not promote, merge, deploy or approve anything. Remediation belongs to the artifact's builder and promotion to HowlPlane and HowlChangeOps.
