"""Unit tests for Result type."""

import unittest
import sys
import os

# Add parent directory to path for imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from shared.result import Result, Ok, Err, CommandResult, OperationResult, operation_ok, operation_err


class TestResult(unittest.TestCase):
    """Test cases for Result type."""

    def test_ok_creation(self):
        """Test creating an Ok result."""
        result = Ok(42)
        self.assertTrue(result.is_ok())
        self.assertFalse(result.is_err())
        self.assertEqual(result.value, 42)

    def test_err_creation(self):
        """Test creating an Err result."""
        result = Err("something went wrong")
        self.assertFalse(result.is_ok())
        self.assertTrue(result.is_err())
        self.assertEqual(result.error, "something went wrong")

    def test_err_from_exception(self):
        """Test creating Err from exception."""
        result = Err(ValueError("invalid value"))
        self.assertTrue(result.is_err())
        self.assertEqual(result.error, "invalid value")

    def test_value_raises_on_err(self):
        """Test that accessing value on Err raises."""
        result = Err("error")
        with self.assertRaises(ValueError):
            _ = result.value

    def test_error_raises_on_ok(self):
        """Test that accessing error on Ok raises."""
        result = Ok(42)
        with self.assertRaises(ValueError):
            _ = result.error

    def test_value_or(self):
        """Test value_or default handling."""
        ok_result = Ok(42)
        err_result = Err("error")

        self.assertEqual(ok_result.value_or(0), 42)
        self.assertEqual(err_result.value_or(0), 0)

    def test_map_on_ok(self):
        """Test map transforms Ok value."""
        result = Ok(5).map(lambda x: x * 2)
        self.assertTrue(result.is_ok())
        self.assertEqual(result.value, 10)

    def test_map_on_err(self):
        """Test map propagates Err."""
        result = Err("error").map(lambda x: x * 2)
        self.assertTrue(result.is_err())
        self.assertEqual(result.error, "error")

    def test_map_exception_becomes_err(self):
        """Test that map catches exceptions."""
        result = Ok(5).map(lambda x: x / 0)
        self.assertTrue(result.is_err())
        self.assertIn("division", result.error.lower())

    def test_map_err(self):
        """Test map_err transforms error."""
        result = Err("error").map_err(lambda e: f"wrapped: {e}")
        self.assertTrue(result.is_err())
        self.assertEqual(result.error, "wrapped: error")

    def test_map_err_on_ok(self):
        """Test map_err passes through Ok."""
        result = Ok(42).map_err(lambda e: f"wrapped: {e}")
        self.assertTrue(result.is_ok())
        self.assertEqual(result.value, 42)

    def test_and_then(self):
        """Test and_then chains Result-returning functions."""
        def double_if_positive(x):
            if x > 0:
                return Ok(x * 2)
            return Err("must be positive")

        self.assertEqual(Ok(5).and_then(double_if_positive).value, 10)
        self.assertTrue(Ok(-5).and_then(double_if_positive).is_err())
        self.assertTrue(Err("original").and_then(double_if_positive).is_err())

    def test_unwrap(self):
        """Test unwrap returns value or raises."""
        self.assertEqual(Ok(42).unwrap(), 42)
        with self.assertRaises(RuntimeError):
            Err("error").unwrap()

    def test_unwrap_or_else(self):
        """Test unwrap_or_else with fallback function."""
        self.assertEqual(Ok(42).unwrap_or_else(lambda e: 0), 42)
        self.assertEqual(Err("error").unwrap_or_else(lambda e: len(e)), 5)

    def test_expect(self):
        """Test expect with custom message."""
        self.assertEqual(Ok(42).expect("should work"), 42)
        with self.assertRaises(RuntimeError) as ctx:
            Err("error").expect("custom message")
        self.assertIn("custom message", str(ctx.exception))

    def test_bool_conversion(self):
        """Test Result in boolean context."""
        self.assertTrue(bool(Ok(42)))
        self.assertTrue(bool(Ok(0)))  # Even falsy values are Ok
        self.assertFalse(bool(Err("error")))

    def test_repr(self):
        """Test string representation."""
        self.assertEqual(repr(Ok(42)), "Ok(42)")
        self.assertEqual(repr(Err("error")), "Err('error')")


class TestCommandResult(unittest.TestCase):
    """Test cases for CommandResult."""

    def test_success_result(self):
        """Test successful command result."""
        result = CommandResult(
            success=True,
            stdout="output",
            stderr="",
            return_code=0,
            command="echo hello"
        )
        self.assertTrue(result.is_ok())
        self.assertFalse(result.is_err())
        self.assertTrue(bool(result))

    def test_failed_result(self):
        """Test failed command result."""
        result = CommandResult(
            success=False,
            stdout="",
            stderr="command not found",
            return_code=127,
            command="invalid_cmd",
            error="Command not found"
        )
        self.assertFalse(result.is_ok())
        self.assertTrue(result.is_err())
        self.assertFalse(bool(result))

    def test_to_result_success(self):
        """Test converting success to Result."""
        cmd_result = CommandResult(success=True, stdout="output")
        result = cmd_result.to_result()
        self.assertTrue(result.is_ok())
        self.assertEqual(result.value, "output")

    def test_to_result_failure(self):
        """Test converting failure to Result."""
        cmd_result = CommandResult(
            success=False,
            stderr="error output",
            error="Command failed"
        )
        result = cmd_result.to_result()
        self.assertTrue(result.is_err())
        self.assertEqual(result.error, "Command failed")


class TestOperationResult(unittest.TestCase):
    """Test cases for OperationResult."""

    def test_operation_ok(self):
        """Test operation_ok helper."""
        result = operation_ok({"key": "value"}, operation="test_op", duration=1.5)
        self.assertTrue(result.is_ok())
        self.assertEqual(result.value, {"key": "value"})
        self.assertEqual(result.operation, "test_op")
        self.assertEqual(result.duration, 1.5)

    def test_operation_err(self):
        """Test operation_err helper."""
        result = operation_err("failed", operation="test_op", details="more info")
        self.assertTrue(result.is_err())
        self.assertEqual(result.error, "failed")
        self.assertEqual(result.operation, "test_op")
        self.assertEqual(result.details, "more info")

    def test_to_result(self):
        """Test converting to basic Result."""
        ok_result = operation_ok(42, "test").to_result()
        self.assertTrue(ok_result.is_ok())
        self.assertEqual(ok_result.value, 42)

        err_result = operation_err("failed", "test").to_result()
        self.assertTrue(err_result.is_err())

    def test_with_details(self):
        """Test adding details to result."""
        result = operation_ok(42, "test")
        detailed = result.with_details("additional info")
        self.assertEqual(detailed.details, "additional info")
        self.assertEqual(detailed.value, 42)


if __name__ == "__main__":
    unittest.main()
