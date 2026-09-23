import hashlib
import io
import os
from pathlib import Path
import subprocess
import tarfile
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
INSTALLER = ROOT / "install.sh"


class InstallerTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.fixture = self.root / "release"
        self.fixture.mkdir()
        self.shims = self.root / "shims"
        self.shims.mkdir()
        self.bin_dir = self.root / "bin"
        self.asset = "codex-doctor-darwin-arm64"
        (self.shims / "uname").write_text('#!/bin/sh\ncase "$1" in -s) echo Darwin;; -m) echo arm64;; esac\n')
        (self.shims / "curl").write_text(
            '#!/bin/sh\nfor last; do :; done\n'
            '[ -z "${TEST_CURL_MARKER:-}" ] || : > "$TEST_CURL_MARKER"\n'
            'while [ "$#" -gt 0 ]; do\n'
            '  if [ "$1" = -o ]; then output=$2; shift 2; else shift; fi\n'
            'done\ncp "$TEST_RELEASE_DIR/${last##*/}" "$output"\n'
        )
        for shim in self.shims.iterdir():
            shim.chmod(0o755)
        self.env = dict(os.environ, PATH=f"{self.shims}:{os.environ['PATH']}", TEST_RELEASE_DIR=str(self.fixture))

    def release(self, payload=b"#!/bin/sh\necho fixture\n", checksum=None):
        archive = self.fixture / f"{self.asset}.tar.gz"
        with tarfile.open(archive, "w:gz") as tar:
            info = tarfile.TarInfo("codex-doctor")
            info.mode = 0o755
            info.size = len(payload)
            tar.addfile(info, io.BytesIO(payload))
        digest = checksum or hashlib.sha256(archive.read_bytes()).hexdigest()
        (self.fixture / f"{self.asset}.sha256").write_text(f"{digest}  {archive.name}\n")

    def install(self, extra=()):
        return subprocess.run(
            ["sh", str(INSTALLER), "--repo", "owner/codex-session-doctor", "--install-dir", str(self.bin_dir), *extra],
            env=self.env, text=True, capture_output=True,
        )

    def test_install_and_update(self):
        self.release()
        first = self.install()
        self.assertEqual(first.returncode, 0, first.stderr)
        target = self.bin_dir / "codex-doctor"
        self.assertEqual(subprocess.check_output([str(target)]).strip(), b"fixture")
        self.release(b"#!/bin/sh\necho updated\n")
        second = self.install(("--version", "v0.1.2"))
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual(subprocess.check_output([str(target)]).strip(), b"updated")

    def test_failed_checksum_keeps_old_binary(self):
        self.bin_dir.mkdir()
        target = self.bin_dir / "codex-doctor"
        target.write_bytes(b"old binary")
        self.release(checksum="0" * 64)
        result = self.install()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Checksum mismatch", result.stderr)
        self.assertEqual(target.read_bytes(), b"old binary")

    def test_unsupported_platform(self):
        (self.shims / "uname").write_text('#!/bin/sh\necho Windows\n')
        result = self.install()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Unsupported operating system", result.stderr)
        self.assertFalse(self.bin_dir.exists())

    def test_intel_mac_is_rejected_before_download_and_keeps_install(self):
        (self.shims / "uname").write_text('#!/bin/sh\ncase "$1" in -s) echo Darwin;; -m) echo x86_64;; esac\n')
        self.bin_dir.mkdir()
        target = self.bin_dir / "codex-doctor"
        target.write_bytes(b"existing binary")
        marker = self.root / "curl-was-called"
        self.env["TEST_CURL_MARKER"] = str(marker)
        result = self.install()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Unsupported macOS architecture", result.stderr)
        self.assertFalse(marker.exists())
        self.assertEqual(target.read_bytes(), b"existing binary")


if __name__ == "__main__":
    unittest.main()
