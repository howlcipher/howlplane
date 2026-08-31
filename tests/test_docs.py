import os
import sys
import subprocess
import tempfile


def test_pdoc_api_generation():
    """
    Regression test to ensure pdoc can successfully import and generate
    API documentation for all source packages and scripts.
    This prevents hidden ModuleNotFoundError issues caused by missing
    relative sys.path assignments in standalone scripts.
    """
    repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))

    with tempfile.TemporaryDirectory() as tmpdir:
        result = subprocess.run(
            [sys.executable, "-m", "pdoc", "./src", "./scripts", "-o", tmpdir],
            cwd=repo_root,
            capture_output=True,
            text=True,
        )

        # If pdoc fails, it usually means a module failed to import when evaluated from the root.
        assert (
            result.returncode == 0
        ), f"API Documentation generation failed!\nSTDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"


def test_data_flows_and_control_plane_docs_exist_and_linked():
    """Ensures data_flows.md and CONTROL_PLANE.md exist and are referenced."""
    repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    data_flows = os.path.join(repo_root, "documentation", "data_flows.md")
    control_plane = os.path.join(repo_root, "documentation", "CONTROL_PLANE.md")
    readme = os.path.join(repo_root, "README.md")
    agents_md = os.path.join(repo_root, "AGENTS.md")

    assert os.path.isfile(data_flows), f"{data_flows} does not exist"
    assert os.path.isfile(control_plane), f"{control_plane} does not exist"

    with open(readme, "r", encoding="utf-8") as f:
        readme_text = f.read()
    assert "data_flows.md" in readme_text, "data_flows.md not linked in README.md"

    with open(agents_md, "r", encoding="utf-8") as f:
        agents_text = f.read()
    assert "data_flows.md" in agents_text, "data_flows.md not linked in AGENTS.md"
    assert "CONTROL_PLANE.md" in agents_text, "CONTROL_PLANE.md not linked in AGENTS.md"



# ---------------------------------------------------------------------------
# Governance: the persistent-operation carve-out narrows the freeze without
# suspending it (CONTROL_PLANE.md 1.1.1, ADR 0006).
#
# These are not style checks. The carve-out is the only thing authorizing
# factory work to exist at all, and its whole legitimacy rests on (a) the
# freeze's default rule surviving intact and (b) the carve-out refusing to
# grant authority. A future edit that quietly drops either turns a bounded
# exception into an open door, which is exactly what these pin.
# ---------------------------------------------------------------------------

def _read(*parts):
    repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    with open(os.path.join(repo_root, *parts), "r", encoding="utf-8") as handle:
        return handle.read()


def _flat(*parts):
    """Document text with runs of whitespace collapsed.

    Prose in these documents is hard-wrapped, so a sentence that must be
    present is routinely split across a newline and a `> ` quote marker. These
    assertions are about meaning surviving, not about where the wrap lands.
    """
    return " ".join(_read(*parts).replace(">", " ").split())


def test_architectural_freeze_default_rule_is_not_weakened():
    """The carve-out must narrow the freeze, never suspend it."""
    text = _read("documentation", "CONTROL_PLANE.md")
    flat = _flat("documentation", "CONTROL_PLANE.md")

    assert "The control plane architecture is **FROZEN**" in text
    assert '*"Is X blocking real engineering work?"* If no: **DO NOT BUILD IT**.' in text
    assert "The carve-out narrows the freeze; it does not suspend it." in flat


def test_persistent_operation_carveout_cites_measured_evidence():
    """1.1.1 must carry the operational evidence the freeze demands, not prose."""
    text = _read("documentation", "CONTROL_PLANE.md")

    assert "### 1.1.1 Named Carve-Out: Persistent Operation" in text
    # The two measurements that motivate the whole milestone.
    assert "231 of 722" in text, "pool-exhaustion evidence missing from the carve-out"
    assert "0 of 722" in text, "never-fired-park evidence missing from the carve-out"
    assert "147,806" in text, "evidence-ledger size missing from the carve-out"
    assert "adr/0006_persistent_factory_supervisor.md" in text


def test_carveout_grants_no_new_authority():
    """A capability carve-out must not become an authority carve-out."""
    flat = _flat("documentation", "CONTROL_PLANE.md")

    assert "It grants no new authority mechanism" in flat
    assert "`NEVER_DELEGATABLE_BOUNDARIES` is untouched" in flat


def test_adr_0006_exists_and_records_the_rejected_abstractions():
    """ADR 0005 established the house rule; 0006 must honour the same form."""
    flat = _flat("documentation", "adr", "0006_persistent_factory_supervisor.md")

    assert "## Gap Analysis" in flat
    assert "## Rejection of Speculative Abstractions" in flat
    assert "**No new authority mechanism**" in flat
    assert "**No database as source of truth**" in flat


def test_resilience_doc_permits_only_a_derived_index():
    """The 'no databases' promise is narrowed to correctness, not banned outright."""
    flat = _flat("documentation", "OPERATIONAL_RESILIENCE.md")

    assert "No database is required for correctness" in flat
    assert "derived, rebuildable accelerator" in flat
    assert "No recovery, authority, or verification decision may read from it." in flat
    # The daemon-free promise is unrelated to the index and must survive.
    assert "without requiring background daemons or Redis" in flat


def test_implementation_status_does_not_masquerade_as_current_state():
    """It documents the Go blueprint; readers must not mistake it for the live plane."""
    flat = _flat("documentation", "IMPLEMENTATION_STATUS.md")

    assert "Scope warning" in flat
    assert "does **not** describe the live control plane" in flat
    assert "`src/control_plane/`" in flat
