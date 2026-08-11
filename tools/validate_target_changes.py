#!/usr/bin/env python3
"""Fail closed unless a target checkout changed only approved media data files."""
from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from pathlib import Path

EXACT = {
    "news/feed.json",
    "news/feed.js",
    "news/headlines.json",
    "news/archive/manifest.json",
    "news/archive/media_stats.json",
    "news/archive/media-sweep-state.json",
}
MONTHLY = re.compile(r"news/archive/media_[0-9]{4}-(?:0[1-9]|1[0-2])\.json\Z")
STALE_STATE = re.compile(
    r"news/archive/media-sweep-state\.json\.stale-[0-9]{8}T[0-9]{6}Z(?:-[0-9]+)?\Z"
)


def git(repo: Path, *args: str) -> bytes:
    return subprocess.check_output(["git", "-C", str(repo), *args])


def split_z(payload: bytes) -> set[str]:
    return {item.decode("utf-8") for item in payload.split(b"\0") if item}


def allowed(path: str) -> bool:
    return path in EXACT or MONTHLY.fullmatch(path) is not None or STALE_STATE.fullmatch(path) is not None


def assert_regular_path(repo: Path, relative: str) -> None:
    candidate = repo / relative
    cursor = repo
    for part in Path(relative).parts:
        cursor /= part
        if cursor.is_symlink():
            raise RuntimeError(f"symlink is forbidden: {relative}")
    if candidate.exists() and not candidate.is_file():
        raise RuntimeError(f"non-regular target is forbidden: {relative}")
    try:
        common = os.path.commonpath((str(repo.resolve()), str(candidate.resolve(strict=False))))
    except OSError as exc:
        raise RuntimeError(f"cannot resolve target path: {relative}: {exc}") from exc
    if common != str(repo.resolve()):
        raise RuntimeError(f"path escapes the checkout: {relative}")


def changed_paths(repo: Path, staged_only: bool) -> set[str]:
    if staged_only:
        return split_z(git(repo, "diff", "--cached", "--name-only", "-z", "--"))
    paths = split_z(git(repo, "diff", "--name-only", "-z", "--"))
    paths |= split_z(git(repo, "diff", "--cached", "--name-only", "-z", "--"))
    paths |= split_z(git(repo, "ls-files", "--others", "--exclude-standard", "-z", "--"))
    return paths


def staged_modes(repo: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    for record in git(repo, "ls-files", "--stage", "-z", "--").split(b"\0"):
        if not record:
            continue
        metadata, raw_path = record.split(b"\t", 1)
        mode = metadata.split(b" ", 1)[0].decode("ascii")
        result[raw_path.decode("utf-8")] = mode
    return result


def validate(repo: Path, staged_only: bool) -> list[str]:
    repo = repo.resolve()
    if not (repo / ".git").exists():
        raise RuntimeError(f"not a Git checkout: {repo}")
    paths = sorted(changed_paths(repo, staged_only))
    rejected = [path for path in paths if not allowed(path)]
    if rejected:
        raise RuntimeError("non-allowlisted target changes: " + ", ".join(rejected))
    for path in paths:
        assert_regular_path(repo, path)
    for required in ("news/feed.json", "news/feed.js", "news/headlines.json"):
        target = repo / required
        if not target.is_file() or target.is_symlink():
            raise RuntimeError(f"required feed output is missing or unsafe: {required}")
    if staged_only:
        modes = staged_modes(repo)
        bad_modes = [path for path in paths if path in modes and modes[path] != "100644"]
        if bad_modes:
            raise RuntimeError("staged mode must be 100644: " + ", ".join(bad_modes))
    return paths


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--staged", action="store_true")
    args = parser.parse_args()
    try:
        paths = validate(args.repo, args.staged)
    except (RuntimeError, subprocess.CalledProcessError, UnicodeDecodeError) as exc:
        print(f"target change validation failed: {exc}", file=sys.stderr)
        return 2
    print(f"target change validation PASS ({len(paths)} paths, staged={args.staged})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
