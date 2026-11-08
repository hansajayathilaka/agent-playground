"""Path rules for agent folders.

Every file path that comes from a client or the Builder goes through
``resolve_agent_path``. Nothing else in the platform may turn a client string
into a filesystem path.
"""

from __future__ import annotations

import os
import re
from pathlib import Path, PurePosixPath

from playground_core.errors import InvalidArgumentError, InvalidPathError, NotFoundError

# User agents: lowercase letters, digits and inner dashes. System agents (for
# example "_builder") add a leading underscore and can't be created via the API.
_SLUG_RE = re.compile(r"^_?[a-z0-9](?:[a-z0-9-]{0,48}[a-z0-9])?$")
_MAX_COMPONENT = 255
_MAX_DEPTH = 32

# Top-level folders owned by the platform. Hidden from the tree by default and
# never writable through the API.
PLATFORM_DIRS = frozenset({"state", ".git"})
# Files that must always exist in an agent folder.
UNDELETABLE_FILES = frozenset({"agent.json"})


def validate_slug(slug: str, *, allow_system: bool = True) -> str:
    if not _SLUG_RE.fullmatch(slug):
        raise InvalidArgumentError(
            f"invalid agent slug {slug!r}: use 1-50 lowercase letters, digits and dashes"
        )
    if slug.startswith("_") and not allow_system:
        raise InvalidArgumentError(f"slug {slug!r} is reserved for system agents")
    return slug


def normalize_rel_path(rel: str, *, allow_root: bool = False) -> PurePosixPath:
    """Turn a client path into a clean relative POSIX path, or raise.

    Rejects absolute paths, drive letters, backslashes, NUL bytes, ".." and
    over-long components. "." and empty segments are dropped.
    """
    if "\x00" in rel:
        raise InvalidPathError("path contains a NUL byte")
    if "\\" in rel:
        raise InvalidPathError("use '/' as the path separator")
    if rel.startswith("/") or re.match(r"^[A-Za-z]:", rel):
        raise InvalidPathError("path must be relative to the agent folder")
    parts = [p for p in rel.split("/") if p not in ("", ".")]
    if any(p == ".." for p in parts):
        raise InvalidPathError("'..' is not allowed in paths")
    if any(len(p) > _MAX_COMPONENT for p in parts):
        raise InvalidPathError("path component too long")
    if len(parts) > _MAX_DEPTH:
        raise InvalidPathError("path too deep")
    if not parts and not allow_root:
        raise InvalidPathError("path is empty")
    return PurePosixPath(*parts)


def resolve_agent_path(
    agents_root: Path, slug: str, rel: str = "", *, allow_root: bool = False
) -> tuple[Path, PurePosixPath]:
    """Resolve ``rel`` inside the agent folder ``agents_root/slug``.

    Returns the absolute path and the normalized relative path. Symlinks are
    refused anywhere along the path (no-follow), so the result can't point
    outside the agent folder. Raises NotFoundError if the agent folder is
    missing and InvalidPathError for any unsafe path.
    """
    validate_slug(slug)
    clean = normalize_rel_path(rel, allow_root=allow_root)

    agent_root = agents_root / slug
    if agent_root.is_symlink() or not agent_root.is_dir():
        raise NotFoundError(f"agent {slug!r} not found")
    real_root = agent_root.resolve(strict=True)

    current = agent_root
    for part in clean.parts:
        current = current / part
        if current.is_symlink():
            raise InvalidPathError(f"symlinks are not allowed: {clean.as_posix()}")
        if not current.exists():
            break  # nothing below a missing component can be a symlink

    target = agent_root.joinpath(*clean.parts)
    # Belt and braces: the resolved target must stay under the agent root.
    resolved = Path(os.path.realpath(target))
    if os.path.commonpath([real_root, resolved]) != str(real_root):
        raise InvalidPathError("path escapes the agent folder")
    return target, clean


def top_level(rel: PurePosixPath) -> str:
    return rel.parts[0] if rel.parts else ""


def is_platform_path(rel: PurePosixPath) -> bool:
    return top_level(rel) in PLATFORM_DIRS
