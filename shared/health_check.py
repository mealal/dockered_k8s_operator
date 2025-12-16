"""
Health check utilities for MongoDB deployments.

Provides connection testing and health validation after deployment
to ensure MongoDB is actually accessible and functioning correctly.

Features:
- TCP connectivity testing
- TLS handshake validation
- Retry logic with exponential backoff
- Detailed diagnostic information
- Progress reporting during health checks
"""

import logging
import socket
import ssl
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Tuple, Callable

from .constants import (
    CONNECTION_TIMEOUT,
    HEALTH_CHECK_INTERVAL,
    HEALTH_CHECK_TIMEOUT,
    MONGODB_PORT,
)
from .decorators import ExponentialBackoff, poll_with_backoff, PollResult

logger = logging.getLogger(__name__)


@dataclass
class HealthCheckResult:
    """Result of a MongoDB health check.

    Attributes:
        success: Whether the health check passed
        message: Human-readable status message
        hosts_checked: Number of hosts that were checked
        hosts_reachable: Number of hosts that were reachable
        primary_found: Whether a primary was identified
        details: Additional diagnostic details
        attempts: Number of check attempts made
        elapsed: Time elapsed during health check in seconds
        host_results: Per-host check results
    """
    success: bool
    message: str
    hosts_checked: int = 0
    hosts_reachable: int = 0
    primary_found: bool = False
    details: Optional[str] = None
    attempts: int = 1
    elapsed: float = 0.0
    host_results: List[Tuple[str, bool, str]] = field(default_factory=list)


class MongoDBHealthChecker:
    """Checks MongoDB deployment health and connectivity.

    Performs TCP connectivity tests and basic TLS handshake validation
    to verify that MongoDB is accessible after deployment.

    Attributes:
        hosts: List of host:port strings to check
        tls_enabled: Whether TLS is enabled
        ca_cert_path: Path to CA certificate for TLS verification
    """

    def __init__(
        self,
        hosts: List[str],
        tls_enabled: bool = True,
        ca_cert_path: Optional[Path] = None,
        timeout: int = CONNECTION_TIMEOUT
    ):
        """Initialize the health checker.

        Args:
            hosts: List of host:port strings (e.g., ["localhost:30000", "localhost:30001"])
            tls_enabled: Whether TLS is enabled on the MongoDB deployment
            ca_cert_path: Path to CA certificate for TLS verification
            timeout: Connection timeout in seconds
        """
        self.hosts = hosts
        self.tls_enabled = tls_enabled
        self.ca_cert_path = Path(ca_cert_path) if ca_cert_path else None
        self.timeout = timeout

    def _parse_host(self, host_str: str) -> Tuple[str, int]:
        """Parse host:port string into components.

        Args:
            host_str: Host string in format "hostname:port" or "hostname"

        Returns:
            Tuple of (hostname, port)
        """
        if ':' in host_str:
            parts = host_str.rsplit(':', 1)
            return parts[0], int(parts[1])
        return host_str, MONGODB_PORT

    def check_tcp_connectivity(self, host: str, port: int) -> Tuple[bool, str]:
        """Check if TCP connection can be established.

        Args:
            host: Hostname to connect to
            port: Port number

        Returns:
            Tuple of (success, message)
        """
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(self.timeout)
            result = sock.connect_ex((host, port))
            sock.close()

            if result == 0:
                return True, f"TCP connection to {host}:{port} successful"
            else:
                return False, f"TCP connection to {host}:{port} failed (error code: {result})"

        except socket.timeout:
            return False, f"TCP connection to {host}:{port} timed out"
        except socket.gaierror as e:
            return False, f"DNS resolution failed for {host}: {e}"
        except Exception as e:
            return False, f"TCP connection error to {host}:{port}: {e}"

    def check_tls_handshake(self, host: str, port: int) -> Tuple[bool, str]:
        """Check if TLS handshake completes successfully.

        Args:
            host: Hostname to connect to
            port: Port number

        Returns:
            Tuple of (success, message)
        """
        if not self.tls_enabled:
            return True, "TLS not enabled, skipping handshake check"

        try:
            context = ssl.create_default_context()

            # Load CA certificate if provided
            if self.ca_cert_path and self.ca_cert_path.exists():
                context.load_verify_locations(str(self.ca_cert_path))
            else:
                # For self-signed certs without CA verification
                context.check_hostname = False
                context.verify_mode = ssl.CERT_NONE

            with socket.create_connection((host, port), timeout=self.timeout) as sock:
                with context.wrap_socket(sock, server_hostname=host) as ssock:
                    # Get certificate info
                    cert = ssock.getpeercert()
                    if cert:
                        return True, f"TLS handshake successful with {host}:{port}"
                    else:
                        return True, f"TLS handshake completed (no cert info with CERT_NONE)"

        except ssl.SSLError as e:
            return False, f"TLS handshake failed with {host}:{port}: {e}"
        except socket.timeout:
            return False, f"TLS handshake timed out with {host}:{port}"
        except Exception as e:
            return False, f"TLS connection error to {host}:{port}: {e}"

    def check_single_host(self, host_str: str, retries: int = 3) -> Tuple[bool, str]:
        """Check connectivity to a single host with retry logic.

        Args:
            host_str: Host string in format "hostname:port"
            retries: Number of retry attempts for transient failures

        Returns:
            Tuple of (success, message)
        """
        host, port = self._parse_host(host_str)
        last_error = ""

        for attempt in range(retries):
            # First check TCP connectivity
            tcp_ok, tcp_msg = self.check_tcp_connectivity(host, port)
            if not tcp_ok:
                last_error = tcp_msg
                if attempt < retries - 1:
                    logger.debug(f"TCP check failed for {host}:{port}, retrying... ({attempt + 1}/{retries})")
                    time.sleep(1)  # Brief pause before retry
                    continue
                return False, tcp_msg

            # Then check TLS if enabled
            if self.tls_enabled:
                tls_ok, tls_msg = self.check_tls_handshake(host, port)
                if not tls_ok:
                    last_error = tls_msg
                    if attempt < retries - 1:
                        logger.debug(f"TLS check failed for {host}:{port}, retrying... ({attempt + 1}/{retries})")
                        time.sleep(1)
                        continue
                    return False, tls_msg
                return True, tls_msg

            return True, tcp_msg

        return False, last_error

    def check_all_hosts(self) -> HealthCheckResult:
        """Check connectivity to all configured hosts.

        Returns:
            HealthCheckResult with overall status and details
        """
        if not self.hosts:
            return HealthCheckResult(
                success=False,
                message="No hosts configured for health check"
            )

        results = []
        host_results = []
        hosts_reachable = 0

        for host_str in self.hosts:
            success, message = self.check_single_host(host_str)
            results.append(f"  {host_str}: {'OK' if success else 'FAILED'} - {message}")
            host_results.append((host_str, success, message))
            if success:
                hosts_reachable += 1

        details = "\n".join(results)

        # For a replica set, we need at least one host reachable
        # (could be stricter and require majority)
        if hosts_reachable == 0:
            return HealthCheckResult(
                success=False,
                message="No MongoDB hosts are reachable",
                hosts_checked=len(self.hosts),
                hosts_reachable=0,
                details=details,
                host_results=host_results
            )
        elif hosts_reachable < len(self.hosts):
            return HealthCheckResult(
                success=True,
                message=f"MongoDB partially reachable ({hosts_reachable}/{len(self.hosts)} hosts)",
                hosts_checked=len(self.hosts),
                hosts_reachable=hosts_reachable,
                details=details,
                host_results=host_results
            )
        else:
            return HealthCheckResult(
                success=True,
                message=f"All MongoDB hosts reachable ({hosts_reachable}/{len(self.hosts)})",
                hosts_checked=len(self.hosts),
                hosts_reachable=hosts_reachable,
                details=details,
                host_results=host_results
            )

    def wait_for_healthy(
        self,
        timeout: int = 60,
        interval: int = HEALTH_CHECK_INTERVAL,
        use_backoff: bool = True,
        on_progress: Optional[Callable[[int, float, HealthCheckResult], None]] = None
    ) -> HealthCheckResult:
        """Wait for MongoDB to become healthy with exponential backoff.

        Polls connectivity until timeout or success. Uses exponential backoff
        by default for more efficient polling.

        Args:
            timeout: Maximum time to wait in seconds
            interval: Initial/base polling interval in seconds
            use_backoff: If True, use exponential backoff for polling
            on_progress: Optional callback(attempt, elapsed, result) for progress updates

        Returns:
            HealthCheckResult with final status including attempt count and elapsed time
        """
        logger.info(f"Waiting for MongoDB connectivity (timeout: {timeout}s)...")
        start_time = time.time()
        attempts = 0

        if use_backoff:
            # Use exponential backoff: starts at interval, doubles each time up to 30s
            backoff = ExponentialBackoff(
                initial=float(interval),
                multiplier=1.5,
                max_interval=30.0,
                jitter=True  # Add jitter to prevent thundering herd
            )

            def check_func() -> Tuple[bool, HealthCheckResult]:
                nonlocal attempts
                attempts += 1
                result = self.check_all_hosts()
                return (result.success, result)

            def progress_callback(attempt: int, elapsed: float, result: HealthCheckResult):
                if not result.success:
                    reachable = result.hosts_reachable
                    total = result.hosts_checked
                    logger.info(f"[{elapsed:.0f}s] Waiting for MongoDB... ({reachable}/{total} hosts reachable)")
                if on_progress:
                    on_progress(attempt, elapsed, result)

            poll_result: PollResult[HealthCheckResult] = poll_with_backoff(
                check_func=check_func,
                timeout=float(timeout),
                backoff=backoff,
                on_attempt=progress_callback,
                description="MongoDB connectivity"
            )

            elapsed = time.time() - start_time

            if poll_result.success and poll_result.value:
                result = poll_result.value
                result.attempts = poll_result.attempts
                result.elapsed = elapsed
                logger.info(f"MongoDB connectivity check passed after {poll_result.attempts} attempts ({elapsed:.1f}s)")
                return result

            # Return the last result with attempt info
            if poll_result.value:
                result = poll_result.value
                result.attempts = poll_result.attempts
                result.elapsed = elapsed
            else:
                result = HealthCheckResult(
                    success=False,
                    message=f"Health check timed out after {timeout}s",
                    attempts=poll_result.attempts,
                    elapsed=elapsed
                )

            logger.error(f"MongoDB connectivity check failed after {poll_result.attempts} attempts ({elapsed:.1f}s)")
            if result.details:
                logger.error(result.details)
            return result

        else:
            # Legacy constant-interval polling
            while time.time() - start_time < timeout:
                attempts += 1
                result = self.check_all_hosts()
                if result.success:
                    elapsed = time.time() - start_time
                    result.attempts = attempts
                    result.elapsed = elapsed
                    logger.info(f"MongoDB connectivity check passed: {result.message}")
                    return result

                elapsed = int(time.time() - start_time)
                logger.debug(f"[{elapsed}s] MongoDB not yet reachable, retrying...")
                if on_progress:
                    on_progress(attempts, elapsed, result)
                time.sleep(interval)

            # Final check
            attempts += 1
            result = self.check_all_hosts()
            elapsed = time.time() - start_time
            result.attempts = attempts
            result.elapsed = elapsed

            if not result.success:
                logger.error(f"MongoDB connectivity check failed after {timeout}s")
                logger.error(result.details)
            return result


def verify_mongodb_deployment(
    hosts: List[str],
    tls_enabled: bool = True,
    ca_cert_path: Optional[Path] = None,
    timeout: int = 60
) -> bool:
    """Convenience function to verify MongoDB deployment is accessible.

    Args:
        hosts: List of host:port strings
        tls_enabled: Whether TLS is enabled
        ca_cert_path: Path to CA certificate
        timeout: How long to wait for connectivity

    Returns:
        True if deployment is accessible, False otherwise
    """
    checker = MongoDBHealthChecker(
        hosts=hosts,
        tls_enabled=tls_enabled,
        ca_cert_path=ca_cert_path
    )
    result = checker.wait_for_healthy(timeout=timeout)
    return result.success


def format_health_check_result(result: HealthCheckResult, verbose: bool = False) -> str:
    """Format health check result for display.

    Args:
        result: HealthCheckResult to format
        verbose: If True, include additional diagnostic information

    Returns:
        Formatted string for console output
    """
    # Use ASCII-safe characters for Windows compatibility
    status_icon = "[OK]" if result.success else "[FAILED]"
    lines = [
        f"\n{'='*60}",
        f"MONGODB CONNECTIVITY CHECK: {status_icon} {result.message}",
        f"{'='*60}",
    ]

    # Add timing information
    if result.elapsed > 0:
        lines.append(f"Duration: {result.elapsed:.1f}s ({result.attempts} attempt(s))")

    if result.details:
        lines.append("")
        lines.append("Host Status:")
        lines.append(result.details)

    # Add verbose diagnostics for failures
    if verbose and not result.success:
        lines.append("")
        lines.append("Troubleshooting Tips:")
        lines.append("  1. Verify MongoDB pods are running: kubectl get pods -n mongodb-rs")
        lines.append("  2. Check operator logs: kubectl logs -n mongodb -l app.kubernetes.io/name=mongodb-enterprise-operator")
        lines.append("  3. Verify port mappings: docker port <cluster-name>-control-plane")
        lines.append("  4. Check firewall allows connections to the MongoDB ports")

    lines.append(f"{'='*60}\n")
    return "\n".join(lines)


def get_diagnostic_info(hosts: List[str], ca_cert_path: Optional[Path] = None) -> str:
    """Get detailed diagnostic information about connectivity issues.

    Performs additional checks beyond basic health checking to help
    diagnose connectivity problems.

    Args:
        hosts: List of host:port strings to check
        ca_cert_path: Path to CA certificate

    Returns:
        Formatted diagnostic string
    """
    lines = ["MongoDB Connectivity Diagnostics", "=" * 40]

    for host_str in hosts:
        lines.append(f"\nChecking {host_str}:")

        # Parse host
        if ':' in host_str:
            host, port = host_str.rsplit(':', 1)
            port = int(port)
        else:
            host, port = host_str, MONGODB_PORT

        # DNS resolution check
        try:
            ip_addr = socket.gethostbyname(host)
            lines.append(f"  DNS: {host} -> {ip_addr}")
        except socket.gaierror as e:
            lines.append(f"  DNS: FAILED - {e}")
            continue

        # TCP connectivity check
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(5)
            result = sock.connect_ex((host, port))
            sock.close()
            if result == 0:
                lines.append(f"  TCP: Port {port} is open")
            else:
                lines.append(f"  TCP: Port {port} is closed (error code: {result})")
                continue
        except Exception as e:
            lines.append(f"  TCP: Connection failed - {e}")
            continue

        # TLS check
        try:
            context = ssl.create_default_context()
            if ca_cert_path and Path(ca_cert_path).exists():
                context.load_verify_locations(str(ca_cert_path))
            else:
                context.check_hostname = False
                context.verify_mode = ssl.CERT_NONE

            with socket.create_connection((host, port), timeout=5) as sock:
                with context.wrap_socket(sock, server_hostname=host) as ssock:
                    cert = ssock.getpeercert()
                    if cert:
                        # Get subject info
                        subject = dict(x[0] for x in cert.get('subject', []))
                        cn = subject.get('commonName', 'Unknown')
                        lines.append(f"  TLS: Handshake successful (CN: {cn})")
                    else:
                        lines.append(f"  TLS: Handshake successful (no cert verification)")
        except ssl.SSLError as e:
            lines.append(f"  TLS: Handshake failed - {e}")
        except Exception as e:
            lines.append(f"  TLS: Error - {e}")

    lines.append("")
    return "\n".join(lines)
