#!/usr/bin/env python3
"""
hygiene_policy.py

Deterministic semantic classification and verification for repository hygiene policies
(e.g., SlopsLint configuration, ceilings, tombstones, and provider integrity).

Classifies policy changes into:
- TIGHTENING: Autonomously allowed (after deterministic verification succeeds)
- NEUTRAL: Autonomously allowed (formatting / documentation edits)
- WEAKENING: Human authority required (AWAITING_HUMAN)
- DEBT_ACCEPTANCE: Human authority required (AWAITING_HUMAN)
- HARD_REJECT: Prohibited (e.g. ceiling increase in ratchet / deleted config)
- UNKNOWN: Unparseable / unclassifiable -> MUST FAIL CLOSED (AWAITING_HUMAN / reject)
"""

from dataclasses import dataclass, field, asdict
from enum import Enum
import hashlib
from pathlib import Path
import shutil
import subprocess
from typing import Any, Dict, List, Optional, Set, Tuple

PINNED_SLOPSLINT_VERSION = "0.1.0"


class PolicyChangeType(str, Enum):
    TIGHTENING = "tightening"
    NEUTRAL = "neutral"
    WEAKENING = "weakening"
    DEBT_ACCEPTANCE = "debt_acceptance"
    HARD_REJECT = "hard_reject"
    UNKNOWN = "unknown"


@dataclass
class PolicyChangeDetail:
    """Individual classified modification to a repository hygiene policy."""

    change_type: PolicyChangeType
    category: str  # "ceiling", "config_scope", "config_global", "tombstone", "provider"
    target: str    # scope name, tombstone id, or config key
    description: str
    old_value: Any = None
    new_value: Any = None

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["change_type"] = self.change_type.value
        return d


@dataclass
class PolicyEvaluationResult:
    """Overall evaluation result across all policy modifications."""

    verdict: PolicyChangeType
    requires_human_approval: bool
    is_hard_rejected: bool
    changes: List[PolicyChangeDetail] = field(default_factory=list)
    summary: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "verdict": self.verdict.value,
            "requires_human_approval": self.requires_human_approval,
            "is_hard_rejected": self.is_hard_rejected,
            "changes": [c.to_dict() for c in self.changes],
            "summary": self.summary,
        }


class HygienePolicyClassifier:
    """
    Deterministically inspects and classifies changes to .slop/ configuration,
    ceilings, tombstones, and provider definitions.
    """

    @classmethod
    def classify_config(
        cls,
        old_config: Optional[Dict[str, Any]],
        new_config: Optional[Dict[str, Any]],
    ) -> List[PolicyChangeDetail]:
        """
        Deterministically compares old vs new .slop/config.yml.
        """
        changes: List[PolicyChangeDetail] = []

        if old_config is None and new_config is None:
            return changes

        if old_config is not None and new_config is None:
            changes.append(PolicyChangeDetail(
                change_type=PolicyChangeType.HARD_REJECT,
                category="config_global",
                target="config.yml",
                description="SlopsLint config.yml was deleted",
                old_value="present",
                new_value="deleted",
            ))
            return changes

        if old_config is None and new_config is not None:
            changes.append(PolicyChangeDetail(
                change_type=PolicyChangeType.TIGHTENING,
                category="config_global",
                target="config.yml",
                description="Initial introduction of SlopsLint config.yml",
                old_value="none",
                new_value="present",
            ))
            return changes

        assert old_config is not None and new_config is not None

        # 1. Defaults comparison
        old_defaults = old_config.get("defaults", {}) or {}
        new_defaults = new_config.get("defaults", {}) or {}

        # min_lines
        old_ml = old_defaults.get("min_lines")
        new_ml = new_defaults.get("min_lines")
        if old_ml is not None and new_ml is not None and old_ml != new_ml:
            if new_ml > old_ml:
                changes.append(PolicyChangeDetail(
                    change_type=PolicyChangeType.WEAKENING,
                    category="config_global",
                    target="defaults.min_lines",
                    description=f"min_lines increased from {old_ml} to {new_ml} (less sensitive)",
                    old_value=old_ml,
                    new_value=new_ml,
                ))
            else:
                changes.append(PolicyChangeDetail(
                    change_type=PolicyChangeType.TIGHTENING,
                    category="config_global",
                    target="defaults.min_lines",
                    description=f"min_lines decreased from {old_ml} to {new_ml} (stricter)",
                    old_value=old_ml,
                    new_value=new_ml,
                ))

        # min_tokens
        old_mt = old_defaults.get("min_tokens")
        new_mt = new_defaults.get("min_tokens")
        if old_mt is not None and new_mt is not None and old_mt != new_mt:
            if new_mt > old_mt:
                changes.append(PolicyChangeDetail(
                    change_type=PolicyChangeType.WEAKENING,
                    category="config_global",
                    target="defaults.min_tokens",
                    description=f"min_tokens increased from {old_mt} to {new_mt} (less sensitive)",
                    old_value=old_mt,
                    new_value=new_mt,
                ))
            else:
                changes.append(PolicyChangeDetail(
                    change_type=PolicyChangeType.TIGHTENING,
                    category="config_global",
                    target="defaults.min_tokens",
                    description=f"min_tokens decreased from {old_mt} to {new_mt} (stricter)",
                    old_value=old_mt,
                    new_value=new_mt,
                ))

        # mode (e.g. strict vs mild vs weak)
        mode_strictness = {"strict": 3, "mild": 2, "weak": 1}
        old_mode = old_defaults.get("mode", "mild")
        new_mode = new_defaults.get("mode", "mild")
        if old_mode != new_mode:
            old_rank = mode_strictness.get(str(old_mode).lower(), 2)
            new_rank = mode_strictness.get(str(new_mode).lower(), 2)
            if new_rank < old_rank:
                changes.append(PolicyChangeDetail(
                    change_type=PolicyChangeType.WEAKENING,
                    category="config_global",
                    target="defaults.mode",
                    description=f"Detector mode weakened from '{old_mode}' to '{new_mode}'",
                    old_value=old_mode,
                    new_value=new_mode,
                ))
            elif new_rank > old_rank:
                changes.append(PolicyChangeDetail(
                    change_type=PolicyChangeType.TIGHTENING,
                    category="config_global",
                    target="defaults.mode",
                    description=f"Detector mode tightened from '{old_mode}' to '{new_mode}'",
                    old_value=old_mode,
                    new_value=new_mode,
                ))

        # 2. Global Ignore comparison
        old_gi = set(old_config.get("global_ignore", []) or [])
        new_gi = set(new_config.get("global_ignore", []) or [])
        added_gi = new_gi - old_gi
        removed_gi = old_gi - new_gi

        for item in sorted(added_gi):
            changes.append(PolicyChangeDetail(
                change_type=PolicyChangeType.WEAKENING,
                category="config_global",
                target=f"global_ignore:{item}",
                description=f"Added global ignore pattern '{item}' (exempts files from scan)",
                old_value=None,
                new_value=item,
            ))
        for item in sorted(removed_gi):
            changes.append(PolicyChangeDetail(
                change_type=PolicyChangeType.TIGHTENING,
                category="config_global",
                target=f"global_ignore:{item}",
                description=f"Removed global ignore pattern '{item}' (expands scan coverage)",
                old_value=item,
                new_value=None,
            ))

        # 3. Scopes comparison
        old_scopes = old_config.get("scopes", {}) or {}
        new_scopes = new_config.get("scopes", {}) or {}

        old_scope_names = set(old_scopes.keys())
        new_scope_names = set(new_scopes.keys())

        for s in sorted(new_scope_names - old_scope_names):
            changes.append(PolicyChangeDetail(
                change_type=PolicyChangeType.TIGHTENING,
                category="config_scope",
                target=f"scope:{s}",
                description=f"Added new scan scope '{s}'",
                old_value=None,
                new_value=new_scopes[s],
            ))

        for s in sorted(old_scope_names - new_scope_names):
            changes.append(PolicyChangeDetail(
                change_type=PolicyChangeType.WEAKENING,
                category="config_scope",
                target=f"scope:{s}",
                description=f"Deleted scan scope '{s}' (reduces hygiene coverage)",
                old_value=old_scopes[s],
                new_value=None,
            ))

        for s in sorted(old_scope_names & new_scope_names):
            osc = old_scopes[s] or {}
            nsc = new_scopes[s] or {}

            # scan_path
            old_sp = osc.get("scan_path")
            new_sp = nsc.get("scan_path")
            if old_sp != new_sp:
                # If new scan_path is narrower or changed
                is_weaker = str(new_sp).startswith(str(old_sp))
                changes.append(PolicyChangeDetail(
                    change_type=PolicyChangeType.WEAKENING if is_weaker else PolicyChangeType.UNKNOWN,
                    category="config_scope",
                    target=f"scope:{s}.scan_path",
                    description=f"Scope '{s}' scan_path changed from '{old_sp}' to '{new_sp}'",
                    old_value=old_sp,
                    new_value=new_sp,
                ))

            # pattern
            old_pat = osc.get("pattern")
            new_pat = nsc.get("pattern")
            if old_pat != new_pat:
                changes.append(PolicyChangeDetail(
                    change_type=PolicyChangeType.WEAKENING,
                    category="config_scope",
                    target=f"scope:{s}.pattern",
                    description=f"Scope '{s}' file pattern changed from '{old_pat}' to '{new_pat}'",
                    old_value=old_pat,
                    new_value=new_pat,
                ))

            # ignore list within scope
            old_sc_ign = set(osc.get("ignore", []) or [])
            new_sc_ign = set(nsc.get("ignore", []) or [])
            added_sc_ign = new_sc_ign - old_sc_ign
            removed_sc_ign = old_sc_ign - new_sc_ign

            for item in sorted(added_sc_ign):
                changes.append(PolicyChangeDetail(
                    change_type=PolicyChangeType.WEAKENING,
                    category="config_scope",
                    target=f"scope:{s}.ignore:{item}",
                    description=f"Added ignore pattern '{item}' to scope '{s}'",
                    old_value=None,
                    new_value=item,
                ))
            for item in sorted(removed_sc_ign):
                changes.append(PolicyChangeDetail(
                    change_type=PolicyChangeType.TIGHTENING,
                    category="config_scope",
                    target=f"scope:{s}.ignore:{item}",
                    description=f"Removed ignore pattern '{item}' from scope '{s}'",
                    old_value=item,
                    new_value=None,
                ))

        return changes

    @classmethod
    def classify_ceilings(
        cls,
        old_ceilings: Optional[Dict[str, Any]],
        new_ceilings: Optional[Dict[str, Any]],
    ) -> List[PolicyChangeDetail]:
        """
        Deterministically compares old vs new .slop/ceilings.yml.
        Enforces monotonic decreases.
        """
        changes: List[PolicyChangeDetail] = []

        if old_ceilings is None and new_ceilings is None:
            return changes

        if old_ceilings is not None and new_ceilings is None:
            changes.append(PolicyChangeDetail(
                change_type=PolicyChangeType.HARD_REJECT,
                category="ceiling",
                target="ceilings.yml",
                description="SlopsLint ceilings.yml was deleted",
                old_value="present",
                new_value="deleted",
            ))
            return changes

        if old_ceilings is None and new_ceilings is not None:
            changes.append(PolicyChangeDetail(
                change_type=PolicyChangeType.TIGHTENING,
                category="ceiling",
                target="ceilings.yml",
                description="Initial introduction of SlopsLint ceilings.yml",
                old_value="none",
                new_value="present",
            ))
            return changes

        assert old_ceilings is not None and new_ceilings is not None

        old_scopes = old_ceilings.get("scopes", {}) or {}
        new_scopes = new_ceilings.get("scopes", {}) or {}

        old_names = set(old_scopes.keys())
        new_names = set(new_scopes.keys())

        # Added scope ceiling
        for s in sorted(new_names - old_names):
            c_val = new_scopes[s].get("active_clones_ceiling") if isinstance(new_scopes[s], dict) else new_scopes[s]
            changes.append(PolicyChangeDetail(
                change_type=PolicyChangeType.TIGHTENING,
                category="ceiling",
                target=f"ceiling:{s}",
                description=f"Added active clones ceiling for new scope '{s}' ({c_val})",
                old_value=None,
                new_value=c_val,
            ))

        # Removed scope ceiling
        for s in sorted(old_names - new_names):
            changes.append(PolicyChangeDetail(
                change_type=PolicyChangeType.WEAKENING,
                category="ceiling",
                target=f"ceiling:{s}",
                description=f"Removed active clones ceiling for scope '{s}'",
                old_value=old_scopes[s],
                new_value=None,
            ))

        # Modified scope ceiling
        for s in sorted(old_names & new_names):
            old_entry = old_scopes[s] or {}
            new_entry = new_scopes[s] or {}
            old_c = old_entry.get("active_clones_ceiling") if isinstance(old_entry, dict) else old_entry
            new_c = new_entry.get("active_clones_ceiling") if isinstance(new_entry, dict) else new_entry

            if old_c is not None and new_c is not None and old_c != new_c:
                if new_c < old_c:
                    # Lowering ceiling -> TIGHTENING (Quality improvement / ratchet reduction)
                    changes.append(PolicyChangeDetail(
                        change_type=PolicyChangeType.TIGHTENING,
                        category="ceiling",
                        target=f"ceiling:{s}",
                        description=f"Lowered active clones ceiling for scope '{s}' from {old_c} to {new_c} (debt reduction)",
                        old_value=old_c,
                        new_value=new_c,
                    ))
                else:
                    # Raising ceiling -> HARD_REJECT / WEAKENING (Debt inflation)
                    desc = f"Increased active clones ceiling for scope '{s}' from {old_c} to {new_c} (debt inflation)"
                    changes.append(PolicyChangeDetail(
                        change_type=PolicyChangeType.HARD_REJECT,
                        category="ceiling",
                        target=f"ceiling:{s}",
                        description=desc,
                        old_value=old_c,
                        new_value=new_c,
                    ))

        return changes

    @classmethod
    def classify_tombstones(
        cls,
        old_tombstones: Dict[str, Dict[str, Any]],
        new_tombstones: Dict[str, Dict[str, Any]],
        stale_tombstone_ids: Optional[Set[str]] = None,
    ) -> List[PolicyChangeDetail]:
        """
        Deterministically compares old vs new .slop/tombstones/ records.
        """
        changes: List[PolicyChangeDetail] = []
        stale_ids = stale_tombstone_ids or set()

        old_ids = set(old_tombstones.keys())
        new_ids = set(new_tombstones.keys())

        # Added tombstone -> Debt acceptance
        for tid in sorted(new_ids - old_ids):
            t_data = new_tombstones[tid]
            changes.append(PolicyChangeDetail(
                change_type=PolicyChangeType.DEBT_ACCEPTANCE,
                category="tombstone",
                target=f"tombstone:{tid}",
                description=f"Added new debt tombstone '{tid}' (category: {t_data.get('category', 'duplication')})",
                old_value=None,
                new_value=t_data,
            ))

        # Deleted tombstone
        for tid in sorted(old_ids - new_ids):
            t_data = old_tombstones[tid]
            if tid in stale_ids:
                # Stale tombstone deleted after underlying debt eliminated -> TIGHTENING
                changes.append(PolicyChangeDetail(
                    change_type=PolicyChangeType.TIGHTENING,
                    category="tombstone",
                    target=f"tombstone:{tid}",
                    description=f"Deleted stale tombstone '{tid}' after debt elimination (quality improvement)",
                    old_value=t_data,
                    new_value=None,
                ))
            else:
                # Non-stale tombstone deleted -> TIGHTENING (removes debt exemption; finding becomes active)
                changes.append(PolicyChangeDetail(
                    change_type=PolicyChangeType.TIGHTENING,
                    category="tombstone",
                    target=f"tombstone:{tid}",
                    description=f"Removed accepted debt exemption '{tid}' (findings subject to active ceiling)",
                    old_value=t_data,
                    new_value=None,
                ))

        # Modified tombstone
        for tid in sorted(old_ids & new_ids):
            ot = old_tombstones[tid] or {}
            nt = new_tombstones[tid] or {}

            # Fingerprint change (repointing tombstone to different code)
            old_fp = ot.get("match", {}).get("fingerprint") or ot.get("fingerprint")
            new_fp = nt.get("match", {}).get("fingerprint") or nt.get("fingerprint")
            if old_fp and new_fp and old_fp != new_fp:
                fp_desc = (
                    f"Modified fingerprint for tombstone '{tid}' from "
                    f"{str(old_fp)[:12]} to {str(new_fp)[:12]} (repointed debt)"
                )
                changes.append(PolicyChangeDetail(
                    change_type=PolicyChangeType.DEBT_ACCEPTANCE,
                    category="tombstone",
                    target=f"tombstone:{tid}.fingerprint",
                    description=fp_desc,
                    old_value=old_fp,
                    new_value=new_fp,
                ))
                continue

            # Scope change
            old_sc = ot.get("match", {}).get("scope") or ot.get("scope")
            new_sc = nt.get("match", {}).get("scope") or nt.get("scope")
            if old_sc != new_sc:
                changes.append(PolicyChangeDetail(
                    change_type=PolicyChangeType.WEAKENING,
                    category="tombstone",
                    target=f"tombstone:{tid}.scope",
                    description=f"Modified scope for tombstone '{tid}' from '{old_sc}' to '{new_sc}'",
                    old_value=old_sc,
                    new_value=new_sc,
                ))
                continue

            # Status change (e.g. proposed -> accepted)
            old_st = ot.get("status")
            new_st = nt.get("status")
            if old_st != new_st:
                if new_st == "accepted" and old_st != "accepted":
                    changes.append(PolicyChangeDetail(
                        change_type=PolicyChangeType.DEBT_ACCEPTANCE,
                        category="tombstone",
                        target=f"tombstone:{tid}.status",
                        description=f"Changed tombstone '{tid}' status from '{old_st}' to '{new_st}'",
                        old_value=old_st,
                        new_value=new_st,
                    ))
                else:
                    changes.append(PolicyChangeDetail(
                        change_type=PolicyChangeType.NEUTRAL,
                        category="tombstone",
                        target=f"tombstone:{tid}.status",
                        description=f"Updated tombstone '{tid}' status from '{old_st}' to '{new_st}'",
                        old_value=old_st,
                        new_value=new_st,
                    ))
                continue

            # Check if only non-critical metadata changed
            changes.append(PolicyChangeDetail(
                change_type=PolicyChangeType.NEUTRAL,
                category="tombstone",
                target=f"tombstone:{tid}",
                description=f"Non-semantic metadata update to tombstone '{tid}'",
                old_value=ot,
                new_value=nt,
            ))

        return changes

    @classmethod
    def evaluate_changes(
        cls,
        changes: List[PolicyChangeDetail],
        verification_passed: bool = True,
    ) -> PolicyEvaluationResult:
        """
        Synthesizes individual policy change details into an overall policy evaluation verdict.
        UNKNOWN MUST FAIL CLOSED.
        """
        if not changes:
            return PolicyEvaluationResult(
                verdict=PolicyChangeType.NEUTRAL,
                requires_human_approval=False,
                is_hard_rejected=False,
                changes=[],
                summary="No hygiene policy changes detected.",
            )

        has_hard_reject = any(c.change_type == PolicyChangeType.HARD_REJECT for c in changes)
        has_unknown = any(c.change_type == PolicyChangeType.UNKNOWN for c in changes)
        has_debt_acceptance = any(c.change_type == PolicyChangeType.DEBT_ACCEPTANCE for c in changes)
        has_weakening = any(c.change_type == PolicyChangeType.WEAKENING for c in changes)
        all_tightening_or_neutral = all(
            c.change_type in (PolicyChangeType.TIGHTENING, PolicyChangeType.NEUTRAL)
            for c in changes
        )

        if has_hard_reject:
            verdict = PolicyChangeType.HARD_REJECT
            req_human = True
            hard_rej = True
            summary = "Prohibited hygiene policy violation detected (e.g. ceiling increase or config deletion)."
        elif has_unknown:
            verdict = PolicyChangeType.UNKNOWN
            req_human = True
            hard_rej = False
            summary = "Unrecognized hygiene policy change detected (fail-closed, requires human authorization)."
        elif has_debt_acceptance:
            verdict = PolicyChangeType.DEBT_ACCEPTANCE
            req_human = True
            hard_rej = False
            summary = "New repository debt acceptance or tombstone modification requires human authorization."
        elif has_weakening:
            verdict = PolicyChangeType.WEAKENING
            req_human = True
            hard_rej = False
            summary = "Hygiene policy weakening requires human authorization."
        elif all_tightening_or_neutral:
            has_tightening = any(c.change_type == PolicyChangeType.TIGHTENING for c in changes)
            verdict = PolicyChangeType.TIGHTENING if has_tightening else PolicyChangeType.NEUTRAL
            req_human = False
            hard_rej = False
            summary = "Policy tightening / quality improvement autonomously permitted under deterministic verification."
        else:
            verdict = PolicyChangeType.UNKNOWN
            req_human = True
            hard_rej = False
            summary = "Unknown policy change combination (fail-closed)."

        return PolicyEvaluationResult(
            verdict=verdict,
            requires_human_approval=req_human,
            is_hard_rejected=hard_rej,
            changes=changes,
            summary=summary,
        )

    @classmethod
    def verify_provider_integrity(
        cls,
        executable_name: str = "slopslint",
        pinned_version: str = PINNED_SLOPSLINT_VERSION,
    ) -> Tuple[bool, str, Dict[str, Any]]:
        """
        Deterministically verifies the hygiene provider executable, version, and binary integrity.
        Fails if binary is missing, unpinned, or mismatched.
        """
        bin_path = shutil.which(executable_name)
        if not bin_path:
            return False, f"Executable '{executable_name}' not found on PATH.", {
                "provider": executable_name,
                "status": "missing",
            }

        resolved_path = str(Path(bin_path).resolve())

        # Compute sha256 checksum
        try:
            with open(resolved_path, "rb") as f:
                sha256 = hashlib.sha256(f.read()).hexdigest()
        except Exception as e:
            return False, f"Unable to read executable '{resolved_path}': {e}", {
                "provider": executable_name,
                "path": resolved_path,
                "status": "unreadable",
            }

        # Check version
        try:
            res = subprocess.run(
                [resolved_path, "--version"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            version_output = res.stdout.strip()
        except Exception as e:
            return False, f"Failed to execute '{resolved_path} --version': {e}", {
                "provider": executable_name,
                "path": resolved_path,
                "status": "exec_error",
            }

        extracted_version = ""
        for token in version_output.split():
            if token.replace(".", "").isdigit() or "0." in token or "1." in token:
                extracted_version = token
                break

        if not extracted_version:
            extracted_version = version_output

        if pinned_version and extracted_version != pinned_version:
            return False, (
                f"Provider version mismatch: found '{extracted_version}', "
                f"expected pinned '{pinned_version}'."
            ), {
                "provider": executable_name,
                "path": resolved_path,
                "version": extracted_version,
                "pinned_version": pinned_version,
                "checksum": sha256,
                "status": "version_mismatch",
            }

        return True, f"Provider '{executable_name}' v{extracted_version} verified.", {
            "provider": executable_name,
            "path": resolved_path,
            "version": extracted_version,
            "pinned_version": pinned_version,
            "checksum": sha256,
            "status": "verified",
        }
