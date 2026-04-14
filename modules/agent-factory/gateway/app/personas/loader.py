"""
Persona loader — loads persona definitions from multiple sources.

Supports two formats:
1. ADP markdown personas (.adp-rules/personas/*.md) — used by the GitHub agent
2. YAML personas (config/personas/*.yaml) — AISuperPlane format

The loader checks YAML first (gateway-specific), then falls back to ADP markdown.
For markdown personas, the entire file content becomes the system_prompt since
the ADP format is already a complete persona instruction document.

Usage:
    persona = load_persona("developer")
    system_prompt = persona.system_prompt
"""

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Directories to search for persona files
PERSONAS_YAML_DIR = os.environ.get("PERSONAS_DIR", "/app/config/personas")
PERSONAS_MD_DIR = os.environ.get("ADP_RULES_DIR", "/app/repo/.adp-rules/personas")

# In-memory cache
_cache: dict[str, "Persona"] = {}


@dataclass
class Persona:
    """A persona configuration."""
    name: str = "default"
    description: str = ""
    system_prompt: str = ""
    tools: list[str] = field(default_factory=list)
    model_override: str | None = None
    max_tokens_override: int | None = None
    source: str = "default"  # "yaml", "markdown", or "default"

    @classmethod
    def from_yaml(cls, path: Path) -> "Persona":
        """Load from YAML file (AISuperPlane format)."""
        try:
            import yaml
        except ImportError:
            logger.warning("PyYAML not installed — cannot load YAML personas")
            return cls(name=path.stem, source="yaml")

        with open(path) as f:
            data: dict[str, Any] = yaml.safe_load(f) or {}

        return cls(
            name=data.get("name", path.stem),
            description=data.get("description", ""),
            system_prompt=data.get("system_prompt", ""),
            tools=data.get("tools", []),
            model_override=data.get("model_override"),
            max_tokens_override=data.get("max_tokens_override"),
            source="yaml",
        )

    @classmethod
    def from_markdown(cls, path: Path) -> "Persona":
        """Load from ADP markdown file (.adp-rules/personas/*.md).

        The entire markdown content becomes the system_prompt.
        The first heading is used as the description.
        """
        content = path.read_text()

        # Extract description from first line that starts with #
        description = ""
        for line in content.split("\n"):
            if line.startswith("# "):
                description = line.lstrip("# ").strip()
                break

        return cls(
            name=path.stem,
            description=description,
            system_prompt=content,
            source="markdown",
        )


# Default fallback persona
_DEFAULT = Persona(
    name="default",
    description="General-purpose ADP assistant",
    system_prompt=(
        "You are an AI assistant for the ADP (Agent Development Platform). "
        "You help users with software development, infrastructure management, "
        "and operational tasks. Be concise, accurate, and actionable. "
        "When you don't have enough information, ask clarifying questions."
    ),
    source="default",
)


def load_persona(name: str) -> Persona:
    """Load a persona by name.

    Search order:
    1. In-memory cache
    2. YAML file at {PERSONAS_YAML_DIR}/{name}.yaml
    3. Markdown file at {PERSONAS_MD_DIR}/{name}.md
    4. Default fallback persona

    Args:
        name: Persona name (e.g., "developer", "operations", "reviewer")

    Returns:
        The loaded Persona.
    """
    if name in _cache:
        return _cache[name]

    persona = _try_load(name)
    _cache[name] = persona
    return persona


def _try_load(name: str) -> Persona:
    # Try YAML first (gateway-specific personas)
    yaml_path = Path(PERSONAS_YAML_DIR) / f"{name}.yaml"
    if yaml_path.exists():
        logger.info("Loading persona '%s' from YAML: %s", name, yaml_path)
        return Persona.from_yaml(yaml_path)

    # Try ADP markdown personas
    md_path = Path(PERSONAS_MD_DIR) / f"{name}.md"
    if md_path.exists():
        logger.info("Loading persona '%s' from ADP markdown: %s", name, md_path)
        return Persona.from_markdown(md_path)

    # Check if the ADP repo is cloned at a different path
    alt_paths = [
        Path("/app/repo/.adp-rules/personas") / f"{name}.md",
        Path("/workspace/.adp-rules/personas") / f"{name}.md",
        Path(os.environ.get("WORK_DIR", "")) / ".adp-rules/personas" / f"{name}.md",
    ]
    for alt in alt_paths:
        if alt.exists():
            logger.info("Loading persona '%s' from alt path: %s", name, alt)
            return Persona.from_markdown(alt)

    logger.info("Persona '%s' not found — using default", name)
    return Persona(name=name, system_prompt=_DEFAULT.system_prompt, source="default")


def list_personas() -> list[str]:
    """List all available persona names."""
    names = set()

    yaml_dir = Path(PERSONAS_YAML_DIR)
    if yaml_dir.exists():
        names.update(p.stem for p in yaml_dir.glob("*.yaml"))

    md_dir = Path(PERSONAS_MD_DIR)
    if md_dir.exists():
        names.update(p.stem for p in md_dir.glob("*.md"))

    return sorted(names)
