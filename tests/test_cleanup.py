"""Unit tests for cleanup module."""

import unittest
import sys
import os
import tempfile
import shutil
from pathlib import Path

# Add parent directory to path for imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from shared.cleanup import CleanupManager


class TestCleanupManager(unittest.TestCase):
    """Test cases for CleanupManager class."""

    def setUp(self):
        """Create temporary directory structure for testing."""
        self.temp_dir = Path(tempfile.mkdtemp())
        self.manager = CleanupManager(self.temp_dir, dry_run=False)

    def tearDown(self):
        """Clean up temporary directory."""
        if self.temp_dir.exists():
            shutil.rmtree(self.temp_dir)

    def test_cleanup_manager_creation(self):
        """Test creating a cleanup manager."""
        manager = CleanupManager(self.temp_dir)
        self.assertEqual(manager.script_dir, self.temp_dir)
        self.assertFalse(manager.dry_run)

    def test_cleanup_manager_dry_run(self):
        """Test dry run mode."""
        manager = CleanupManager(self.temp_dir, dry_run=True)
        self.assertTrue(manager.dry_run)

    def test_delete_file(self):
        """Test deleting a single file."""
        test_file = self.temp_dir / "test.txt"
        test_file.write_text("test content")

        self.assertTrue(test_file.exists())
        result = self.manager._delete_file(test_file)

        self.assertTrue(result)
        self.assertFalse(test_file.exists())

    def test_delete_nonexistent_file(self):
        """Test deleting a file that doesn't exist."""
        test_file = self.temp_dir / "nonexistent.txt"
        result = self.manager._delete_file(test_file)

        self.assertFalse(result)

    def test_delete_file_dry_run(self):
        """Test deleting file in dry run mode."""
        manager = CleanupManager(self.temp_dir, dry_run=True)
        test_file = self.temp_dir / "test.txt"
        test_file.write_text("test content")

        result = manager._delete_file(test_file)

        self.assertTrue(result)  # Reports as would delete
        self.assertTrue(test_file.exists())  # But doesn't actually delete

    def test_delete_directory(self):
        """Test deleting a directory."""
        test_dir = self.temp_dir / "subdir"
        test_dir.mkdir()
        (test_dir / "file.txt").write_text("content")

        result = self.manager._delete_directory(test_dir)

        self.assertTrue(result)
        self.assertFalse(test_dir.exists())

    def test_delete_nonexistent_directory(self):
        """Test deleting a directory that doesn't exist."""
        test_dir = self.temp_dir / "nonexistent"
        result = self.manager._delete_directory(test_dir)

        self.assertFalse(result)

    def test_delete_directory_dry_run(self):
        """Test deleting directory in dry run mode."""
        manager = CleanupManager(self.temp_dir, dry_run=True)
        test_dir = self.temp_dir / "subdir"
        test_dir.mkdir()

        result = manager._delete_directory(test_dir)

        self.assertTrue(result)  # Reports as would delete
        self.assertTrue(test_dir.exists())  # But doesn't actually delete

    def test_delete_directory_contents(self):
        """Test deleting directory contents without deleting the directory."""
        test_dir = self.temp_dir / "subdir"
        test_dir.mkdir()
        (test_dir / "file1.txt").write_text("content1")
        (test_dir / "file2.txt").write_text("content2")
        nested_dir = test_dir / "nested"
        nested_dir.mkdir()
        (nested_dir / "file3.txt").write_text("content3")

        deleted = self.manager._delete_directory_contents(test_dir)

        self.assertEqual(deleted, 3)  # 2 files + 1 nested dir
        self.assertTrue(test_dir.exists())  # Parent still exists
        self.assertEqual(list(test_dir.iterdir()), [])  # But empty

    def test_delete_directory_contents_empty(self):
        """Test deleting contents of empty directory."""
        test_dir = self.temp_dir / "empty"
        test_dir.mkdir()

        deleted = self.manager._delete_directory_contents(test_dir)

        self.assertEqual(deleted, 0)

    def test_delete_directory_contents_nonexistent(self):
        """Test deleting contents of nonexistent directory."""
        test_dir = self.temp_dir / "nonexistent"

        deleted = self.manager._delete_directory_contents(test_dir)

        self.assertEqual(deleted, 0)

    def test_add_cleanup_hook(self):
        """Test adding cleanup hooks."""
        cleanup_called = [False]

        def custom_cleanup():
            cleanup_called[0] = True

        self.manager.add_cleanup_hook(custom_cleanup)

        self.assertEqual(len(self.manager._cleanup_hooks), 1)

    def test_script_dir_resolution(self):
        """Test that script_dir is resolved to absolute path."""
        manager = CleanupManager(Path("."))
        self.assertTrue(manager.script_dir.is_absolute())


class TestCleanupManagerIntegration(unittest.TestCase):
    """Integration tests for CleanupManager with real file structures."""

    def setUp(self):
        """Create temporary directory with realistic structure."""
        self.temp_dir = Path(tempfile.mkdtemp())

        # Create k8s directory with YAML files
        self.k8s_dir = self.temp_dir / "k8s"
        self.k8s_dir.mkdir()
        (self.k8s_dir / "mongodb-rs.yaml").write_text("apiVersion: v1")
        (self.k8s_dir / "secrets.yaml").write_text("apiVersion: v1")

        # Create certs directory
        self.certs_dir = self.temp_dir / "certs"
        self.certs_dir.mkdir()
        mongodb_certs = self.certs_dir / "mongodb"
        mongodb_certs.mkdir()
        (mongodb_certs / "server.pem").write_text("cert content")
        (mongodb_certs / "server.key").write_text("key content")

        # Create kubeconfig
        self.kube_dir = self.temp_dir / ".kube"
        self.kube_dir.mkdir()
        (self.kube_dir / "config").write_text("kubeconfig content")

        self.manager = CleanupManager(self.temp_dir)

    def tearDown(self):
        """Clean up temporary directory."""
        if self.temp_dir.exists():
            shutil.rmtree(self.temp_dir)

    def test_file_structure_created(self):
        """Verify test file structure was created correctly."""
        self.assertTrue(self.k8s_dir.exists())
        self.assertTrue(self.certs_dir.exists())
        self.assertTrue(self.kube_dir.exists())
        self.assertEqual(len(list(self.k8s_dir.glob("*.yaml"))), 2)

    def test_cleanup_specific_files(self):
        """Test cleaning up specific files."""
        yaml_files = list(self.k8s_dir.glob("*.yaml"))
        initial_count = len(yaml_files)

        for f in yaml_files:
            self.manager._delete_file(f)

        remaining = list(self.k8s_dir.glob("*.yaml"))
        self.assertEqual(len(remaining), 0)
        self.assertTrue(self.k8s_dir.exists())  # Directory still exists

    def test_cleanup_subdirectory(self):
        """Test cleaning up subdirectory with contents."""
        mongodb_certs = self.certs_dir / "mongodb"
        self.assertTrue(mongodb_certs.exists())
        self.assertEqual(len(list(mongodb_certs.iterdir())), 2)

        self.manager._delete_directory(mongodb_certs)

        self.assertFalse(mongodb_certs.exists())
        self.assertTrue(self.certs_dir.exists())  # Parent still exists


if __name__ == "__main__":
    unittest.main()
