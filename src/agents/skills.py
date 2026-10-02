from __future__ import annotations

from pathlib import Path

from .board import AGENTS_DIRNAME, BoardError


def skills_dir(root: Path | str) -> Path:
    return Path(root) / AGENTS_DIRNAME / "skills"


def available_skills(root: Path | str) -> list[str]:
    directory = skills_dir(root)
    if not directory.is_dir():
        return []
    return sorted(path.stem for path in directory.glob("*.md"))


def validate_skills(root: Path | str, names: list[str]) -> None:
    known = set(available_skills(root))
    for name in names:
        if name not in known:
            raise BoardError(f"unknown skill '{name}'; available: {', '.join(sorted(known)) or '(none)'}")


def load_skills(root: Path | str, names: list[str], max_chars: int) -> str:
    directory = skills_dir(root)
    chunks: list[str] = []
    total = 0
    for name in names:
        path = directory / f"{name}.md"
        if not path.is_file():
            raise BoardError(f"skill '{name}' not found in .agents/skills/")
        text = path.read_text().strip()
        block = f"### skill: {name}\n{text}"
        if total + len(block) > max_chars:
            raise BoardError(f"skills exceed the {max_chars}-char budget; trim .agents/skills/{name}.md")
        chunks.append(block)
        total += len(block)
    return "\n\n".join(chunks)
