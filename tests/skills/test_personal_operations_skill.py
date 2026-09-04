"""Tests for optional-skills/productivity/personal-operations."""

import re
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
SKILL_MD = (
    REPO_ROOT
    / "optional-skills"
    / "productivity"
    / "personal-operations"
    / "SKILL.md"
)


def _frontmatter_and_body():
    text = SKILL_MD.read_text(encoding="utf-8")
    m = re.match(r"^---\n(.*?)\n---\n(.*)$", text, re.DOTALL)
    assert m, "SKILL.md missing YAML frontmatter"
    return yaml.safe_load(m.group(1)), m.group(2)


def test_name_and_description():
    fm, _ = _frontmatter_and_body()
    assert fm["name"] == "personal-operations"
    assert len(fm["description"]) <= 60
    assert fm["description"].endswith(".")
    assert fm["license"] == "MIT"
    assert "hamad" in str(fm["metadata"]["hermes"]).lower()


def test_author_credits_human_first():
    fm, _ = _frontmatter_and_body()
    assert not str(fm["author"]).startswith("Hermes Agent")
    assert "Abdulrahman" in fm["author"]


def test_related_skills_resolve():
    fm, _ = _frontmatter_and_body()
    related = fm["metadata"]["hermes"]["related_skills"]
    for required in ("maps", "product-price-monitor", "telephony", "computer-use"):
        assert required in related
    for name in related:
        hits = (
            list(REPO_ROOT.glob(f"skills/*/{name}/SKILL.md"))
            + list(REPO_ROOT.glob(f"optional-skills/*/{name}/SKILL.md"))
            + list(REPO_ROOT.glob(f"skills/*/*/{name}/SKILL.md"))
        )
        assert hits, f"related_skills entry does not resolve in-repo: {name}"


def test_body_routes_existing_surfaces_only():
    _, body = _frontmatter_and_body()
    for section in (
        "## When to Use",
        "## When NOT to Use",
        "## Prerequisites",
        "## How to Run",
        "## Quick Reference",
        "## Procedure",
        "## Pitfalls",
        "## Verification",
    ):
        assert section in body, f"missing section: {section}"
    assert "AgentComputer" in body or "hosted" in body.lower()
    assert "`computer_use`" in body
    assert "telephony" in body
    assert "`maps`" in body
    assert "product-price-monitor" in body
    assert "clarify" in body
    assert "consequence" in body.lower()
    assert "Hamad" in body
    assert "booking backend" in body.lower()
    assert "daily Mac Chrome" in body


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
