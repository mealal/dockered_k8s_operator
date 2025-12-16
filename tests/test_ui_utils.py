"""Unit tests for UI utilities module."""

import unittest
import sys
import os
import io
import time
from unittest.mock import patch

# Add parent directory to path for imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from shared.ui_utils import (
    mask_password,
    mask_sensitive,
    ProgressBar,
    format_error_with_suggestion,
    format_success_message,
    DeploymentSummary,
    StepTracker,
    format_duration,
    format_bytes,
)


class TestMaskPassword(unittest.TestCase):
    """Test cases for mask_password function."""

    def test_mask_empty_password(self):
        """Test masking empty password."""
        self.assertEqual(mask_password(""), "****")

    def test_mask_short_password(self):
        """Test masking password shorter than visible chars."""
        self.assertEqual(mask_password("abc"), "***")
        self.assertEqual(mask_password("12345678"), "********")

    def test_mask_normal_password(self):
        """Test masking normal length password."""
        result = mask_password("MySecretPassword123")
        self.assertTrue(result.startswith("MySe"))
        self.assertTrue(result.endswith("123"))
        self.assertIn("*", result)

    def test_mask_custom_visible_chars(self):
        """Test masking with custom visible characters."""
        result = mask_password("MySecretPassword", visible_chars=2)
        self.assertTrue(result.startswith("My"))
        self.assertTrue(result.endswith("rd"))


class TestMaskSensitive(unittest.TestCase):
    """Test cases for mask_sensitive function."""

    def test_mask_empty(self):
        """Test masking empty value."""
        self.assertEqual(mask_sensitive(""), "[empty]")

    def test_mask_with_length(self):
        """Test masking with length indication."""
        result = mask_sensitive("secret")
        self.assertEqual(result, "[6 chars hidden]")

    def test_mask_without_length(self):
        """Test masking without length indication."""
        result = mask_sensitive("secret", show_length=False)
        self.assertEqual(result, "[hidden]")


class TestProgressBar(unittest.TestCase):
    """Test cases for ProgressBar class."""

    def test_progress_bar_creation(self):
        """Test creating a progress bar."""
        output = io.StringIO()
        bar = ProgressBar(total=100, message="Test", stream=output)

        self.assertEqual(bar.total, 100)
        self.assertEqual(bar.current, 0)

    def test_progress_bar_update(self):
        """Test updating progress bar."""
        output = io.StringIO()
        bar = ProgressBar(total=100, message="Test", stream=output)

        bar.update(50)
        self.assertEqual(bar.current, 50)

        content = output.getvalue()
        self.assertIn("50.0%", content)

    def test_progress_bar_finish(self):
        """Test finishing progress bar."""
        output = io.StringIO()
        bar = ProgressBar(total=100, message="Test", stream=output)

        bar.finish()
        content = output.getvalue()
        self.assertIn("100.0%", content)


class TestMessageFormatting(unittest.TestCase):
    """Test cases for message formatting functions."""

    def test_format_error_with_suggestion(self):
        """Test error message formatting."""
        result = format_error_with_suggestion(
            error="Connection failed",
            suggestion="Check if server is running",
            details="Connection refused on port 27017"
        )

        self.assertIn("ERROR", result)
        self.assertIn("Connection failed", result)
        self.assertIn("Check if server is running", result)
        self.assertIn("port 27017", result)

    def test_format_error_without_details(self):
        """Test error message without details."""
        result = format_error_with_suggestion(
            error="Something broke",
            suggestion="Try again"
        )

        self.assertIn("Something broke", result)
        self.assertIn("Try again", result)
        self.assertNotIn("Details:", result)

    def test_format_success_message(self):
        """Test success message formatting."""
        result = format_success_message(
            message="Deployment completed",
            details={"Cluster": "mongodb-cluster", "Pods": "3"}
        )

        self.assertIn("SUCCESS", result)
        self.assertIn("Deployment completed", result)
        self.assertIn("Cluster: mongodb-cluster", result)
        self.assertIn("Pods: 3", result)

    def test_format_success_without_details(self):
        """Test success message without details."""
        result = format_success_message(message="Done!")

        self.assertIn("SUCCESS", result)
        self.assertIn("Done!", result)


class TestDeploymentSummary(unittest.TestCase):
    """Test cases for DeploymentSummary class."""

    def test_summary_creation(self):
        """Test creating a deployment summary."""
        summary = DeploymentSummary("Test Deployment")
        self.assertEqual(summary.title, "Test Deployment")
        self.assertEqual(summary.steps, [])
        self.assertEqual(summary.resources, {})

    def test_add_step(self):
        """Test adding steps to summary."""
        summary = DeploymentSummary("Test")
        summary.add_step("Step 1", success=True, duration=5.0)
        summary.add_step("Step 2", success=False, message="Error")

        self.assertEqual(len(summary.steps), 2)
        self.assertTrue(summary.steps[0]["success"])
        self.assertFalse(summary.steps[1]["success"])

    def test_add_resource(self):
        """Test adding resources to summary."""
        summary = DeploymentSummary("Test")
        summary.add_resource("Cluster", "test-cluster")
        summary.add_resource("Namespace", "mongodb-ns")
        summary.add_resource("Cluster", "second-cluster")

        self.assertIn("Cluster", summary.resources)
        self.assertEqual(len(summary.resources["Cluster"]), 2)

    def test_add_connection_info(self):
        """Test adding connection info."""
        summary = DeploymentSummary("Test")
        summary.add_connection_info("MongoDB", "localhost:27017")

        self.assertEqual(
            summary.connection_info["MongoDB"],
            "localhost:27017"
        )

    def test_success_property(self):
        """Test success property checks all steps."""
        summary = DeploymentSummary("Test")
        summary.add_step("Step 1", success=True)
        summary.add_step("Step 2", success=True)
        self.assertTrue(summary.success)

        summary.add_step("Step 3", success=False)
        self.assertFalse(summary.success)

    def test_format_output(self):
        """Test formatting summary output."""
        summary = DeploymentSummary("Test Deployment")
        summary.add_step("Create cluster", success=True, duration=10.5)
        summary.add_resource("Cluster", "test-cluster")
        summary.add_connection_info("MongoDB", "localhost:27017")

        output = summary.format()

        self.assertIn("Test Deployment", output)
        self.assertIn("[SUCCESS]", output)
        self.assertIn("Create cluster", output)
        self.assertIn("test-cluster", output)
        self.assertIn("localhost:27017", output)

    def test_format_with_warnings(self):
        """Test formatting with warnings."""
        summary = DeploymentSummary("Test")
        summary.add_step("Step 1", success=True)
        summary.add_warning("This is a warning")

        output = summary.format()
        self.assertIn("[!]", output)
        self.assertIn("This is a warning", output)


class TestStepTracker(unittest.TestCase):
    """Test cases for StepTracker class."""

    def test_step_tracker_creation(self):
        """Test creating a step tracker."""
        tracker = StepTracker(total_steps=5)
        self.assertEqual(tracker.total_steps, 5)
        self.assertEqual(tracker.current_step, 0)

    def test_step_context_manager(self):
        """Test using step as context manager."""
        tracker = StepTracker(total_steps=3)

        with tracker.step("First step"):
            pass

        self.assertEqual(tracker.current_step, 1)
        self.assertEqual(len(tracker.step_times), 1)

    def test_multiple_steps(self):
        """Test tracking multiple steps."""
        tracker = StepTracker(total_steps=3)

        with tracker.step("Step 1"):
            time.sleep(0.01)
        with tracker.step("Step 2"):
            time.sleep(0.01)

        self.assertEqual(tracker.current_step, 2)
        self.assertTrue(tracker.elapsed_time > 0)


class TestFormatFunctions(unittest.TestCase):
    """Test cases for format helper functions."""

    def test_format_duration_seconds(self):
        """Test formatting duration in seconds."""
        self.assertEqual(format_duration(45), "45s")
        self.assertEqual(format_duration(5.5), "6s")

    def test_format_duration_minutes(self):
        """Test formatting duration in minutes."""
        self.assertEqual(format_duration(90), "1m 30s")
        self.assertEqual(format_duration(125), "2m 5s")

    def test_format_duration_hours(self):
        """Test formatting duration in hours."""
        self.assertEqual(format_duration(3661), "1h 1m")
        self.assertEqual(format_duration(7200), "2h 0m")

    def test_format_bytes_small(self):
        """Test formatting small byte values."""
        self.assertEqual(format_bytes(100), "100.0 B")
        self.assertEqual(format_bytes(1023), "1023.0 B")

    def test_format_bytes_kilobytes(self):
        """Test formatting kilobyte values."""
        self.assertEqual(format_bytes(1024), "1.0 KB")
        self.assertEqual(format_bytes(2048), "2.0 KB")

    def test_format_bytes_megabytes(self):
        """Test formatting megabyte values."""
        self.assertEqual(format_bytes(1048576), "1.0 MB")
        self.assertEqual(format_bytes(5242880), "5.0 MB")

    def test_format_bytes_gigabytes(self):
        """Test formatting gigabyte values."""
        self.assertEqual(format_bytes(1073741824), "1.0 GB")


if __name__ == "__main__":
    unittest.main()
