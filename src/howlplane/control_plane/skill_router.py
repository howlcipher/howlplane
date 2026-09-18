#!/usr/bin/env python3
"""
skill_router.py

Routes user prompts to relevant skills stored in the agents skills directory.

Matching runs in two stages:
1. Deterministic triggers: keyword lists declared in a skill's frontmatter
   always route when found in the prompt.
2. Semantic scoring: skill descriptions are scored against the prompt with a
   cross encoder, filling the remaining slots above a score threshold.

Only the name and description are used for routing. The full SKILL.md body is
loaded for winners only, keeping prompt context small (progressive disclosure).
"""

import os
import re
from dataclasses import dataclass, field
from typing import Callable, List, Optional, Tuple

import yaml

from howlplane.control_plane.config_loader import default_loader, load_config

FRONTMATTER_PATTERN = re.compile(r"\A---\s*\n(.*?)\n---\s*\n?", re.DOTALL)

FALLBACK_STOPWORDS = (
    "about", "all", "and", "any", "are", "but", "can", "could", "during", "for",
    "from", "has", "have", "help", "how", "into", "not", "our", "please", "should",
    "than", "that", "the", "them", "then", "this", "using", "was", "were", "what",
    "when", "will", "with", "would", "you", "your",
)
_FALLBACK_STOPWORDS_SET = frozenset(FALLBACK_STOPWORDS)

FALLBACK_MIN_OVERLAP = 2


@dataclass
class Skill:
    """A routable skill parsed from a SKILL.md frontmatter."""

    name: str
    description: str
    path: str
    triggers: List[str] = field(default_factory=list)
    tier: Optional[int] = None
    pipeline_pass: Optional[int] = None

    def load_content(self) -> str:
        """Reads and returns the full SKILL.md body."""
        with open(self.path, "r", encoding="utf8") as f:
            return f.read()


class SkillRouter:
    """
    Selects skills relevant to a prompt and renders them as a context block.
    """

    def __init__(self, skills_dir: Optional[str] = None, cfg: Optional[dict] = None):
        """
        Args:
            skills_dir: Directory containing one subdirectory per skill, each
                with a SKILL.md file. Defaults to the configured location
                relative to the repository root.
            cfg: Optional preloaded configuration dictionary.
        """
        self.cfg = cfg or load_config()
        router_cfg = self.cfg.get("skill_router", {}) or {}

        self.enabled = router_cfg.get("enabled", True)
        self.top_k = router_cfg.get("top_k", 3)
        self.score_threshold = router_cfg.get("score_threshold", 0.0)
        self.max_context_chars = router_cfg.get("max_context_chars", 12000)

        if skills_dir is None:
            skills_dir = os.path.join(
                default_loader.get_repo_root(),
                router_cfg.get("skills_dir", os.path.join(".agents", "skills")),
            )
        self.skills_dir = skills_dir

        self._scorer: Optional[Callable] = None
        self._scorer_failed = False
        self.skills = self._load_skills()

    def _load_skills(self) -> List[Skill]:
        """Scans the skills directory and parses each SKILL.md frontmatter."""
        skills = []
        if not os.path.isdir(self.skills_dir):
            return skills

        for entry in sorted(os.listdir(self.skills_dir)):
            skill_path = os.path.join(self.skills_dir, entry, "SKILL.md")
            if not os.path.isfile(skill_path):
                continue
            try:
                with open(skill_path, "r", encoding="utf8") as f:
                    text = f.read()
                meta = self._parse_frontmatter(text)
                if not meta:
                    continue
                triggers = meta.get("triggers") or []
                if isinstance(triggers, str):
                    triggers = [triggers]
                tier = meta.get("tier")
                try:
                    tier = int(tier) if tier is not None else None
                except (TypeError, ValueError):
                    tier = None
                pipeline_pass = meta.get("pipeline_pass")
                try:
                    pipeline_pass = int(pipeline_pass) if pipeline_pass is not None else None
                except (TypeError, ValueError):
                    pipeline_pass = None
                skills.append(
                    Skill(
                        name=str(meta.get("name", entry)),
                        description=str(meta.get("description", "")),
                        path=skill_path,
                        triggers=[str(t).lower() for t in triggers],
                        tier=tier,
                        pipeline_pass=pipeline_pass,
                    )
                )
            except Exception as e:
                print(f"[SkillRouter] Skipping {skill_path}: {e}")
        return skills

    @staticmethod
    def _parse_frontmatter(text: str) -> Optional[dict]:
        """Extracts the YAML frontmatter block from a SKILL.md file."""
        match = FRONTMATTER_PATTERN.match(text)
        if not match:
            return None
        try:
            data = yaml.safe_load(match.group(1))
        except yaml.YAMLError:
            return None
        return data if isinstance(data, dict) else None

    def _get_scorer(self) -> Optional[Callable]:
        """
        Lazily loads the cross encoder used for semantic scoring. Returns None
        when the model cannot be loaded, which activates the keyword fallback.
        """
        if self._scorer is not None:
            return self._scorer
        if self._scorer_failed:
            return None
        try:
            from sentence_transformers import CrossEncoder

            model = CrossEncoder("cross-encoder/ms-marco-MiniLM-L-6-v2")
            self._scorer = model.predict
        except Exception as e:
            print(
                f"[SkillRouter] Cross encoder unavailable ({e}). "
                "Falling back to keyword overlap scoring."
            )
            self._scorer_failed = True
        return self._scorer

    @staticmethod
    def _tokenize(text: str) -> set:
        tokens = re.findall(r"[a-z0-9_]{3,}", text.lower())
        return {t for t in tokens if t not in _FALLBACK_STOPWORDS_SET}

    def _semantic_scores(self, prompt: str, skills: List[Skill]) -> List[float]:
        """
        Scores skills against the prompt. Uses the cross encoder when
        available; otherwise falls back to keyword token overlap, where the
        score is the overlap count and anything below the minimum overlap is
        pushed under any sensible threshold.
        """
        scorer = self._get_scorer()
        if scorer is not None:
            pairs = [[prompt, f"{s.name}: {s.description}"] for s in skills]
            return [float(x) for x in scorer(pairs)]

        prompt_tokens = self._tokenize(prompt)
        scores = []
        for s in skills:
            overlap = len(prompt_tokens & self._tokenize(f"{s.name} {s.description}"))
            scores.append(
                float(overlap) if overlap >= FALLBACK_MIN_OVERLAP else float("-inf")
            )
        return scores

    def route(self, prompt: str) -> List[Tuple[Skill, float, str]]:
        """
        Selects skills relevant to the prompt.

        Returns:
            A list of (skill, score, reason) tuples, trigger matches first,
            then semantic matches ranked by score. Trigger matches are always
            included; semantic matches fill remaining slots up to top_k.
        """
        if not self.enabled or not prompt.strip() or not self.skills:
            return []

        prompt_lower = prompt.lower()
        selected = []
        triggered_names = set()

        for skill in self.skills:
            for trigger in skill.triggers:
                if trigger and trigger in prompt_lower:
                    selected.append((skill, float("inf"), f"trigger '{trigger}'"))
                    triggered_names.add(skill.name)
                    break

        remaining_slots = max(self.top_k - len(selected), 0)
        if remaining_slots > 0:
            candidates = [s for s in self.skills if s.name not in triggered_names]
            if candidates:
                scores = self._semantic_scores(prompt, candidates)
                ranked = sorted(
                    zip(candidates, scores), key=lambda x: x[1], reverse=True
                )
                for skill, score in ranked[:remaining_slots]:
                    if score >= self.score_threshold:
                        selected.append((skill, score, f"semantic score {score:.3f}"))

        return selected

    def build_context(self, prompt: str, pipeline_pass: Optional[int] = None) -> str:
        """
        Renders the routed skills as a context block for prompt injection,
        capped at max_context_chars. Skills that do not fit within the budget
        are listed by name and description only.

        pipeline_pass: when given, only skills whose own `pipeline_pass` is
        None (applies to every pass) or equal to this value are included.
        """
        routed = self.route(prompt)
        if not routed:
            return ""

        if pipeline_pass is not None:
            routed = [
                (skill, score, reason)
                for skill, score, reason in routed
                if skill.pipeline_pass is None or skill.pipeline_pass == pipeline_pass
            ]
            if not routed:
                return ""

        names = ", ".join(skill.name for skill, _, _ in routed)
        print(f"[SkillRouter] Routed skills: {names}")

        parts = ["Relevant skills for this task (follow their directives):"]
        budget = self.max_context_chars

        for skill, _, reason in routed:
            header = f"\n--- Skill: {skill.name} (matched by {reason}) ---\n"
            try:
                body = skill.load_content()
            except Exception as e:
                print(f"[SkillRouter] Could not load {skill.path}: {e}")
                continue

            if len(header) + len(body) <= budget:
                parts.append(header + body)
                budget -= len(header) + len(body)
            else:
                summary = f"\n--- Skill: {skill.name} ---\n{skill.description}\n"
                if len(summary) <= budget:
                    parts.append(summary)
                    budget -= len(summary)

        return "\n".join(parts)
