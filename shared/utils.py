"""
Utility functions for MongoDB Kubernetes deployment scripts.

Contains common utilities used by both single-cluster and multi-cluster
deployment scripts including command execution, path conversion, and
password generation.
"""

import logging
import os
import secrets
import string
import subprocess
import sys
from pathlib import Path
from typing import List, Optional, Tuple

import yaml

logger = logging.getLogger(__name__)


def run_command(
    cmd: List[str],
    check: bool = True,
    capture: bool = True,
    timeout: Optional[int] = None,
    input_data: Optional[str] = None
) -> subprocess.CompletedProcess:
    """Run a shell command with consistent error handling.

    Args:
        cmd: Command and arguments as a list
        check: If True, raise exception on non-zero exit
        capture: If True, capture stdout/stderr
        timeout: Optional timeout in seconds
        input_data: Optional input to send to stdin

    Returns:
        CompletedProcess instance with return code and output

    Raises:
        subprocess.CalledProcessError: If check=True and command fails
        subprocess.TimeoutExpired: If command exceeds timeout
    """
    logger.debug(f"Running command: {' '.join(cmd)}")
    try:
        # Use UTF-8 encoding to avoid Windows cp1252 codec issues
        result = subprocess.run(
            cmd,
            check=check,
            capture_output=capture,
            text=True,
            encoding='utf-8',
            errors='replace',
            timeout=timeout,
            input=input_data
        )
        return result
    except subprocess.CalledProcessError as e:
        logger.error(f"Command failed: {e.stderr if e.stderr else e}")
        raise
    except subprocess.TimeoutExpired as e:
        logger.error(f"Command timed out after {timeout}s: {' '.join(cmd)}")
        raise


def convert_path_for_docker(path: Path) -> str:
    """Convert Windows path to Docker-compatible path format.

    Docker on Windows requires paths in /c/Users/... format rather than
    C:\\Users\\... format when mounting volumes.

    Args:
        path: Path object to convert

    Returns:
        Docker-compatible path string

    Examples:
        >>> convert_path_for_docker(Path("C:\\Users\\foo\\bar"))
        '/c/Users/foo/bar'
        >>> convert_path_for_docker(Path("/home/user/bar"))
        '/home/user/bar'
    """
    path_str = str(path.resolve())
    # Convert Windows paths like C:\foo to /c/foo for Docker
    if sys.platform == "win32" and len(path_str) >= 2 and path_str[1] == ':':
        drive = path_str[0].lower()
        return f"/{drive}{path_str[2:].replace(os.sep, '/')}"
    return path_str


def check_docker() -> bool:
    """Check if Docker daemon is running and accessible.

    Attempts to run 'docker info' to verify Docker connectivity.

    Returns:
        True if Docker is available, False otherwise
    """
    try:
        result = run_command(["docker", "info"], check=False, timeout=30)
        if result.returncode != 0:
            logger.error("Docker is not running or not accessible")
            logger.error("Please start Docker Desktop and try again")
            return False
        return True
    except FileNotFoundError:
        logger.error("Docker command not found. Please install Docker.")
        return False
    except subprocess.TimeoutExpired:
        logger.error("Docker command timed out. Docker may be starting up.")
        return False


def find_openssl() -> str:
    """Find OpenSSL executable, checking common Windows paths.

    On Windows, OpenSSL is often installed via Git for Windows, Chocolatey,
    or standalone installers. This function checks common locations before
    falling back to PATH lookup.

    Returns:
        Path to openssl executable

    Raises:
        FileNotFoundError: If OpenSSL cannot be found
    """
    openssl_cmd = "openssl"

    # On Windows, check common installation paths
    if sys.platform == "win32":
        common_paths = [
            # Git for Windows (most common)
            r"C:\Program Files\Git\mingw64\bin\openssl.exe",
            r"C:\Program Files\Git\usr\bin\openssl.exe",
            r"C:\Program Files (x86)\Git\mingw64\bin\openssl.exe",
            # Standalone OpenSSL installations
            r"C:\OpenSSL-Win64\bin\openssl.exe",
            r"C:\OpenSSL-Win32\bin\openssl.exe",
            r"C:\Program Files\OpenSSL-Win64\bin\openssl.exe",
            # Chocolatey
            r"C:\ProgramData\chocolatey\bin\openssl.exe",
        ]

        for path in common_paths:
            if os.path.isfile(path):
                return path

        # Try to find via where command
        try:
            result = subprocess.run(
                ["where", "openssl"],
                capture_output=True, text=True, timeout=10
            )
            if result.returncode == 0 and result.stdout.strip():
                return result.stdout.strip().split('\n')[0]
        except Exception:
            pass

    # On non-Windows or if all else fails, verify it's in PATH
    try:
        subprocess.run(
            [openssl_cmd, "version"],
            capture_output=True, check=True, timeout=10
        )
        return openssl_cmd
    except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired):
        raise FileNotFoundError(
            "OpenSSL not found. Please install OpenSSL:\n"
            "  - Windows: Install Git for Windows (includes OpenSSL) or download from https://slproweb.com/products/Win32OpenSSL.html\n"
            "  - Linux: sudo apt-get install openssl\n"
            "  - macOS: brew install openssl"
        )


def run_openssl(args: list, description: str) -> bool:
    """Run an OpenSSL command with proper error logging.

    Uses find_openssl() to locate the OpenSSL executable, which handles
    Windows-specific paths (Git for Windows, Chocolatey, etc.).

    Args:
        args: Command arguments (without 'openssl' prefix)
        description: Human-readable description of the operation

    Returns:
        True if successful, False otherwise
    """
    try:
        openssl_path = find_openssl()
        cmd = [openssl_path] + args
        subprocess.run(
            cmd, check=True, capture_output=True, text=True,
            encoding='utf-8', errors='replace'
        )
        return True
    except subprocess.CalledProcessError as e:
        logger.error(f"OpenSSL {description} failed")
        if e.stderr:
            logger.error(f"OpenSSL stderr: {e.stderr.strip()}")
        if e.stdout:
            logger.debug(f"OpenSSL stdout: {e.stdout.strip()}")
        return False
    except FileNotFoundError as e:
        logger.error(str(e))
        return False


def generate_secure_password(length: int = 16) -> str:
    """Generate a cryptographically secure password.

    Creates a password using a mix of uppercase, lowercase, digits,
    and special characters suitable for MongoDB authentication.

    Args:
        length: Desired password length (default: 16)

    Returns:
        Randomly generated password string

    Raises:
        ValueError: If length is less than 8
    """
    if length < 8:
        raise ValueError("Password length must be at least 8 characters")

    alphabet = string.ascii_letters + string.digits + "!@#$%&*"
    return ''.join(secrets.choice(alphabet) for _ in range(length))


def setup_logging(verbose: bool = False, log_file: Optional[str] = None) -> None:
    """Configure logging with consistent format across scripts.

    Args:
        verbose: If True, set level to DEBUG; otherwise INFO
        log_file: Optional file path to write logs to
    """
    level = logging.DEBUG if verbose else logging.INFO
    handlers: List[logging.Handler] = [logging.StreamHandler()]

    if log_file:
        handlers.append(logging.FileHandler(log_file))

    logging.basicConfig(
        level=level,
        format='%(asctime)s - %(levelname)s - %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S',
        handlers=handlers
    )


def validate_yaml(content: str) -> Tuple[bool, Optional[str]]:
    """Validate YAML content before applying to Kubernetes.

    Parses YAML to detect syntax errors and performs basic validation
    of Kubernetes resource structure.

    Args:
        content: YAML string to validate

    Returns:
        Tuple of (is_valid, error_message). error_message is None if valid.

    Examples:
        >>> validate_yaml("apiVersion: v1\\nkind: ConfigMap")
        (True, None)
        >>> validate_yaml("invalid: yaml: content:")
        (False, "YAML parse error: ...")
    """
    try:
        # Parse all YAML documents (handles multi-document YAML with ---)
        docs = list(yaml.safe_load_all(content))

        if not docs:
            return False, "Empty YAML content"

        # Validate each document has required Kubernetes fields
        for i, doc in enumerate(docs):
            if doc is None:
                # Empty document in multi-doc YAML (trailing ---), skip it
                continue

            if not isinstance(doc, dict):
                return False, f"Document {i+1}: Expected a mapping, got {type(doc).__name__}"

            # Check for required Kubernetes fields
            if 'apiVersion' not in doc:
                return False, f"Document {i+1}: Missing required field 'apiVersion'"
            if 'kind' not in doc:
                return False, f"Document {i+1}: Missing required field 'kind'"

        return True, None

    except yaml.YAMLError as e:
        return False, f"YAML parse error: {e}"
