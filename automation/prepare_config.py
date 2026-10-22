"""Read configuration as Git data, without executing anything from the PR."""

import argparse
import re
import subprocess
from pathlib import Path, PurePosixPath


def prepare(repository, base, head, path, output):
    if not all(re.fullmatch(r"[0-9a-f]{40}", sha) for sha in (base, head)):
        raise ValueError("Expected immutable commit SHAs")
    relative = PurePosixPath(path)
    if (
        relative.is_absolute()
        or ".." in relative.parts
        or not relative.parts
        or "\\" in path
        or any(ord(char) < 32 for char in path)
    ):
        raise ValueError("Invalid configuration path")
    actual = subprocess.check_output(
        ["git", "-C", str(repository), "rev-parse", "HEAD"], text=True
    ).strip()
    if actual != head:
        raise ValueError("PR changed during checkout; rerun on the latest revision")
    blob = f"{base}:{relative.as_posix()}"
    size = int(
        subprocess.check_output(
            ["git", "-C", str(repository), "cat-file", "-s", blob], text=True
        )
    )
    if size > 32000:
        raise ValueError("Configuration is too large")
    output.write_bytes(
        subprocess.check_output(["git", "-C", str(repository), "show", blob])
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--repository", type=Path, required=True)
    parser.add_argument("--base", required=True)
    parser.add_argument("--head", required=True)
    parser.add_argument("--path", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    prepare(args.repository, args.base, args.head, args.path, args.output)
