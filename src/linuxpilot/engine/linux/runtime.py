"""Which runtime a program is built on, read from its installed files (no list of app names).

Every Chromium runtime (browsers, Electron and CEF apps) ships its ICU data and V8 start-up snapshot
next to its executable; finding one of those files is what makes a program "Chromium-based".
"""

from __future__ import annotations

import os
import shutil
from collections.abc import Sequence
from functools import lru_cache
from pathlib import Path

CHROMIUM_RUNTIME_FILES = frozenset({"icudtl.dat", "v8_context_snapshot.bin", "snapshot_blob.bin"})
SCAN_BUDGET = 20000
"""Directory entries looked at per search, so a huge install tree cannot stall a launch."""
PACKAGE_DEPTH = 5
"""Snap and Flatpak layouts are the packager's choice: search this many levels down."""
OWN_DIR_DEPTH = 2


def shared_program_dirs(environ: dict[str, str] | None = None) -> frozenset[str]:
    """Directories many programs install into (``PATH`` entries and their lib/libexec): no identity there."""
    env = os.environ if environ is None else environ
    dirs: set[str] = set()
    for entry in env.get("PATH", "").split(os.pathsep):
        if entry:
            real = os.path.realpath(os.path.expanduser(entry))
            prefix = os.path.dirname(real)
            dirs.update({real, *(os.path.join(prefix, sub) for sub in ("libexec", "lib", "lib64"))})
    return frozenset(dirs)


def program_of_command(argv: Sequence[str]) -> tuple[str, list[str]]:
    """The program a command runs and its arguments, skipping ``env`` and its assignments."""
    parts = list(argv)
    if parts and Path(parts[0]).name == "env":
        parts = parts[1:]
    while parts and (("=" in parts[0] and not parts[0].startswith("/")) or parts[0].startswith("-")):
        parts = parts[1:]
    return (parts[0], parts[1:]) if parts else ("", [])


def install_dirs(program: str, args: Sequence[str] = (), *, home: Path | None = None) -> list[tuple[str, int]]:
    """Where ``program`` is installed, as ``(directory, search depth)``; depth 0 for shared directories."""
    resolved = shutil.which(program) or program
    real = os.path.realpath(resolved)
    if resolved.startswith("/snap/bin/"):
        return [(f"/snap/{Path(resolved).name.split('.', 1)[0]}/current", PACKAGE_DEPTH)]
    if Path(real).name == "flatpak":
        app_id = next((a for a in args if a and not a.startswith("-") and a != "run"), "")
        if not app_id:
            return []
        roots = ((home or Path.home()) / ".local/share/flatpak", Path("/var/lib/flatpak"))
        return [(str(r / "app" / app_id / "current" / "active" / "files"), PACKAGE_DEPTH) for r in roots]
    directory = os.path.dirname(real)
    return [(directory, 0 if directory in shared_program_dirs() else OWN_DIR_DEPTH)]


@lru_cache(maxsize=256)
def has_chromium_runtime(directory: str, depth: int = 0) -> bool:
    """Whether ``directory`` (searched ``depth`` levels down) holds Chromium runtime files."""
    budget = SCAN_BUDGET
    stack = [(directory, 0)]
    while stack and budget > 0:
        path, level = stack.pop()
        try:
            with os.scandir(path) as entries:
                for entry in entries:
                    budget -= 1
                    if entry.name in CHROMIUM_RUNTIME_FILES:
                        return True
                    if level < depth and entry.is_dir(follow_symlinks=False):
                        stack.append((entry.path, level + 1))
        except OSError:
            continue
    return False


def is_chromium_command(argv: Sequence[str]) -> bool:
    """Whether the command starts a Chromium-based program (browser, Electron or CEF app)."""
    program, args = program_of_command(argv)
    return bool(program) and any(has_chromium_runtime(d, depth) for d, depth in install_dirs(program, args))
