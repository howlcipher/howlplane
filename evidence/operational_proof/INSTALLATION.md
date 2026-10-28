# Published installation boundary

Executed on Bazzite amd64, system Python 3.14.8, using published Howl v0.2.1. Archive SHA256 `f7af10cf0865c002054468069e9003a1569ac76fb3b293d915ba0e87480e3dbf` matched the downloaded release SHA256SUMS. The source archive and checksum evidence were already present under `release-acceptance-20261025/downloads/howl`; no source build substituted.

Sandbox: `bwrap --ro-bind / / --bind <this-directory>/install_home /var/home/howlcipher --tmpfs /tmp --proc /proc --dev /dev --setenv FORBID_LOCAL_INFERENCE 1 -- <this-directory>/installer_bin/howl install --profile standard --yes`. Existing HOME stayed `/home/howlcipher` (resolves to `/var/home/howlcipher`). Caller-owned empty directory hid the owner's home; root filesystem was read-only, network available. This exercises installation on the current supported host userspace, not a fresh OS image.

First fixture attempt omitted `--tmpfs /tmp`: actual Frame and ChangeOps installed, then pip could not find writable temporary storage. This was a harness error. `clean_install.log` retains the failure. Correcting the sandbox and retrying recovered the same isolated installation; `clean_install_retry.log` ends `Install complete.` with all five components `ok`.

Installed real published assets: Frame 0.1.0, ChangeOps 0.2.0, Plane engine 0.1.0, Plane CLI 0.1.0, Writer 0.1.0. No optional local inference installation or invocation occurred. These published versions do **not** include the mission's new source fixes.

`howl status --json` recorded in `install_status.json`. `doctor --strict --json` returns exit 0 but reports **DEGRADED**, not verified: component health, manifest validity and installed version compatibility pass; Ollama is absent and published Frame lacks an interop descriptor, leaving its edges unchecked. Never infer successful end-to-end engineering from installer health checks. Exact rerun command, exit, stdout and stderr are in `install_results.json`; the subsequent idempotent standard installation also exits 0.

No one-shot pristine installation success is claimed: this is an initially empty-home installation plus accurately recorded retry/recovery after a fixture failure. No services were deployed and no owner installation was modified.
