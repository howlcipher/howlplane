# Optional HowlDream Python API

Status: retained existing integration, explicitly pinned and tested.

Plane's NativeHowlDreamProvider first imports HowlDream's ExplorationRequest
and engine.explore. It constructs the request from the governed objective and
budget, calls explore(request, root=work_dir), and serializes model_dump(). Only
ImportError selects the CLI fallback. Runtime engine errors are propagated;
there is no retry with another boundary after side effects. Availability of an
importable API is distinct from availability of an executable.

Install the optional dream-api extra to use the tested API revision:
9594c98b25df624733c9609fa5743aef091e59f5 (source version 0.4.4).
The exact pin in pyproject.toml is the installation contract. Standalone Plane
keeps Dream optional. Other installed revisions can be importable but are not
covered by this compatibility evidence; deployments should use the pinned extra.
CLI descriptors cannot verify this Python boundary.

## Alternatives and costs

Retaining imports avoids process startup and request-file overhead and preserves
existing behavior, but couples Python dependencies and requires coordinated pins.
Always using the CLI would isolate dependencies and match the default ecosystem
contract, but changes current selection behavior and adds process/error handling
migration. This remediation keeps the established import path and declares it.
It does not add an HTTP service or duplicate Dream engine logic.

## Verification and authority

The ordinary Pinned Dream API CI job installs the exact optional extra and runs
test_howldream_api_contract.py without enabling remote-provider tests. The test
executes the real Dream engine using its built-in mock generation provider,
checks schema and advisory authority, and forbids CLI fallback. This is genuine
component API integration with simulated generation; it is not live-model
quality evidence. The existing live-provider gate remains separate.

Dream results are untrusted advisory data, never authorization. Model B owner
approval requires an independently protected signing/execution boundary; neither
this import nor a compatible schema establishes that authority.

## Combined-environment assessment correction

The exploration runner also optionally imports Create's scaffold_candidate for
advisory promotion. With Create installed, four existing integration tests
exposed legacy assessment fields in both the Python fallback and the Frame
program. The same four failed on original Plane52b4a365. The fallback now reads
the current flat candidate status and emits schema_version, assessment_id and
limitations; the Frame program emits limitations instead of the obsolete reason
field. Bytecode is rebuilt with the existing CI compiler pin and its packaged
copy is synchronized. Create validation remains strict. New contract regressions
preserve the original failure. This additional correction requires independent
review; it must not be inferred from the earlier F9/F12/API-only review.
