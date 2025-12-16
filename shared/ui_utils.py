"""
UI utilities for MongoDB Kubernetes deployment scripts.

Provides progress indicators, password masking, and improved
user feedback for long-running operations.
"""

import logging
import sys
import threading
import time
from typing import Optional

from .constants import PROGRESS_SPINNER, PROGRESS_BAR_WIDTH

logger = logging.getLogger(__name__)


# =============================================================================
# Password Masking
# =============================================================================

def mask_password(password: str, visible_chars: int = 4) -> str:
    """Mask a password for safe display.

    Shows first and last few characters with asterisks in between.

    Args:
        password: Password to mask
        visible_chars: Number of characters to show at start and end

    Returns:
        Masked password string
    """
    if not password:
        return "****"

    if len(password) <= visible_chars * 2:
        return "*" * len(password)

    return f"{password[:visible_chars]}{'*' * (len(password) - visible_chars * 2)}{password[-visible_chars:]}"


def mask_sensitive(value: str, show_length: bool = True) -> str:
    """Mask a sensitive value completely.

    Args:
        value: Value to mask
        show_length: Whether to indicate the original length

    Returns:
        Masked string
    """
    if not value:
        return "[empty]"

    if show_length:
        return f"[{len(value)} chars hidden]"
    return "[hidden]"


# =============================================================================
# Progress Indicators
# =============================================================================

class ProgressSpinner:
    """Animated spinner for long-running operations.

    Displays a spinning animation in the console while a task is running.
    Thread-safe and can be used as a context manager.

    Example:
        with ProgressSpinner("Deploying"):
            deploy_something()

        # Or manually:
        spinner = ProgressSpinner("Loading")
        spinner.start()
        do_work()
        spinner.stop()
    """

    def __init__(self, message: str = "Processing", stream=sys.stdout):
        """Initialize the spinner.

        Args:
            message: Message to display alongside spinner
            stream: Output stream (default: stdout)
        """
        self.message = message
        self.stream = stream
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._spinner_chars = PROGRESS_SPINNER

    def _spin(self):
        """Spinner animation loop (runs in separate thread)."""
        idx = 0
        while not self._stop_event.is_set():
            char = self._spinner_chars[idx % len(self._spinner_chars)]
            self.stream.write(f"\r{self.message}... {char}")
            self.stream.flush()
            idx += 1
            time.sleep(0.1)

        # Clear spinner line
        self.stream.write(f"\r{self.message}... done\n")
        self.stream.flush()

    def start(self):
        """Start the spinner animation."""
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._spin, daemon=True)
        self._thread.start()

    def stop(self):
        """Stop the spinner animation."""
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=1)
            self._thread = None

    def __enter__(self):
        """Context manager entry."""
        self.start()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        """Context manager exit."""
        self.stop()
        return False


class ProgressBar:
    """Text-based progress bar for operations with known progress.

    Example:
        bar = ProgressBar(total=100, message="Downloading")
        for i in range(100):
            do_something()
            bar.update(i + 1)
        bar.finish()
    """

    def __init__(
        self,
        total: int,
        message: str = "Progress",
        width: int = PROGRESS_BAR_WIDTH,
        stream=sys.stdout
    ):
        """Initialize the progress bar.

        Args:
            total: Total number of steps
            message: Message to display
            width: Width of the progress bar in characters
            stream: Output stream
        """
        self.total = total
        self.message = message
        self.width = width
        self.stream = stream
        self.current = 0

    def update(self, current: int):
        """Update progress bar to current value.

        Args:
            current: Current progress value
        """
        self.current = current
        percent = (current / self.total) * 100 if self.total > 0 else 0
        filled = int(self.width * current / self.total) if self.total > 0 else 0
        bar = '=' * filled + '-' * (self.width - filled)

        self.stream.write(f"\r{self.message}: [{bar}] {percent:.1f}%")
        self.stream.flush()

    def finish(self):
        """Complete the progress bar."""
        self.update(self.total)
        self.stream.write("\n")
        self.stream.flush()


class CountdownTimer:
    """Displays countdown during wait operations.

    Shows elapsed time and optionally remaining time estimate.

    Example:
        timer = CountdownTimer(timeout=300, message="Waiting for Ops Manager")
        timer.start()
        while not ready:
            timer.tick()
            time.sleep(1)
        timer.stop(success=True)
    """

    def __init__(
        self,
        timeout: int,
        message: str = "Waiting",
        stream=sys.stdout
    ):
        """Initialize the countdown timer.

        Args:
            timeout: Maximum wait time in seconds
            message: Message to display
            stream: Output stream
        """
        self.timeout = timeout
        self.message = message
        self.stream = stream
        self.start_time: Optional[float] = None
        self.last_update = 0

    def start(self):
        """Start the countdown timer."""
        self.start_time = time.time()
        self.last_update = 0
        self._display()

    def tick(self):
        """Update the timer display."""
        if self.start_time is None:
            return

        elapsed = int(time.time() - self.start_time)
        # Only update display every second
        if elapsed > self.last_update:
            self.last_update = elapsed
            self._display()

    def _display(self):
        """Display current timer state."""
        if self.start_time is None:
            return

        elapsed = int(time.time() - self.start_time)
        remaining = max(0, self.timeout - elapsed)

        self.stream.write(
            f"\r{self.message}... "
            f"[{elapsed}s elapsed, {remaining}s remaining]   "
        )
        self.stream.flush()

    def stop(self, success: bool = True):
        """Stop the timer and display final status.

        Args:
            success: Whether the operation succeeded
        """
        elapsed = int(time.time() - self.start_time) if self.start_time else 0
        status = "completed" if success else "timed out"
        self.stream.write(f"\r{self.message}... {status} ({elapsed}s)\n")
        self.stream.flush()


# =============================================================================
# Message Formatting
# =============================================================================

def format_error_with_suggestion(
    error: str,
    suggestion: str,
    details: Optional[str] = None
) -> str:
    """Format an error message with actionable suggestion.

    Args:
        error: The error message
        suggestion: Suggested action to fix the error
        details: Optional additional details

    Returns:
        Formatted error message
    """
    lines = [
        f"\n{'='*60}",
        f"ERROR: {error}",
        f"{'='*60}",
    ]

    if details:
        lines.append(f"\nDetails: {details}")

    lines.extend([
        f"\nSuggested action:",
        f"  {suggestion}",
        f"{'='*60}\n"
    ])

    return "\n".join(lines)


def format_success_message(message: str, details: Optional[dict] = None) -> str:
    """Format a success message with optional details.

    Args:
        message: Success message
        details: Optional dictionary of details to display

    Returns:
        Formatted success message
    """
    lines = [
        f"\n{'='*60}",
        f"SUCCESS: {message}",
        f"{'='*60}",
    ]

    if details:
        lines.append("")
        for key, value in details.items():
            lines.append(f"  {key}: {value}")

    lines.append(f"{'='*60}\n")
    return "\n".join(lines)


def print_step(step_number: int, total_steps: int, message: str):
    """Print a deployment step indicator.

    Args:
        step_number: Current step number
        total_steps: Total number of steps
        message: Step description
    """
    logger.info(f"Step [{step_number}/{total_steps}]: {message}")


def print_section_header(title: str, width: int = 60):
    """Print a section header.

    Args:
        title: Section title
        width: Header width
    """
    print(f"\n{'='*width}")
    print(f" {title}")
    print(f"{'='*width}")


# =============================================================================
# Deployment Summary
# =============================================================================

class DeploymentSummary:
    """Tracks and displays deployment progress and final summary.

    Collects information about each step of the deployment and generates
    a formatted summary at the end showing what was created/configured.

    Example:
        summary = DeploymentSummary("MongoDB Replica Set Deployment")
        summary.add_step("Create cluster", success=True, duration=45.2)
        summary.add_step("Deploy operator", success=True, duration=30.1)
        summary.add_resource("Cluster", "mongodb-cluster")
        summary.add_connection_info("MongoDB", "localhost:30000,localhost:30001,localhost:30002")
        print(summary.format())
    """

    def __init__(self, title: str):
        """Initialize deployment summary.

        Args:
            title: Title for the deployment summary
        """
        self.title = title
        self.steps: list = []
        self.resources: dict = {}
        self.connection_info: dict = {}
        self.warnings: list = []
        self.start_time = time.time()

    def add_step(
        self,
        name: str,
        success: bool = True,
        duration: Optional[float] = None,
        message: Optional[str] = None
    ):
        """Record a deployment step.

        Args:
            name: Step name
            success: Whether the step succeeded
            duration: Time taken in seconds
            message: Optional message or error details
        """
        self.steps.append({
            "name": name,
            "success": success,
            "duration": duration,
            "message": message
        })

    def add_resource(self, resource_type: str, name: str):
        """Record a created resource.

        Args:
            resource_type: Type of resource (Cluster, Namespace, etc.)
            name: Resource name
        """
        if resource_type not in self.resources:
            self.resources[resource_type] = []
        self.resources[resource_type].append(name)

    def add_connection_info(self, service: str, connection_string: str):
        """Record connection information.

        Args:
            service: Service name (MongoDB, Ops Manager, etc.)
            connection_string: How to connect to the service
        """
        self.connection_info[service] = connection_string

    def add_warning(self, message: str):
        """Add a warning to display in the summary.

        Args:
            message: Warning message
        """
        self.warnings.append(message)

    @property
    def total_duration(self) -> float:
        """Get total deployment duration in seconds."""
        return time.time() - self.start_time

    @property
    def success(self) -> bool:
        """Check if all steps succeeded."""
        return all(step["success"] for step in self.steps)

    def format(self, verbose: bool = False) -> str:
        """Format the deployment summary.

        Args:
            verbose: Whether to include additional details

        Returns:
            Formatted summary string
        """
        total_time = self.total_duration
        status = "[SUCCESS]" if self.success else "[FAILED]"

        lines = [
            "",
            "=" * 70,
            f"  {self.title}",
            "=" * 70,
            f"  Status: {status}",
            f"  Total Time: {total_time:.1f}s ({total_time/60:.1f} minutes)",
            ""
        ]

        # Steps summary
        if self.steps:
            lines.append("  Steps:")
            for step in self.steps:
                icon = "[OK]" if step["success"] else "[FAILED]"
                duration_str = f" ({step['duration']:.1f}s)" if step["duration"] else ""
                lines.append(f"    {icon} {step['name']}{duration_str}")
                if not step["success"] and step["message"]:
                    lines.append(f"        Error: {step['message']}")
            lines.append("")

        # Resources created
        if self.resources:
            lines.append("  Resources Created:")
            for resource_type, names in self.resources.items():
                for name in names:
                    lines.append(f"    - {resource_type}: {name}")
            lines.append("")

        # Connection information
        if self.connection_info and self.success:
            lines.append("  Connection Information:")
            for service, conn_str in self.connection_info.items():
                lines.append(f"    {service}: {conn_str}")
            lines.append("")

        # Warnings
        if self.warnings:
            lines.append("  Warnings:")
            for warning in self.warnings:
                lines.append(f"    [!] {warning}")
            lines.append("")

        lines.append("=" * 70)
        return "\n".join(lines)


class StepTracker:
    """Tracks progress through a series of numbered steps.

    Provides consistent step numbering and timing throughout deployment.

    Example:
        tracker = StepTracker(total_steps=5)
        with tracker.step("Creating cluster"):
            create_cluster()
        with tracker.step("Deploying operator"):
            deploy_operator()
    """

    def __init__(self, total_steps: int, logger_instance=None):
        """Initialize step tracker.

        Args:
            total_steps: Total number of steps
            logger_instance: Logger to use (default: module logger)
        """
        self.total_steps = total_steps
        self.current_step = 0
        self._logger = logger_instance or logger
        self.step_times: list = []

    def step(self, description: str):
        """Create a context manager for a step.

        Args:
            description: Step description

        Returns:
            StepContext for use in with statement
        """
        return StepContext(self, description)

    def _start_step(self, description: str) -> float:
        """Start a new step (called by StepContext).

        Args:
            description: Step description

        Returns:
            Start time
        """
        self.current_step += 1
        self._logger.info(
            f"Step [{self.current_step}/{self.total_steps}]: {description}"
        )
        return time.time()

    def _end_step(self, start_time: float, success: bool = True):
        """End current step (called by StepContext).

        Args:
            start_time: When the step started
            success: Whether step succeeded
        """
        duration = time.time() - start_time
        self.step_times.append(duration)
        status = "completed" if success else "failed"
        self._logger.debug(f"  Step {status} in {duration:.1f}s")

    @property
    def elapsed_time(self) -> float:
        """Get total elapsed time."""
        return sum(self.step_times)


class StepContext:
    """Context manager for a single step in StepTracker."""

    def __init__(self, tracker: StepTracker, description: str):
        self.tracker = tracker
        self.description = description
        self.start_time: float = 0
        self.success = True

    def __enter__(self):
        self.start_time = self.tracker._start_step(self.description)
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.success = exc_type is None
        self.tracker._end_step(self.start_time, self.success)
        return False  # Don't suppress exceptions


def format_duration(seconds: float) -> str:
    """Format duration in human-readable form.

    Args:
        seconds: Duration in seconds

    Returns:
        Formatted string (e.g., "2m 30s" or "45s")
    """
    if seconds < 60:
        return f"{seconds:.0f}s"
    elif seconds < 3600:
        minutes = int(seconds // 60)
        secs = int(seconds % 60)
        return f"{minutes}m {secs}s"
    else:
        hours = int(seconds // 3600)
        minutes = int((seconds % 3600) // 60)
        return f"{hours}h {minutes}m"


def format_bytes(num_bytes: int) -> str:
    """Format bytes in human-readable form.

    Args:
        num_bytes: Number of bytes

    Returns:
        Formatted string (e.g., "1.5 GB")
    """
    for unit in ['B', 'KB', 'MB', 'GB', 'TB']:
        if abs(num_bytes) < 1024.0:
            return f"{num_bytes:.1f} {unit}"
        num_bytes /= 1024.0
    return f"{num_bytes:.1f} PB"
