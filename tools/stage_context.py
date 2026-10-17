"""Stages the image's build context: every repo on sources.txt, copied side by side from <root>
into <dest> as its working tree stands, tracked and untracked files alike, without what git
ignores (.venv, site/, node_modules, out/).

    python3 tools/stage_context.py <root> <dest>

`kc project build` runs it before kaniko: kaniko streams its whole context to the builder, and
/work holds far more than the image reads. Symlinks are copied as symlinks. Stdlib only, for the
dev container's python3.
"""

import os
import shutil
import subprocess
import sys
from pathlib import Path

SOURCES = Path(__file__).resolve().parent.parent / "sources.txt"


def read_sources() -> list[str]:
    lines = (line.strip() for line in SOURCES.read_text(encoding="utf8").splitlines())
    return [line for line in lines if line and not line.startswith("#")]


def stage(repo: Path, dest: Path) -> int:
    listed = subprocess.run(
        ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
        cwd=repo,
        capture_output=True,
        check=True,
    ).stdout.split(b"\0")
    count = 0
    for name in filter(None, (entry.decode() for entry in listed)):
        source, target = repo / name, dest / name
        # A tracked path deleted or turned into a directory in the working tree: its files, if
        # any, are listed on their own.
        if not os.path.lexists(source) or (source.is_dir() and not source.is_symlink()):
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        if source.is_symlink():
            os.symlink(os.readlink(source), target)
        else:
            shutil.copy2(source, target)
        count += 1
    return count


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: stage_context.py <root> <dest>", file=sys.stderr)
        return 2
    root, dest = Path(argv[0]).resolve(), Path(argv[1]).resolve()
    shutil.rmtree(dest, ignore_errors=True)
    dest.mkdir(parents=True)
    for name in read_sources():
        if not (root / name).is_dir():
            print(f"stage_context: {root / name}: missing; sources.txt names it", file=sys.stderr)
            return 1
        print(f"stage_context: {name}: {stage(root / name, dest / name)} files", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
