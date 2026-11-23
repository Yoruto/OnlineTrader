"""Canonical Git worktree candidate identity shared by preflight and QA."""

from __future__ import annotations

import hashlib
from pathlib import Path
import subprocess


CANDIDATE_TREE_ALGORITHM = "ONLINE_TRADER_CANDIDATE_TREE/1"


def sha256_file(path: Path) -> str:
    """Return the lowercase SHA-256 hex digest of a file using bounded chunks."""
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _git(root: Path, *arguments: str) -> bytes:
    return subprocess.run(
        ["git", *arguments],
        cwd=root,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    ).stdout


def compute_candidate_identity(root: Path, *, include_entries: bool = True) -> dict[str, object]:
    """Compute the canonical commit and working-tree digest for a candidate root."""
    root = root.resolve()
    head = _git(root, "rev-parse", "HEAD").decode("ascii").strip()
    tracked = _git(root, "ls-files", "-z").split(b"\0")
    untracked = _git(root, "ls-files", "--others", "--exclude-standard", "-z").split(b"\0")
    paths = sorted({item.decode("utf-8", "surrogateescape") for item in (*tracked, *untracked) if item})
    tree = hashlib.sha256()
    entries: list[dict[str, str]] = []
    for relative in paths:
        path = root / relative
        normalized = relative.replace("\\", "/")
        if path.is_symlink():
            file_hash = hashlib.sha256(path.readlink().as_posix().encode("utf-8")).hexdigest()
            marker = "symlink"
        elif path.is_file():
            file_hash = sha256_file(path)
            marker = "file"
        else:
            file_hash = "-"
            marker = "missing"
        record = f"{marker}\0{normalized}\0{file_hash}\n".encode("utf-8", "surrogateescape")
        tree.update(record)
        if include_entries:
            entries.append({"path": normalized, "kind": marker, "sha256": file_hash})
    identity: dict[str, object] = {
        "algorithm": CANDIDATE_TREE_ALGORITHM,
        "head": head,
        "tree_sha256": tree.hexdigest(),
        "entry_count": len(paths),
    }
    if include_entries:
        identity["entries"] = entries
    return identity
