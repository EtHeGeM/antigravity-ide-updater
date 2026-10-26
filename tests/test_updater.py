"""Comprehensive test suite for Antigravity IDE Updater."""

from __future__ import annotations

import io
import json
import os
import platform
import shutil
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import antigravity_updater as updater
from antigravity_changelog import ChangelogError


class TestVersionParsing(unittest.TestCase):
    def test_valid_versions(self):
        self.assertEqual(updater.parse_version("1.0.0"), (1, 0, 0))
        self.assertEqual(updater.parse_version("2.5.5"), (2, 5, 5))
        self.assertEqual(updater.parse_version("10.200.3000"), (10, 200, 3000))

    def test_version_with_v_prefix(self):
        self.assertEqual(updater.parse_version("v2.5.5"), (2, 5, 5))
        self.assertEqual(updater.parse_version(" v2.5.5 "), (2, 5, 5))

    def test_version_with_build_metadata(self):
        self.assertEqual(updater.parse_version("2.5.5-4923483625488384"), (2, 5, 5))
        self.assertEqual(updater.parse_version("2.5.5+build.123"), (2, 5, 5))

    def test_version_comparison(self):
        self.assertGreater(updater.parse_version("2.6.0"), updater.parse_version("2.5.5"))
        self.assertGreater(updater.parse_version("2.5.6"), updater.parse_version("2.5.5"))
        self.assertEqual(updater.parse_version("2.5.5"), updater.parse_version("v2.5.5"))
        self.assertLess(updater.parse_version("2.5.4"), updater.parse_version("2.5.5"))

    def test_malformed_versions(self):
        with self.assertRaises((ValueError, TypeError)):
            updater.parse_version(None)
        with self.assertRaises(ValueError):
            updater.parse_version("")
        with self.assertRaises(ValueError):
            updater.parse_version("no_digits_here")


class TestArchitecture(unittest.TestCase):
    @patch("platform.machine", return_value="x86_64")
    def test_x86_64_detection(self, _):
        self.assertEqual(updater.get_system_arch(), "x64")

    @patch("platform.machine", return_value="AMD64")
    def test_amd64_detection(self, _):
        self.assertEqual(updater.get_system_arch(), "x64")

    @patch("platform.machine", return_value="aarch64")
    def test_aarch64_detection(self, _):
        self.assertEqual(updater.get_system_arch(), "arm64")

    @patch("platform.machine", return_value="arm64")
    def test_arm64_detection(self, _):
        self.assertEqual(updater.get_system_arch(), "arm64")

    @patch("platform.machine", return_value="mips64")
    def test_unsupported_arch(self, _):
        with self.assertRaises(updater.UpdateError):
            updater.get_system_arch()

    def test_arch_matching(self):
        self.assertTrue(updater.arch_matches("x64", "x64"))
        self.assertTrue(updater.arch_matches("amd64", "x64"))
        self.assertTrue(updater.arch_matches("arm", "arm64"))
        self.assertTrue(updater.arch_matches("arm64", "arm64"))
        self.assertTrue(updater.arch_matches("aarch64", "arm64"))
        self.assertFalse(updater.arch_matches("x64", "arm64"))
        self.assertFalse(updater.arch_matches("arm64", "x64"))


class TestPathSafety(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.base_path = Path(self.temp_dir.name)

    def tearDown(self):
        self.temp_dir.cleanup()

    def create_mock_ide(self, root: Path, version: str = "2.5.5"):
        pj = root / "resources" / "app" / "product.json"
        pj.parent.mkdir(parents=True, exist_ok=True)
        pj.write_text(json.dumps({"name": "Antigravity", "ideVersion": version}))

    def test_root_and_system_paths_rejected(self):
        for sys_dir in ("/usr", "/etc", "/var", "/bin", "/home", "/"):
            with self.assertRaises(updater.UpdateError):
                updater.assert_safe(Path(sys_dir), require_installed=False)

    def test_home_and_user_protected_paths_rejected(self):
        home = Path.home()
        for prot in (home, home / ".config", home / ".local", home / ".antigravity", home / ".gemini"):
            with self.assertRaises(updater.UpdateError):
                updater.assert_safe(prot, require_installed=False)

    def test_parent_cannot_be_root_or_home(self):
        with self.assertRaises(updater.UpdateError):
            updater.assert_safe(Path("/arbitrary_ide"), require_installed=False)
        with self.assertRaises(updater.UpdateError):
            updater.assert_safe(Path("/home/arbitrary_ide"), require_installed=False)

    def test_nonexistent_parent_rejected(self):
        with self.assertRaises(updater.UpdateError):
            updater.assert_safe(self.base_path / "nonexistent" / "ide", require_installed=False)

    def test_empty_path_rejected(self):
        with self.assertRaises(updater.UpdateError):
            updater.assert_safe(Path(""), require_installed=False)

    def test_missing_product_json_rejected_when_required(self):
        target = self.base_path / "app"
        target.mkdir()
        with self.assertRaises(updater.UpdateError):
            updater.assert_safe(target, require_installed=True)

    def test_invalid_product_json_rejected(self):
        target = self.base_path / "app"
        target.mkdir()
        pj = target / "resources" / "app" / "product.json"
        pj.parent.mkdir(parents=True)
        pj.write_text("invalid json")
        with self.assertRaises(updater.UpdateError):
            updater.assert_safe(target, require_installed=True)

    def test_valid_ide_path_accepted(self):
        target = self.base_path / "app"
        self.create_mock_ide(target, "2.5.5")
        # Should not raise
        updater.assert_safe(target, require_installed=True)


class TestDownloadSecurity(unittest.TestCase):
    def test_trusted_urls(self):
        self.assertTrue(updater.is_trusted_url("https://antigravity.google/download/linux"))
        self.assertTrue(updater.is_trusted_url("https://edgedl.me.gvt1.com/edgedl/release2/antigravity/stable/ide.tar.gz"))
        self.assertTrue(updater.is_trusted_url("https://storage.googleapis.com/antigravity/stable/ide.tar.gz"))
        self.assertTrue(updater.is_trusted_url("https://dl.google.com/antigravity/ide.tar.gz"))

    def test_untrusted_urls(self):
        self.assertFalse(updater.is_trusted_url("http://antigravity.google/download/linux"))
        self.assertFalse(updater.is_trusted_url("https://malicious-site.com/ide.tar.gz"))
        self.assertFalse(updater.is_trusted_url("https://google.com.attacker.com/ide.tar.gz"))
        self.assertFalse(updater.is_trusted_url("file:///etc/passwd"))

    def test_safe_redirect_handler(self):
        handler = updater.SafeRedirectHandler()
        req = MagicMock()
        fp = MagicMock()

        # Insecure redirect
        with self.assertRaises(updater.UpdateError):
            handler.redirect_request(req, fp, 302, "Found", {}, "http://antigravity.google/insecure")

        # Untrusted host redirect
        with self.assertRaises(updater.UpdateError):
            handler.redirect_request(req, fp, 302, "Found", {}, "https://evil.com/fake")

    def test_download_size_limit_exceeded(self):
        with tempfile.TemporaryDirectory() as td:
            dest = Path(td) / "test.tar.gz"
            mock_resp = MagicMock()
            mock_resp.headers = {"Content-Length": "2000"}
            mock_resp.read.return_value = b"x" * 1024

            mock_opener = MagicMock()
            mock_opener.open.return_value.__enter__.return_value = mock_resp

            with patch("urllib.request.build_opener", return_value=mock_opener):
                with self.assertRaises(updater.UpdateError):
                    updater.download_file("https://antigravity.google/test.tar.gz", dest, max_size=500, timeout=10)

            # Ensure partial file is cleaned up
            self.assertFalse(dest.exists())

    def test_download_network_connectivity_failure(self):
        import urllib.error

        with tempfile.TemporaryDirectory() as td:
            dest = Path(td) / "test.tar.gz"
            mock_opener = MagicMock()
            mock_opener.open.side_effect = urllib.error.URLError("Network unreachable")

            with patch("urllib.request.build_opener", return_value=mock_opener):
                with self.assertRaises(urllib.error.URLError):
                    updater.download_file("https://antigravity.google/test.tar.gz", dest, max_size=1000, timeout=10)

            self.assertFalse(dest.exists())


class TestSafeTarballExtraction(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.base_path = Path(self.temp_dir.name)

    def tearDown(self):
        self.temp_dir.cleanup()

    def create_tarball(self, filename: str, members: list[tuple[str, bytes | str, str]]):
        tar_path = self.base_path / filename
        with tarfile.open(tar_path, "w:gz") as tar:
            for item in members:
                name, content, mtype = item
                if mtype == "file":
                    ti = tarfile.TarInfo(name=name)
                    data = content if isinstance(content, bytes) else content.encode()
                    ti.size = len(data)
                    tar.addfile(ti, io.BytesIO(data))
                elif mtype == "symlink":
                    ti = tarfile.TarInfo(name=name)
                    ti.type = tarfile.SYMTYPE
                    ti.linkname = content
                    tar.addfile(ti)
                elif mtype == "hardlink":
                    ti = tarfile.TarInfo(name=name)
                    ti.type = tarfile.LNKTYPE
                    ti.linkname = content
                    tar.addfile(ti)
                elif mtype == "fifo":
                    ti = tarfile.TarInfo(name=name)
                    ti.type = tarfile.FIFOTYPE
                    tar.addfile(ti)
        return tar_path

    def test_safe_extraction_succeeds(self):
        tar_path = self.create_tarball(
            "safe.tar.gz",
            [
                ("app/file1.txt", b"hello", "file"),
                ("app/sub/file2.txt", b"world", "file"),
                ("app/sym1", "file1.txt", "symlink"),
            ],
        )
        extract_dir = self.base_path / "extracted"
        updater.extract_tarball_safe(tar_path, extract_dir)
        self.assertTrue((extract_dir / "app" / "file1.txt").exists())
        self.assertTrue((extract_dir / "app" / "sub" / "file2.txt").exists())

    def test_path_traversal_rejected(self):
        tar_path = self.create_tarball(
            "traversal.tar.gz",
            [
                ("../escape.txt", b"evil", "file"),
            ],
        )
        extract_dir = self.base_path / "extracted"
        with self.assertRaises(updater.UpdateError):
            updater.extract_tarball_safe(tar_path, extract_dir)

    def test_symlink_escape_rejected(self):
        tar_path = self.create_tarball(
            "symlink_escape.tar.gz",
            [
                ("app/escape_link", "../../outside", "symlink"),
            ],
        )
        extract_dir = self.base_path / "extracted"
        with self.assertRaises(updater.UpdateError):
            updater.extract_tarball_safe(tar_path, extract_dir)

    def test_absolute_symlink_rejected(self):
        tar_path = self.create_tarball(
            "abs_symlink.tar.gz",
            [
                ("app/root_link", "/etc/passwd", "symlink"),
            ],
        )
        extract_dir = self.base_path / "extracted"
        with self.assertRaises(updater.UpdateError):
            updater.extract_tarball_safe(tar_path, extract_dir)

    def test_fifo_special_file_rejected(self):
        tar_path = self.create_tarball(
            "fifo.tar.gz",
            [
                ("app/my_fifo", "", "fifo"),
            ],
        )
        extract_dir = self.base_path / "extracted"
        with self.assertRaises(updater.UpdateError):
            updater.extract_tarball_safe(tar_path, extract_dir)


class TestAtomicInstallationAndRollback(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.base_path = Path(self.temp_dir.name)
        self.install_dir = self.base_path / "Antigravity IDE"
        self.pending_dir = self.base_path / "state" / "pending"
        self.state_dir = self.base_path / "state"

        # Patch environment and variables in updater
        self.patch_install_dir = patch.object(updater, "INSTALL_DIR", self.install_dir)
        self.patch_pending_dir = patch.object(updater, "PENDING_DIR", self.pending_dir)
        self.patch_state_dir = patch.object(updater, "STATE_DIR", self.state_dir)
        self.patch_install_dir.start()
        self.patch_pending_dir.start()
        self.patch_state_dir.start()

    def tearDown(self):
        self.patch_install_dir.stop()
        self.patch_pending_dir.stop()
        self.patch_state_dir.stop()
        self.temp_dir.cleanup()

    def create_mock_ide(self, root: Path, version: str):
        pj = root / "resources" / "app" / "product.json"
        pj.parent.mkdir(parents=True, exist_ok=True)
        pj.write_text(json.dumps({"name": "Antigravity", "ideVersion": version}))
        (root / "bin").mkdir(parents=True, exist_ok=True)
        (root / "bin" / "antigravity").write_text("#!/bin/sh\n")

    def test_successful_atomic_install(self):
        # Create initial install
        self.create_mock_ide(self.install_dir, "2.5.5")

        # Create staged update
        staged_root = self.pending_dir / "2.5.6" / "x" / "Antigravity IDE"
        self.create_mock_ide(staged_root, "2.5.6")

        updater.install(staged_root, "2.5.6")

        # Verify new installation
        self.assertEqual(updater.installed_version(self.install_dir), "2.5.6")
        # Verify backup exists and contains previous version
        backup_dir = self.install_dir.with_name(f"{self.install_dir.name}.bak")
        self.assertTrue(backup_dir.exists())
        self.assertEqual(updater.installed_version(backup_dir), "2.5.5")
        # Staged version should be cleaned up
        self.assertFalse((self.pending_dir / "2.5.6").exists())

    def test_failed_swap_triggers_rollback(self):
        self.create_mock_ide(self.install_dir, "2.5.5")

        # Create staged root with a mismatched version inside
        staged_root = self.pending_dir / "2.5.6" / "x" / "Antigravity IDE"
        self.create_mock_ide(staged_root, "2.5.5")  # Corrupted: says 2.5.5 instead of 2.5.6

        with self.assertRaises(updater.UpdateError):
            updater.install(staged_root, "2.5.6")

        # Original install must remain completely intact
        self.assertTrue(self.install_dir.exists())
        self.assertEqual(updater.installed_version(self.install_dir), "2.5.5")

    def test_interrupted_install_recovery(self):
        # Scenario: IDE was renamed to .bak.tmp-12345, but crash occurred before new install took place
        temp_backup = self.base_path / f".{self.install_dir.name}.bak.tmp-12345"
        self.create_mock_ide(temp_backup, "2.5.5")

        # install_dir is missing!
        self.assertFalse(self.install_dir.exists())

        recovered = updater.recover_interrupted_install(self.install_dir)
        self.assertTrue(recovered)
        self.assertTrue(self.install_dir.exists())
        self.assertEqual(updater.installed_version(self.install_dir), "2.5.5")

    def test_interrupted_install_recovery_corrupt_install_dir_restores_from_backup(self):
        # Scenario: install_dir exists but is corrupt (empty directory without product.json)
        self.install_dir.mkdir(parents=True, exist_ok=True)
        temp_backup = self.base_path / f".{self.install_dir.name}.bak.tmp-12345"
        self.create_mock_ide(temp_backup, "2.5.5")

        recovered = updater.recover_interrupted_install(self.install_dir)
        self.assertTrue(recovered)
        self.assertTrue(self.install_dir.exists())
        self.assertEqual(updater.installed_version(self.install_dir), "2.5.5")

    def test_interrupted_install_recovery_preserves_backups_when_no_valid_source(self):
        # Scenario: install_dir is corrupt, and temp_backup is also corrupt/empty
        self.install_dir.mkdir(parents=True, exist_ok=True)
        temp_backup = self.base_path / f".{self.install_dir.name}.bak.tmp-12345"
        temp_backup.mkdir(parents=True, exist_ok=True)

        recovered = updater.recover_interrupted_install(self.install_dir)
        self.assertFalse(recovered)
        # Crucial safeguard: corrupt backup must NOT be deleted if there is no valid installation
        self.assertTrue(temp_backup.exists())

    def test_recovery_tries_subsequent_backups_when_latest_temp_is_corrupt(self):
        # Scenario: install_dir missing, latest temp backup is corrupt, but official backup is valid
        temp_backup = self.base_path / f".{self.install_dir.name}.bak.tmp-12345"
        temp_backup.mkdir(parents=True, exist_ok=True)  # Corrupt / empty
        backup_dir = self.install_dir.with_name(f"{self.install_dir.name}.bak")
        self.create_mock_ide(backup_dir, "2.5.4")

        recovered = updater.recover_interrupted_install(self.install_dir)
        self.assertTrue(recovered)
        self.assertTrue(self.install_dir.exists())
        self.assertEqual(updater.installed_version(self.install_dir), "2.5.4")

    def test_recovery_secures_missing_backup_when_active_install_healthy(self):
        # Scenario: active install updated to 2.5.6, but backup_dir is missing while temp_backup (2.5.5) exists
        self.create_mock_ide(self.install_dir, "2.5.6")
        temp_backup = self.base_path / f".{self.install_dir.name}.bak.tmp-12345"
        self.create_mock_ide(temp_backup, "2.5.5")
        backup_dir = self.install_dir.with_name(f"{self.install_dir.name}.bak")
        self.assertFalse(backup_dir.exists())

        recovered = updater.recover_interrupted_install(self.install_dir)
        self.assertFalse(recovered)
        # temp_backup should have been promoted to backup_dir instead of deleted!
        self.assertTrue(backup_dir.exists())
        self.assertEqual(updater.installed_version(backup_dir), "2.5.5")

    def test_idempotent_install(self):
        self.create_mock_ide(self.install_dir, "2.5.5")
        staged_root = self.pending_dir / "2.5.5" / "x" / "Antigravity IDE"
        self.create_mock_ide(staged_root, "2.5.5")

        # Should return without re-installing or failing
        updater.install(staged_root, "2.5.5")
        self.assertEqual(updater.installed_version(self.install_dir), "2.5.5")


    def test_backup_never_deleted_prematurely(self):
        self.create_mock_ide(self.install_dir, "2.5.5")
        backup_dir = self.install_dir.with_name(f"{self.install_dir.name}.bak")
        self.create_mock_ide(backup_dir, "2.5.4")

        staged_root = self.pending_dir / "2.5.6" / "x" / "Antigravity IDE"
        self.create_mock_ide(staged_root, "2.5.6")

        updater.install(staged_root, "2.5.6")

        # Now backup should contain 2.5.5
        self.assertTrue(backup_dir.exists())
        self.assertEqual(updater.installed_version(backup_dir), "2.5.5")


class TestStagingBehavior(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.base_path = Path(self.temp_dir.name)
        self.pending_dir = self.base_path / "pending"
        self.patch_pending = patch.object(updater, "PENDING_DIR", self.pending_dir)
        self.patch_pending.start()

    def tearDown(self):
        self.patch_pending.stop()
        self.temp_dir.cleanup()

    def create_mock_staged(self, version: str, content_version: str):
        root = self.pending_dir / version / "x" / "Antigravity IDE"
        pj = root / "resources" / "app" / "product.json"
        pj.parent.mkdir(parents=True, exist_ok=True)
        pj.write_text(json.dumps({"name": "Antigravity", "ideVersion": content_version}))
        return root

    def test_valid_staged_update(self):
        self.create_mock_staged("2.5.6", "2.5.6")
        root = updater.verify_pending("2.5.6")
        self.assertTrue(root.exists())

    def test_corrupted_staged_update_detected_and_purged(self):
        self.create_mock_staged("2.5.6", "2.5.5")  # Version mismatch
        with self.assertRaises(updater.UpdateError):
            updater.verify_pending("2.5.6")
        # Directory should have been purged
        self.assertFalse((self.pending_dir / "2.5.6").exists())

    def test_purge_stale_pending(self):
        self.create_mock_staged("2.5.5", "2.5.5")
        self.create_mock_staged("2.5.6", "2.5.6")
        (self.pending_dir / ".tmp-stage-2.5.7-123").mkdir()

        updater.purge_stale_pending(keep_version="2.5.6")

        self.assertFalse((self.pending_dir / "2.5.5").exists())
        self.assertTrue((self.pending_dir / "2.5.6").exists())
        self.assertFalse((self.pending_dir / ".tmp-stage-2.5.7-123").exists())


class TestConcurrencyLocking(unittest.TestCase):
    def test_flock_acquisition_and_contention(self):
        with tempfile.TemporaryDirectory() as td:
            lock_path = Path(td) / "test.lock"

            # First acquisition succeeds
            with updater.ProcessLock(lock_path) as lock1:
                self.assertIsNotNone(lock1.fd)
                pid_content = lock_path.read_text().strip()
                self.assertEqual(pid_content, str(os.getpid()))

                # Second acquisition attempt within same or sub process raises LockContentionError
                with self.assertRaises(updater.LockContentionError):
                    with updater.ProcessLock(lock_path):
                        pass

            # Once exited, lock can be re-acquired
            with updater.ProcessLock(lock_path) as lock2:
                self.assertIsNotNone(lock2.fd)


class TestIDERunningDetection(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.base_path = Path(self.temp_dir.name)
        self.install_dir = self.base_path / "Antigravity IDE"
        self.install_dir.mkdir()

    def tearDown(self):
        self.temp_dir.cleanup()

    @patch("os.listdir", return_value=["1001", "1002"])
    @patch("pathlib.Path.is_dir", return_value=True)
    def test_ide_running_matches_install_dir(self, _, __):
        def fake_readlink(path):
            if path == "/proc/1001/exe":
                return str(self.install_dir / "antigravity")
            return "/usr/bin/python3"

        with patch("os.readlink", side_effect=fake_readlink):
            self.assertTrue(updater.ide_running(self.install_dir))

    @patch("os.listdir", return_value=["1001"])
    @patch("pathlib.Path.is_dir", return_value=True)
    def test_ide_running_deleted_binary(self, _, __):
        fake_path = str(self.install_dir / "antigravity") + " (deleted)"
        with patch("os.readlink", return_value=fake_path):
            self.assertTrue(updater.ide_running(self.install_dir))

    @patch("os.listdir", return_value=["1001", "1002"])
    @patch("pathlib.Path.is_dir", return_value=True)
    def test_ide_running_unrelated_process(self, _, __):
        with patch("os.readlink", return_value="/usr/bin/bash"):
            self.assertFalse(updater.ide_running(self.install_dir))

    @patch("os.listdir", return_value=["1001"])
    @patch("pathlib.Path.is_dir", return_value=True)
    def test_process_exits_during_inspection(self, _, __):
        with patch("os.readlink", side_effect=FileNotFoundError):
            self.assertFalse(updater.ide_running(self.install_dir))

    def test_install_refuses_when_ide_running(self):
        with patch.object(updater, "ide_running", return_value=True):
            with self.assertRaises(updater.UpdateError):
                updater.install(self.install_dir, "2.5.6")


class TestReleaseDetection(unittest.TestCase):
    SAMPLE_DOM = """
    <html>
      <body>
        <a href="https://edgedl.me.gvt1.com/edgedl/release2/j0qc3/antigravity/stable/2.5.4-1234/linux-x64/Antigravity%20IDE.tar.gz">Older</a>
        <a href="https://edgedl.me.gvt1.com/edgedl/release2/j0qc3/antigravity/stable/2.5.5-5678/linux-x64/Antigravity%20IDE.tar.gz">Latest x64</a>
        <a href="https://edgedl.me.gvt1.com/edgedl/release2/j0qc3/antigravity/stable/2.5.5-5678/linux-arm/Antigravity%20IDE.tar.gz">Latest ARM</a>
      </body>
    </html>
    """

    @patch.object(updater, "fetch_download_page", return_value=SAMPLE_DOM)
    @patch.object(updater, "get_system_arch", return_value="x64")
    def test_latest_release_x64(self, _, __):
        ver, url = updater.latest_release()
        self.assertEqual(ver, "2.5.5")
        self.assertIn("linux-x64", url)

    @patch.object(updater, "fetch_download_page", return_value=SAMPLE_DOM)
    @patch.object(updater, "get_system_arch", return_value="arm64")
    def test_latest_release_arm64(self, _, __):
        ver, url = updater.latest_release()
        self.assertEqual(ver, "2.5.5")
        self.assertIn("linux-arm", url)

    def test_changelog_failure_does_not_abort(self):
        with patch("antigravity_updater.fetch_changelog", side_effect=ChangelogError("Network down")):
            notes = updater.release_notes("2.5.5")
            self.assertEqual(notes, "")

    @patch.object(updater, "fetch_download_page", return_value="<html><body>No download links here</body></html>")
    @patch.object(updater, "get_system_arch", return_value="x64")
    def test_empty_or_changed_dom_raises_update_error(self, _, __):
        with self.assertRaises(updater.UpdateError):
            updater.latest_release()

    @patch.object(
        updater,
        "fetch_download_page",
        return_value='<a href="https://evil.com/antigravity/stable/2.5.5-1234/linux-x64/Antigravity%20IDE.tar.gz">Untrusted</a>',
    )
    @patch.object(updater, "get_system_arch", return_value="x64")
    def test_untrusted_domain_in_dom_ignored(self, _, __):
        # Untrusted hosts should be filtered out by is_trusted_url, raising UpdateError when no valid links remain
        with self.assertRaises(updater.UpdateError):
            updater.latest_release()


class TestCLIOptions(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.base_path = Path(self.temp_dir.name)
        self.install_dir = self.base_path / "Antigravity IDE"
        self.patch_install_dir = patch.object(updater, "INSTALL_DIR", self.install_dir)
        self.patch_install_dir.start()

        pj = self.install_dir / "resources" / "app" / "product.json"
        pj.parent.mkdir(parents=True, exist_ok=True)
        pj.write_text(json.dumps({"name": "Antigravity", "ideVersion": "2.5.5"}))

    def tearDown(self):
        self.patch_install_dir.stop()
        self.temp_dir.cleanup()

    @patch.object(updater, "latest_release", return_value=("2.5.5", "https://edgedl.me.gvt1.com/test.tar.gz"))
    def test_cli_check_up_to_date(self, _):
        code = updater.check_updates()
        self.assertEqual(code, 0)

    @patch.object(updater, "latest_release", return_value=("2.5.6", "https://edgedl.me.gvt1.com/test.tar.gz"))
    @patch.object(updater, "release_notes", return_value="Some notes")
    def test_cli_check_update_available(self, _, __):
        code = updater.check_updates()
        self.assertEqual(code, 0)

    @patch.object(updater, "latest_release", return_value=("2.5.6", "https://edgedl.me.gvt1.com/test.tar.gz"))
    @patch.object(updater, "ide_running", return_value=False)
    def test_cli_dry_run(self, _, __):
        code = updater.dry_run()
        self.assertEqual(code, 0)

    @patch.object(updater, "latest_release", return_value=("2.5.6", "https://edgedl.me.gvt1.com/test.tar.gz"))
    @patch.object(updater, "ide_running", return_value=False)
    def test_dry_run_has_no_filesystem_side_effects(self, _, __):
        candidate = self.base_path / f".{self.install_dir.name}.candidate-999"
        temp_backup = self.base_path / f".{self.install_dir.name}.bak.tmp-999"
        candidate.mkdir()
        temp_backup.mkdir()

        def snapshot():
            return sorted(str(p.relative_to(self.base_path)) for p in self.base_path.rglob("*"))

        before = snapshot()
        code = updater.dry_run()
        self.assertEqual(code, 0)
        after = snapshot()
        self.assertEqual(before, after)

    @patch.object(updater, "latest_release", return_value=("2.5.6", "https://edgedl.me.gvt1.com/test.tar.gz"))
    @patch.object(updater, "release_notes", return_value="Some notes")
    def test_check_has_no_filesystem_side_effects(self, _, __):
        candidate = self.base_path / f".{self.install_dir.name}.candidate-999"
        temp_backup = self.base_path / f".{self.install_dir.name}.bak.tmp-999"
        candidate.mkdir()
        temp_backup.mkdir()

        def snapshot():
            return sorted(str(p.relative_to(self.base_path)) for p in self.base_path.rglob("*"))

        before = snapshot()
        code = updater.check_updates()
        self.assertEqual(code, 0)
        after = snapshot()
        self.assertEqual(before, after)

    @patch.object(updater, "ide_running", return_value=False)
    def test_cli_status(self, _):
        code = updater.show_status()
        self.assertEqual(code, 0)


if __name__ == "__main__":
    unittest.main()
