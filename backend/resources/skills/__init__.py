"""Skill loader — reads the phase playbooks in this directory.

The Harness does not read these at runtime: `harness.tf` composes the files listed
in `harness_skill_files` into its system prompt, so a skill change takes effect
only after `terraform apply`. This loader backs the read-only Settings → Agent
Skills view.
"""

from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)

SKILLS_DIR = Path(__file__).parent


def load_skill(name: str) -> str:
    """Load a skill file by name (without .md extension).

    Raises FileNotFoundError if the skill does not exist.
    """
    filepath = SKILLS_DIR / f"{name}.md"
    if not filepath.exists():
        msg = (
            f"Skill '{name}' not found at {filepath}. "
            f"Add it to backend/resources/skills/ and harness_skill_files, then terraform apply."
        )
        logger.error(msg)
        raise FileNotFoundError(msg)
    return filepath.read_text()


def list_skills() -> list[dict]:
    """List all available skills with metadata."""
    skills = []
    for f in sorted(SKILLS_DIR.glob("*.md")):
        content = f.read_text()
        name = f.stem
        description = ""
        if content.startswith("---"):
            try:
                end = content.index("---", 3)
                front_matter = content[3:end]
                for line in front_matter.split("\n"):
                    if line.startswith("description:"):
                        description = line[12:].strip()
            except ValueError:
                pass
        skills.append(
            {
                "name": name,
                "description": description,
                "file": f.name,
                "source": "local",
            }
        )
    return skills
