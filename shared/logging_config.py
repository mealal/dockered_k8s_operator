"""
Standardized logging configuration for MongoDB Kubernetes deployment scripts.

Provides consistent logging setup across all deployment scripts and modules,
with support for different verbosity levels and output formats.

Usage:
    from shared.logging_config import setup_logging, get_logger

    # In main script entry point
    setup_logging(verbose=args.verbose)

    # In modules
    logger = get_logger(__name__)
    logger.info("Starting deployment...")

Features:
    - Consistent formatting across all modules
    - Verbose mode for debug output
    - File logging support (optional)
    - Colored output support (optional, if colorama available)
    - Structured logging for machine parsing (optional)
"""

import logging
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

# Default logging configuration
DEFAULT_LOG_FORMAT = "%(asctime)s - %(levelname)s - %(message)s"
DEFAULT_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"
VERBOSE_LOG_FORMAT = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"

# Log levels
LOG_LEVELS = {
    "debug": logging.DEBUG,
    "info": logging.INFO,
    "warning": logging.WARNING,
    "error": logging.ERROR,
    "critical": logging.CRITICAL,
}


class ColoredFormatter(logging.Formatter):
    """Custom formatter that adds colors to log levels.

    Only applies colors when output is a TTY (terminal).
    Falls back to standard formatting otherwise.
    """

    # ANSI color codes
    COLORS = {
        logging.DEBUG: "\033[36m",      # Cyan
        logging.INFO: "\033[32m",       # Green
        logging.WARNING: "\033[33m",    # Yellow
        logging.ERROR: "\033[31m",      # Red
        logging.CRITICAL: "\033[35m",   # Magenta
    }
    RESET = "\033[0m"

    def __init__(self, fmt: str, datefmt: str = None, use_colors: bool = True):
        super().__init__(fmt, datefmt)
        self.use_colors = use_colors and sys.stdout.isatty()

    def format(self, record: logging.LogRecord) -> str:
        if self.use_colors:
            color = self.COLORS.get(record.levelno, "")
            # Only colorize the level name
            record.levelname = f"{color}{record.levelname}{self.RESET}"
        return super().format(record)


class CompactFormatter(logging.Formatter):
    """Compact formatter for less verbose output.

    Shows level indicator and message without timestamps.
    Useful for interactive terminal output.
    """

    LEVEL_INDICATORS = {
        logging.DEBUG: "[D]",
        logging.INFO: "[*]",
        logging.WARNING: "[!]",
        logging.ERROR: "[X]",
        logging.CRITICAL: "[!!]",
    }

    def format(self, record: logging.LogRecord) -> str:
        indicator = self.LEVEL_INDICATORS.get(record.levelno, "[?]")
        return f"{indicator} {record.getMessage()}"


def setup_logging(
    verbose: bool = False,
    log_file: Optional[Path] = None,
    log_level: str = "info",
    use_colors: bool = True,
    compact: bool = False,
) -> logging.Logger:
    """Configure logging for the application.

    Sets up the root logger with consistent formatting. Call this once
    at the start of your main script.

    Args:
        verbose: If True, use DEBUG level and verbose format
        log_file: Optional path to log file for persistent logging
        log_level: Base log level (debug, info, warning, error, critical)
        use_colors: If True, colorize terminal output
        compact: If True, use compact format without timestamps

    Returns:
        Configured root logger

    Example:
        # Basic setup
        setup_logging(verbose=args.verbose)

        # With file logging
        setup_logging(verbose=True, log_file=Path("deployment.log"))

        # Compact output for scripts
        setup_logging(compact=True)
    """
    # Determine log level
    if verbose:
        level = logging.DEBUG
        log_format = VERBOSE_LOG_FORMAT
    else:
        level = LOG_LEVELS.get(log_level.lower(), logging.INFO)
        log_format = DEFAULT_LOG_FORMAT

    # Get root logger
    root_logger = logging.getLogger()
    root_logger.setLevel(level)

    # Remove existing handlers
    root_logger.handlers.clear()

    # Create console handler
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(level)

    # Choose formatter
    if compact:
        formatter = CompactFormatter()
    elif use_colors:
        formatter = ColoredFormatter(log_format, DEFAULT_DATE_FORMAT)
    else:
        formatter = logging.Formatter(log_format, DEFAULT_DATE_FORMAT)

    console_handler.setFormatter(formatter)
    root_logger.addHandler(console_handler)

    # Add file handler if specified
    if log_file:
        log_file = Path(log_file)
        log_file.parent.mkdir(parents=True, exist_ok=True)

        file_handler = logging.FileHandler(log_file, encoding="utf-8")
        file_handler.setLevel(logging.DEBUG)  # Always log everything to file
        file_formatter = logging.Formatter(VERBOSE_LOG_FORMAT, DEFAULT_DATE_FORMAT)
        file_handler.setFormatter(file_formatter)
        root_logger.addHandler(file_handler)

        root_logger.debug(f"Logging to file: {log_file}")

    return root_logger


def get_logger(name: str) -> logging.Logger:
    """Get a logger for the specified module.

    This is a convenience wrapper around logging.getLogger() that ensures
    consistent logger naming.

    Args:
        name: Usually __name__ of the calling module

    Returns:
        Logger instance for the module

    Example:
        logger = get_logger(__name__)
        logger.info("Module initialized")
    """
    return logging.getLogger(name)


def set_verbose(verbose: bool = True) -> None:
    """Enable or disable verbose logging.

    Can be called at any time to change log level.

    Args:
        verbose: If True, set DEBUG level; otherwise INFO
    """
    level = logging.DEBUG if verbose else logging.INFO
    logging.getLogger().setLevel(level)
    for handler in logging.getLogger().handlers:
        handler.setLevel(level)


def add_file_handler(
    log_file: Path,
    level: int = logging.DEBUG,
) -> logging.FileHandler:
    """Add a file handler to the root logger.

    Args:
        log_file: Path to log file
        level: Minimum level for file logging

    Returns:
        The created FileHandler
    """
    log_file = Path(log_file)
    log_file.parent.mkdir(parents=True, exist_ok=True)

    handler = logging.FileHandler(log_file, encoding="utf-8")
    handler.setLevel(level)
    handler.setFormatter(logging.Formatter(VERBOSE_LOG_FORMAT, DEFAULT_DATE_FORMAT))

    logging.getLogger().addHandler(handler)
    return handler


def create_session_log_file(
    base_dir: Path,
    prefix: str = "deployment",
) -> Path:
    """Create a unique log file path for this session.

    Args:
        base_dir: Directory to store log files
        prefix: Prefix for log file name

    Returns:
        Path to the new log file

    Example:
        log_file = create_session_log_file(Path("./logs"), "deploy")
        # Returns: ./logs/deploy_20231215_143022.log
    """
    base_dir = Path(base_dir)
    base_dir.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return base_dir / f"{prefix}_{timestamp}.log"


# Context manager for temporary verbose logging
class VerboseLogging:
    """Context manager for temporarily enabling verbose logging.

    Example:
        with VerboseLogging():
            # This block will have DEBUG level logging
            do_detailed_operation()
        # Logging returns to previous level
    """

    def __init__(self):
        self._previous_level = None

    def __enter__(self):
        self._previous_level = logging.getLogger().level
        set_verbose(True)
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        logging.getLogger().setLevel(self._previous_level)
        return False


# Convenience functions for logging with context
def log_step(message: str, step: int = None, total: int = None) -> None:
    """Log a deployment step with optional progress.

    Args:
        message: Step description
        step: Current step number (optional)
        total: Total number of steps (optional)

    Example:
        log_step("Creating namespace", step=1, total=5)
        # Output: [1/5] Creating namespace
    """
    logger = logging.getLogger("deployment")
    if step is not None and total is not None:
        logger.info(f"[{step}/{total}] {message}")
    elif step is not None:
        logger.info(f"[{step}] {message}")
    else:
        logger.info(message)


def log_success(message: str) -> None:
    """Log a success message.

    Args:
        message: Success description
    """
    logger = logging.getLogger("deployment")
    logger.info(f"[OK] {message}")


def log_warning(message: str) -> None:
    """Log a warning message.

    Args:
        message: Warning description
    """
    logger = logging.getLogger("deployment")
    logger.warning(message)


def log_error(message: str, suggestion: str = None) -> None:
    """Log an error message with optional suggestion.

    Args:
        message: Error description
        suggestion: How to resolve the error
    """
    logger = logging.getLogger("deployment")
    logger.error(message)
    if suggestion:
        logger.error(f"  Suggestion: {suggestion}")
