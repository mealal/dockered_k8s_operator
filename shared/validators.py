"""
Argument validators for CLI argument parsing.

These validators are used with argparse to validate command-line arguments
for both single-cluster and multi-cluster deployment scripts.
"""

import argparse
import re


def positive_int(value: str) -> int:
    """Argparse type validator for positive integers.

    Args:
        value: String value from command line

    Returns:
        Validated positive integer

    Raises:
        argparse.ArgumentTypeError: If value is not a positive integer
    """
    try:
        ivalue = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"Invalid integer value: {value}")
    if ivalue <= 0:
        raise argparse.ArgumentTypeError(f"Must be a positive integer, got {value}")
    return ivalue


def non_negative_int(value: str) -> int:
    """Argparse type validator for non-negative integers.

    Args:
        value: String value from command line

    Returns:
        Validated non-negative integer

    Raises:
        argparse.ArgumentTypeError: If value is negative
    """
    try:
        ivalue = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"Invalid integer value: {value}")
    if ivalue < 0:
        raise argparse.ArgumentTypeError(f"Must be a non-negative integer, got {value}")
    return ivalue


def valid_port(value: str) -> int:
    """Argparse type validator for valid port numbers (1-65535).

    Args:
        value: String value from command line

    Returns:
        Validated port number

    Raises:
        argparse.ArgumentTypeError: If value is not a valid port
    """
    try:
        ivalue = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"Invalid port number: {value}")
    if not 1 <= ivalue <= 65535:
        raise argparse.ArgumentTypeError(f"Port must be between 1 and 65535, got {value}")
    return ivalue


def valid_namespace(value: str) -> str:
    """Argparse type validator for Kubernetes namespace names.

    Validates that the namespace follows Kubernetes naming conventions:
    - Lowercase alphanumeric characters or hyphens
    - Must start and end with alphanumeric
    - Maximum 63 characters

    Args:
        value: String value from command line

    Returns:
        Validated namespace name

    Raises:
        argparse.ArgumentTypeError: If value is not a valid namespace name
    """
    if not value:
        raise argparse.ArgumentTypeError("Namespace cannot be empty")
    if not re.match(r'^[a-z0-9][a-z0-9-]{0,61}[a-z0-9]?$', value):
        raise argparse.ArgumentTypeError(
            f"Invalid namespace '{value}': must be lowercase alphanumeric with hyphens, max 63 chars"
        )
    return value


def valid_timeout(min_val: int = 10, max_val: int = 3600):
    """Create an argparse type validator for timeout values with bounds.

    Factory function that creates a validator for timeout values.
    Default bounds are 10 seconds to 1 hour.

    Args:
        min_val: Minimum allowed timeout in seconds (default: 10)
        max_val: Maximum allowed timeout in seconds (default: 3600)

    Returns:
        A validator function for use with argparse type= parameter

    Example:
        parser.add_argument('--timeout', type=valid_timeout(30, 1800))
    """
    def validator(value: str) -> int:
        try:
            ivalue = int(value)
        except ValueError:
            raise argparse.ArgumentTypeError(f"Invalid timeout value: {value}")
        if ivalue < min_val:
            raise argparse.ArgumentTypeError(
                f"Timeout must be at least {min_val} seconds, got {value}"
            )
        if ivalue > max_val:
            raise argparse.ArgumentTypeError(
                f"Timeout must not exceed {max_val} seconds, got {value}"
            )
        return ivalue
    return validator
