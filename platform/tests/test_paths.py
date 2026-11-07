"""Escape suite for resolve_agent_path(): nothing may resolve outside the
agent folder."""

from __future__ import annotations

from pathlib import Path

import pytest

from playground_core.errors import InvalidArgumentError, InvalidPathError, NotFoundError
from playground_core.paths import normalize_rel_path, resolve_agent_path, validate_slug


@pytest.fixture
def root(tmp_path: Path) -> Path:
    (tmp_path / "agents" / "alpha" / "workspace").mkdir(parents=True)
    (tmp_path / "agents" / "beta").mkdir()
    (tmp_path / "agents" / "beta" / "secret.txt").write_text("beta only")
    (tmp_path / "outside.txt").write_text("host file")
    return tmp_path / "agents"


@pytest.mark.parametrize(
    "rel",
    [
        "../beta/secret.txt",
        "workspace/../../beta/secret.txt",
        "..",
        "/etc/passwd",
        "//etc/passwd",
        "C:/Windows/win.ini",
        "c:relative",
        "workspace\\..\\..\\beta",
        "workspace/\x00evil",
        "a/" * 40 + "deep",
        "x" * 256,
    ],
)
def test_rejects_unsafe_paths(root: Path, rel: str) -> None:
    with pytest.raises(InvalidPathError):
        resolve_agent_path(root, "alpha", rel)


def test_rejects_empty_path_unless_root_allowed(root: Path) -> None:
    with pytest.raises(InvalidPathError):
        resolve_agent_path(root, "alpha", "")
    path, rel = resolve_agent_path(root, "alpha", "", allow_root=True)
    assert path == root / "alpha"
    assert rel.parts == ()


def test_normalizes_dots_and_slashes(root: Path) -> None:
    path, rel = resolve_agent_path(root, "alpha", "./workspace//notes.md")
    assert rel.as_posix() == "workspace/notes.md"
    assert path == root / "alpha" / "workspace" / "notes.md"


def test_rejects_symlink_to_other_agent(root: Path) -> None:
    (root / "alpha" / "link").symlink_to(root / "beta")
    with pytest.raises(InvalidPathError):
        resolve_agent_path(root, "alpha", "link/secret.txt")


def test_rejects_symlink_file_even_inside_folder(root: Path) -> None:
    (root / "alpha" / "real.txt").write_text("x")
    (root / "alpha" / "alias.txt").symlink_to(root / "alpha" / "real.txt")
    with pytest.raises(InvalidPathError):
        resolve_agent_path(root, "alpha", "alias.txt")


def test_rejects_symlink_to_host_file(root: Path) -> None:
    (root / "alpha" / "workspace" / "host.txt").symlink_to(root.parent / "outside.txt")
    with pytest.raises(InvalidPathError):
        resolve_agent_path(root, "alpha", "workspace/host.txt")


def test_rejects_symlinked_agent_folder(root: Path) -> None:
    (root / "gamma").symlink_to(root / "beta")
    with pytest.raises(NotFoundError):
        resolve_agent_path(root, "gamma", "secret.txt")


def test_missing_agent(root: Path) -> None:
    with pytest.raises(NotFoundError):
        resolve_agent_path(root, "nope", "x")


@pytest.mark.parametrize("slug", ["", "A", "-x", "x-", "a_b", "../beta", "a/b", ".trash", "x" * 51])
def test_invalid_slugs(slug: str) -> None:
    with pytest.raises(InvalidArgumentError):
        validate_slug(slug)


def test_system_slug_rules() -> None:
    assert validate_slug("_builder") == "_builder"
    with pytest.raises(InvalidArgumentError):
        validate_slug("_builder", allow_system=False)


def test_normalize_keeps_unicode_names() -> None:
    assert normalize_rel_path("notes/résumé.md").as_posix() == "notes/résumé.md"
