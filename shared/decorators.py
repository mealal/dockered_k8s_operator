"""
Decorators and utilities for retry logic and polling.

Contains reusable decorators and functions for:
- Retry with exponential backoff
- Polling with configurable backoff strategies
- Timeout handling

Usage:
    from shared.decorators import (
        retry_with_backoff,
        poll_with_backoff,
        ExponentialBackoff,
    )

    # Decorator for automatic retry
    @retry_with_backoff(max_retries=5)
    def api_call():
        return make_request()

    # Polling for conditions
    result = poll_with_backoff(
        check_func=lambda: check_resource_ready(),
        timeout=300,
        backoff=ExponentialBackoff(initial=5, max_interval=30),
    )
"""

import logging
import time
from dataclasses import dataclass, field
from functools import wraps
from typing import Callable, Optional, Tuple, Type, TypeVar, Generic, Any

logger = logging.getLogger(__name__)

T = TypeVar('T')


# =============================================================================
# Backoff Strategies
# =============================================================================

@dataclass
class BackoffStrategy:
    """Base class for backoff strategies."""

    def get_delay(self, attempt: int) -> float:
        """Get delay for the given attempt number (0-indexed)."""
        raise NotImplementedError


@dataclass
class ConstantBackoff(BackoffStrategy):
    """Constant delay between attempts.

    Attributes:
        interval: Fixed delay between attempts in seconds
    """
    interval: float = 10.0

    def get_delay(self, attempt: int) -> float:
        return self.interval


@dataclass
class LinearBackoff(BackoffStrategy):
    """Linearly increasing delay between attempts.

    Attributes:
        initial: Initial delay in seconds
        increment: Amount to increase delay each attempt
        max_interval: Maximum delay in seconds
    """
    initial: float = 5.0
    increment: float = 5.0
    max_interval: float = 60.0

    def get_delay(self, attempt: int) -> float:
        delay = self.initial + (attempt * self.increment)
        return min(delay, self.max_interval)


@dataclass
class ExponentialBackoff(BackoffStrategy):
    """Exponentially increasing delay between attempts.

    Delay doubles each attempt: initial, initial*2, initial*4, ...

    Attributes:
        initial: Initial delay in seconds
        multiplier: Multiplier for each subsequent attempt
        max_interval: Maximum delay in seconds
        jitter: If True, add random jitter to prevent thundering herd
    """
    initial: float = 2.0
    multiplier: float = 2.0
    max_interval: float = 60.0
    jitter: bool = False

    def get_delay(self, attempt: int) -> float:
        import random
        delay = self.initial * (self.multiplier ** attempt)
        delay = min(delay, self.max_interval)
        if self.jitter:
            # Add up to 25% jitter
            delay = delay * (1 + random.uniform(-0.25, 0.25))
        return delay


# =============================================================================
# Polling Results
# =============================================================================

@dataclass
class PollResult(Generic[T]):
    """Result of a polling operation.

    Attributes:
        success: Whether the condition was met
        value: Final value returned by check function
        attempts: Number of attempts made
        elapsed: Total time elapsed in seconds
        last_error: Last error encountered (if any)
    """
    success: bool
    value: Optional[T]
    attempts: int
    elapsed: float
    last_error: Optional[Exception] = None


def retry_with_backoff(
    max_retries: int = 3,
    base_delay: float = 2.0,
    max_delay: float = 30.0,
    exceptions: Tuple[Type[Exception], ...] = (Exception,)
) -> Callable:
    """Decorator for retrying operations with exponential backoff.

    Retries the decorated function on failure with exponentially increasing
    delays between attempts. Useful for network operations and external
    service calls that may fail transiently.

    Args:
        max_retries: Maximum number of retry attempts (default: 3)
        base_delay: Initial delay in seconds (default: 2.0)
        max_delay: Maximum delay in seconds (default: 30.0)
        exceptions: Tuple of exception types to catch and retry

    Returns:
        Decorated function with retry logic

    Example:
        @retry_with_backoff(max_retries=5, exceptions=(ConnectionError,))
        def fetch_data():
            # May fail transiently
            return requests.get(url)
    """
    def decorator(func: Callable) -> Callable:
        @wraps(func)
        def wrapper(*args, **kwargs):
            last_exception = None
            delay = base_delay

            for attempt in range(max_retries):
                try:
                    return func(*args, **kwargs)
                except exceptions as e:
                    last_exception = e
                    if attempt < max_retries - 1:
                        logger.warning(
                            f"Attempt {attempt + 1}/{max_retries} failed: {e}. "
                            f"Retrying in {delay:.1f}s..."
                        )
                        time.sleep(delay)
                        delay = min(delay * 2, max_delay)
                    else:
                        logger.error(f"All {max_retries} attempts failed")

            raise last_exception

        return wrapper
    return decorator


# =============================================================================
# Polling Functions
# =============================================================================

def poll_with_backoff(
    check_func: Callable[[], Tuple[bool, T]],
    timeout: float,
    backoff: Optional[BackoffStrategy] = None,
    on_attempt: Optional[Callable[[int, float, T], None]] = None,
    description: str = "condition",
) -> PollResult[T]:
    """Poll a condition with configurable backoff strategy.

    Repeatedly calls check_func until it returns (True, value) or timeout.
    Uses exponential backoff by default.

    Args:
        check_func: Function that returns (success: bool, value: T)
                    Success=True stops polling, value is passed through
        timeout: Maximum time to wait in seconds
        backoff: Backoff strategy (default: ExponentialBackoff with 5s initial)
        on_attempt: Optional callback(attempt, elapsed, value) after each check
        description: Description of what we're waiting for (for logging)

    Returns:
        PollResult with success status, final value, and statistics

    Example:
        def check_pod_ready():
            status = get_pod_status()
            return (status == "Running", status)

        result = poll_with_backoff(
            check_func=check_pod_ready,
            timeout=300,
            backoff=ExponentialBackoff(initial=5, max_interval=30),
            description="pod to be Ready",
        )

        if result.success:
            print(f"Pod ready after {result.elapsed:.1f}s")
        else:
            print(f"Timed out after {result.attempts} attempts")
    """
    if backoff is None:
        backoff = ExponentialBackoff(initial=5.0, max_interval=30.0)

    start_time = time.time()
    attempt = 0
    last_value = None
    last_error = None

    logger.debug(f"Polling for {description} (timeout: {timeout}s)")

    while True:
        elapsed = time.time() - start_time

        if elapsed >= timeout:
            logger.warning(f"Timed out waiting for {description} after {elapsed:.1f}s")
            return PollResult(
                success=False,
                value=last_value,
                attempts=attempt,
                elapsed=elapsed,
                last_error=last_error,
            )

        try:
            success, value = check_func()
            last_value = value
            last_error = None

            if on_attempt:
                on_attempt(attempt, elapsed, value)

            if success:
                logger.debug(f"{description.capitalize()} met after {attempt + 1} attempts ({elapsed:.1f}s)")
                return PollResult(
                    success=True,
                    value=value,
                    attempts=attempt + 1,
                    elapsed=elapsed,
                )

        except Exception as e:
            last_error = e
            logger.debug(f"Check failed on attempt {attempt + 1}: {e}")

        # Calculate delay for this attempt
        delay = backoff.get_delay(attempt)

        # Don't wait longer than remaining timeout
        remaining = timeout - (time.time() - start_time)
        if remaining <= 0:
            continue  # Will timeout on next iteration

        actual_delay = min(delay, remaining)
        logger.debug(f"Waiting {actual_delay:.1f}s before next check...")
        time.sleep(actual_delay)

        attempt += 1


def wait_for_condition(
    condition: Callable[[], bool],
    timeout: float,
    interval: float = 10.0,
    description: str = "condition",
) -> bool:
    """Simple polling wrapper with constant interval.

    This is a simpler interface for common polling use cases where
    you just need to wait for a boolean condition.

    Args:
        condition: Function that returns True when condition is met
        timeout: Maximum time to wait in seconds
        interval: Time between checks in seconds
        description: Description for logging

    Returns:
        True if condition was met, False if timed out

    Example:
        def is_ready():
            return get_status() == "Ready"

        if wait_for_condition(is_ready, timeout=300, interval=15):
            print("Ready!")
        else:
            print("Timed out!")
    """
    result = poll_with_backoff(
        check_func=lambda: (condition(), None),
        timeout=timeout,
        backoff=ConstantBackoff(interval=interval),
        description=description,
    )
    return result.success


def wait_for_value(
    get_value: Callable[[], T],
    expected: T,
    timeout: float,
    interval: float = 10.0,
    description: str = "value",
) -> bool:
    """Wait for a function to return an expected value.

    Args:
        get_value: Function that returns the current value
        expected: The value we're waiting for
        timeout: Maximum time to wait in seconds
        interval: Time between checks in seconds
        description: Description for logging

    Returns:
        True if expected value was reached, False if timed out

    Example:
        if wait_for_value(get_pod_phase, "Running", timeout=300):
            print("Pod is running!")
    """
    def check():
        current = get_value()
        return (current == expected, current)

    result = poll_with_backoff(
        check_func=check,
        timeout=timeout,
        backoff=ConstantBackoff(interval=interval),
        description=f"{description} to equal '{expected}'",
    )
    return result.success


class ProgressiveBackoff(BackoffStrategy):
    """Backoff that starts slow, then speeds up, then slows down again.

    Useful for waiting on resources that typically either:
    - Succeed quickly (first few checks)
    - Take a while but succeed eventually (middle phase)
    - Are stuck and will timeout (final phase with slower checks)

    Attributes:
        fast_interval: Initial fast checking interval
        fast_checks: Number of fast checks before slowing down
        normal_interval: Regular checking interval
        slow_interval: Slower interval near timeout
        slow_threshold: Fraction of timeout after which to slow down (0.0-1.0)
    """
    fast_interval: float = 2.0
    fast_checks: int = 5
    normal_interval: float = 10.0
    slow_interval: float = 20.0
    slow_threshold: float = 0.7

    def __init__(
        self,
        fast_interval: float = 2.0,
        fast_checks: int = 5,
        normal_interval: float = 10.0,
        slow_interval: float = 20.0,
        slow_threshold: float = 0.7,
        timeout: float = 300.0,
    ):
        self.fast_interval = fast_interval
        self.fast_checks = fast_checks
        self.normal_interval = normal_interval
        self.slow_interval = slow_interval
        self.slow_threshold = slow_threshold
        self.timeout = timeout
        self._start_time = time.time()

    def get_delay(self, attempt: int) -> float:
        # Fast checks at the beginning
        if attempt < self.fast_checks:
            return self.fast_interval

        # Slow down near the end
        elapsed = time.time() - self._start_time
        if elapsed / self.timeout > self.slow_threshold:
            return self.slow_interval

        return self.normal_interval
