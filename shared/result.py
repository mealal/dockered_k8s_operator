"""
Result type for standardized error handling.

Provides a consistent way to return success/failure from functions
without using exceptions for expected error conditions.

Usage:
    from shared.result import Result, Ok, Err

    def divide(a: int, b: int) -> Result[float]:
        if b == 0:
            return Err("Division by zero")
        return Ok(a / b)

    result = divide(10, 2)
    if result.is_ok():
        print(f"Result: {result.value}")
    else:
        print(f"Error: {result.error}")

    # Or use pattern matching style
    match result:
        case Ok(value):
            print(f"Got: {value}")
        case Err(error):
            print(f"Failed: {error}")
"""

from dataclasses import dataclass
from typing import TypeVar, Generic, Optional, Callable, Union

T = TypeVar('T')
U = TypeVar('U')
E = TypeVar('E')


@dataclass(frozen=True)
class Result(Generic[T]):
    """A result type that can be either Ok(value) or Err(error).

    This provides a functional approach to error handling, making it
    explicit when a function can fail and forcing callers to handle
    both success and failure cases.

    Attributes:
        _value: The success value (if Ok)
        _error: The error message or exception (if Err)
        _is_ok: Whether this is a success result
    """
    _value: Optional[T]
    _error: Optional[str]
    _is_ok: bool

    def is_ok(self) -> bool:
        """Check if this is a success result."""
        return self._is_ok

    def is_err(self) -> bool:
        """Check if this is an error result."""
        return not self._is_ok

    @property
    def value(self) -> T:
        """Get the success value.

        Raises:
            ValueError: If this is an error result
        """
        if not self._is_ok:
            raise ValueError(f"Cannot get value from error result: {self._error}")
        return self._value  # type: ignore

    @property
    def error(self) -> str:
        """Get the error message.

        Raises:
            ValueError: If this is a success result
        """
        if self._is_ok:
            raise ValueError("Cannot get error from success result")
        return self._error  # type: ignore

    def value_or(self, default: T) -> T:
        """Get the value or a default if this is an error."""
        return self._value if self._is_ok else default  # type: ignore

    def map(self, func: Callable[[T], U]) -> 'Result[U]':
        """Apply a function to the value if Ok, otherwise propagate error."""
        if self._is_ok:
            try:
                return Ok(func(self._value))  # type: ignore
            except Exception as e:
                return Err(str(e))
        return Err(self._error)  # type: ignore

    def map_err(self, func: Callable[[str], str]) -> 'Result[T]':
        """Apply a function to the error if Err, otherwise propagate value."""
        if self._is_ok:
            return self
        return Err(func(self._error))  # type: ignore

    def and_then(self, func: Callable[[T], 'Result[U]']) -> 'Result[U]':
        """Chain another Result-returning function if Ok."""
        if self._is_ok:
            return func(self._value)  # type: ignore
        return Err(self._error)  # type: ignore

    def unwrap(self) -> T:
        """Get the value or raise an exception.

        Raises:
            RuntimeError: If this is an error result
        """
        if not self._is_ok:
            raise RuntimeError(f"Unwrap called on error: {self._error}")
        return self._value  # type: ignore

    def unwrap_or_else(self, func: Callable[[str], T]) -> T:
        """Get the value or call a function with the error to get a default."""
        if self._is_ok:
            return self._value  # type: ignore
        return func(self._error)  # type: ignore

    def expect(self, message: str) -> T:
        """Get the value or raise with a custom message.

        Args:
            message: Custom error message prefix

        Raises:
            RuntimeError: If this is an error result
        """
        if not self._is_ok:
            raise RuntimeError(f"{message}: {self._error}")
        return self._value  # type: ignore

    def __bool__(self) -> bool:
        """Allow using Result in boolean context (True if Ok)."""
        return self._is_ok

    def __repr__(self) -> str:
        if self._is_ok:
            return f"Ok({self._value!r})"
        return f"Err({self._error!r})"


def Ok(value: T) -> Result[T]:
    """Create a success result.

    Args:
        value: The success value

    Returns:
        A Result containing the value
    """
    return Result(_value=value, _error=None, _is_ok=True)


def Err(error: Union[str, Exception]) -> Result[T]:
    """Create an error result.

    Args:
        error: The error message or exception

    Returns:
        A Result containing the error
    """
    error_str = str(error) if isinstance(error, Exception) else error
    return Result(_value=None, _error=error_str, _is_ok=False)


@dataclass(frozen=True)
class CommandResult:
    """Result of running a shell command.

    Provides structured access to command execution results including
    stdout, stderr, return code, and success/failure status.

    Attributes:
        success: Whether the command succeeded (return code 0)
        stdout: Standard output from the command
        stderr: Standard error from the command
        return_code: The command's exit code
        command: The command that was executed
        error: Error message if the command failed
    """
    success: bool
    stdout: str = ""
    stderr: str = ""
    return_code: int = 0
    command: str = ""
    error: Optional[str] = None

    def is_ok(self) -> bool:
        """Check if command succeeded."""
        return self.success

    def is_err(self) -> bool:
        """Check if command failed."""
        return not self.success

    def to_result(self) -> Result[str]:
        """Convert to a Result type.

        Returns:
            Ok(stdout) if successful, Err(error) if failed
        """
        if self.success:
            return Ok(self.stdout)
        return Err(self.error or self.stderr or f"Command failed with code {self.return_code}")

    def __bool__(self) -> bool:
        """Allow using in boolean context."""
        return self.success

    def __repr__(self) -> str:
        if self.success:
            return f"CommandResult(success=True, stdout={self.stdout[:50]!r}...)"
        return f"CommandResult(success=False, error={self.error!r})"


@dataclass(frozen=True)
class OperationResult(Generic[T]):
    """Result of a deployment operation with detailed context.

    Extends Result with operation-specific metadata useful for
    deployment scripts.

    Attributes:
        success: Whether the operation succeeded
        value: The result value (if successful)
        error: Error message (if failed)
        operation: Name of the operation
        duration: Time taken in seconds (optional)
        details: Additional context or diagnostics
    """
    success: bool
    value: Optional[T] = None
    error: Optional[str] = None
    operation: str = ""
    duration: Optional[float] = None
    details: Optional[str] = None

    def is_ok(self) -> bool:
        """Check if operation succeeded."""
        return self.success

    def is_err(self) -> bool:
        """Check if operation failed."""
        return not self.success

    def to_result(self) -> Result[T]:
        """Convert to a basic Result type."""
        if self.success:
            return Ok(self.value)  # type: ignore
        return Err(self.error or "Operation failed")

    def with_details(self, details: str) -> 'OperationResult[T]':
        """Create a copy with additional details."""
        return OperationResult(
            success=self.success,
            value=self.value,
            error=self.error,
            operation=self.operation,
            duration=self.duration,
            details=details
        )

    def __bool__(self) -> bool:
        """Allow using in boolean context."""
        return self.success

    def __repr__(self) -> str:
        if self.success:
            return f"OperationResult({self.operation}: success)"
        return f"OperationResult({self.operation}: {self.error})"


def operation_ok(value: T, operation: str = "", duration: float = None) -> OperationResult[T]:
    """Create a successful operation result."""
    return OperationResult(
        success=True,
        value=value,
        operation=operation,
        duration=duration
    )


def operation_err(error: str, operation: str = "", details: str = None) -> OperationResult[T]:
    """Create a failed operation result."""
    return OperationResult(
        success=False,
        error=error,
        operation=operation,
        details=details
    )
