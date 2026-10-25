# SPDX-License-Identifier: MIT
"""scripts/send-to-armada.sh, run against stand-ins for ssh and scp that act on a fake remote home folder."""
import os
import shutil
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "send-to-armada.sh"

FAKE_SSH = r"""#!/usr/bin/env bash
# stand-in for ssh: skips the options, then runs the command locally with HOME set to the fake remote home
[ -n "${FAKE_DOWN:-}" ] && exit 255
args=("$@"); i=0
while [ $i -lt ${#args[@]} ]; do
    case "${args[$i]}" in -i|-o|-p) i=$((i+2)) ;; -*) i=$((i+1)) ;; *) break ;; esac
done
cmd="${args[@]:$((i+1))}"
HOME="$FAKE_REMOTE_HOME" bash -c "cd ~ && $cmd"
"""

FAKE_SCP = r"""#!/usr/bin/env bash
# stand-in for scp: copies the source into <fake remote home>/Downloads, optionally cutting it short
[ -n "${FAKE_DOWN:-}" ] && exit 255
args=("$@"); files=(); i=0
while [ $i -lt ${#args[@]} ]; do
    case "${args[$i]}" in -i|-o|-P) i=$((i+2)) ;; -*) i=$((i+1)) ;; *) files+=("${args[$i]}"); i=$((i+1)) ;; esac
done
src="${files[0]}"; dest="$FAKE_REMOTE_HOME/Downloads/"
mkdir -p "$dest"
if [ -n "${FAKE_CORRUPT:-}" ]; then head -c 1000 "$src" > "$dest/$(basename "$src")"; else cp "$src" "$dest/"; fi
"""


class SendScriptTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="send-test-"))
        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        for name, body in (("ssh", FAKE_SSH), ("scp", FAKE_SCP)):
            p = self.bin / name
            p.write_text(body)
            p.chmod(p.stat().st_mode | stat.S_IXUSR)
        self.remote = self.tmp / "remote-home"
        self.remote.mkdir()
        self.zip = self.tmp / "RomM-Armada-9.9.9.zip"
        self.zip.write_bytes(os.urandom(300_000))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_script(self, *args, **env):
        e = {**os.environ, "PATH": f"{self.bin}:{os.environ['PATH']}", "FAKE_REMOTE_HOME": str(self.remote), **env}
        return subprocess.run(["bash", str(SCRIPT), *args], capture_output=True, text=True, env=e, timeout=60)

    def test_sends_and_proves_it_arrived(self):
        p = self.run_script(str(self.zip))
        self.assertEqual(p.returncode, 0, p.stderr + p.stdout)
        self.assertIn("The handheld answers", p.stdout)
        self.assertIn("Arrived intact", p.stdout)
        self.assertIn("Install Plugin from Zip", p.stdout)
        self.assertEqual((self.remote / "Downloads" / self.zip.name).read_bytes(), self.zip.read_bytes())

    def test_a_missing_downloads_folder_is_created(self):
        self.assertFalse((self.remote / "Downloads").exists())
        self.assertEqual(self.run_script(str(self.zip)).returncode, 0)
        self.assertTrue((self.remote / "Downloads").is_dir())

    def test_check_only_copies_nothing(self):
        p = self.run_script("--check")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("The handheld answers", p.stdout)
        self.assertFalse((self.remote / "Downloads").exists())

    def test_a_handheld_that_is_off_is_explained_and_nothing_is_sent(self):
        p = self.run_script(str(self.zip), FAKE_DOWN="1")
        self.assertEqual(p.returncode, 1)
        self.assertIn("does not answer", p.stderr)
        self.assertIn("Tailscale", p.stderr)
        self.assertFalse((self.remote / "Downloads").exists())

    def test_a_damaged_copy_is_caught(self):
        p = self.run_script(str(self.zip), FAKE_CORRUPT="1")
        self.assertEqual(p.returncode, 3)
        self.assertIn("does not match", p.stderr)

    def test_nothing_to_send(self):
        e = {**os.environ, "PATH": f"{self.bin}:{os.environ['PATH']}", "FAKE_REMOTE_HOME": str(self.remote)}
        p = subprocess.run(["bash", str(SCRIPT), str(self.tmp / "nope.zip")], capture_output=True, text=True, env=e, timeout=30)
        self.assertEqual(p.returncode, 2)
        self.assertIn("scripts/package.sh", p.stderr)

    def test_a_file_name_with_spaces_or_quotes_is_not_a_problem(self):
        odd = self.tmp / "RomM Armada 0.1 test.zip"
        odd.write_bytes(os.urandom(5000))
        p = self.run_script(str(odd))
        self.assertEqual(p.returncode, 0, p.stderr + p.stdout)
        self.assertTrue((self.remote / "Downloads" / odd.name).exists())


if __name__ == "__main__":
    unittest.main()
