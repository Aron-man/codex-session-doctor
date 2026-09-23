#!/usr/bin/env python3
"""Build the current platform's standalone release archive."""

import hashlib
import importlib
import platform
import subprocess
import sys
import tarfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def target():
    systems = {"Darwin": "darwin", "Linux": "linux"}
    machines = {"arm64": "arm64", "aarch64": "arm64", "x86_64": "amd64", "AMD64": "amd64"}
    system = systems.get(platform.system())
    machine = machines.get(platform.machine())
    if not system or not machine:
        raise SystemExit(f"Unsupported build platform: {platform.system()} {platform.machine()}")
    return system, machine


def main():
    try:
        importlib.import_module("PyInstaller")
    except ImportError as exc:
        raise SystemExit("PyInstaller is missing; install requirements-build.txt in an isolated environment") from exc

    system, machine = target()
    sys.path.insert(0, str(ROOT))
    from session_doctor import __version__

    name = f"codex-doctor-{system}-{machine}"
    dist = ROOT / "dist"
    dist.mkdir(exist_ok=True)
    work = ROOT / "build"
    binary_dir = work / "pyinstaller-dist"
    subprocess.run(
        [
            sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--onefile",
            "--name", "codex-doctor", "--distpath", str(binary_dir),
            "--workpath", str(work / "pyinstaller-work"),
            "--specpath", str(work), "--add-data", f"{ROOT / 'web'}:web",
            str(ROOT / "doctor.py"),
        ],
        cwd=ROOT,
        check=True,
    )
    binary = binary_dir / "codex-doctor"
    archive = dist / f"{name}.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        for source, member in ((binary, "codex-doctor"), (ROOT / "LICENSE", "LICENSE"), (ROOT / "README.md", "README.md")):
            if not source.is_file():
                raise SystemExit(f"Missing release file: {source}")
            tar.add(source, arcname=member, recursive=False)
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    checksum = dist / f"{name}.sha256"
    checksum.write_text(f"{digest}  {archive.name}\n", encoding="ascii")
    print(f"version={__version__} archive={archive} sha256={digest}")


if __name__ == "__main__":
    main()
