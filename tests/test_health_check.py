"""Unit tests for health check module."""

import unittest
import sys
import os
from unittest.mock import patch, MagicMock

# Add parent directory to path for imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from shared.health_check import (
    HealthCheckResult,
    MongoDBHealthChecker,
    format_health_check_result,
    get_diagnostic_info,
)


class TestHealthCheckResult(unittest.TestCase):
    """Test cases for HealthCheckResult dataclass."""

    def test_successful_result(self):
        """Test creating a successful health check result."""
        result = HealthCheckResult(
            success=True,
            message="All hosts reachable",
            hosts_checked=3,
            hosts_reachable=3
        )
        self.assertTrue(result.success)
        self.assertEqual(result.hosts_checked, 3)
        self.assertEqual(result.hosts_reachable, 3)

    def test_failed_result(self):
        """Test creating a failed health check result."""
        result = HealthCheckResult(
            success=False,
            message="No hosts reachable",
            hosts_checked=3,
            hosts_reachable=0,
            details="Connection refused"
        )
        self.assertFalse(result.success)
        self.assertEqual(result.details, "Connection refused")

    def test_default_values(self):
        """Test default values are set correctly."""
        result = HealthCheckResult(success=True, message="OK")
        self.assertEqual(result.hosts_checked, 0)
        self.assertEqual(result.hosts_reachable, 0)
        self.assertFalse(result.primary_found)
        self.assertEqual(result.attempts, 1)
        self.assertEqual(result.elapsed, 0.0)
        self.assertEqual(result.host_results, [])


class TestMongoDBHealthChecker(unittest.TestCase):
    """Test cases for MongoDBHealthChecker class."""

    def test_parse_host_with_port(self):
        """Test parsing host:port string."""
        checker = MongoDBHealthChecker(hosts=["localhost:27017"])
        host, port = checker._parse_host("localhost:27017")
        self.assertEqual(host, "localhost")
        self.assertEqual(port, 27017)

    def test_parse_host_without_port(self):
        """Test parsing host without port uses default."""
        checker = MongoDBHealthChecker(hosts=["localhost"])
        host, port = checker._parse_host("localhost")
        self.assertEqual(host, "localhost")
        self.assertEqual(port, 27017)  # Default MongoDB port

    def test_parse_host_ipv4(self):
        """Test parsing IPv4 address with port."""
        checker = MongoDBHealthChecker(hosts=["192.168.1.100:30000"])
        host, port = checker._parse_host("192.168.1.100:30000")
        self.assertEqual(host, "192.168.1.100")
        self.assertEqual(port, 30000)

    @patch('socket.socket')
    def test_tcp_connectivity_success(self, mock_socket_class):
        """Test successful TCP connectivity check."""
        mock_socket = MagicMock()
        mock_socket.connect_ex.return_value = 0
        mock_socket_class.return_value = mock_socket

        checker = MongoDBHealthChecker(hosts=["localhost:27017"], tls_enabled=False)
        success, message = checker.check_tcp_connectivity("localhost", 27017)

        self.assertTrue(success)
        self.assertIn("successful", message)
        mock_socket.settimeout.assert_called_once()
        mock_socket.close.assert_called_once()

    @patch('socket.socket')
    def test_tcp_connectivity_failure(self, mock_socket_class):
        """Test failed TCP connectivity check."""
        mock_socket = MagicMock()
        mock_socket.connect_ex.return_value = 111  # Connection refused
        mock_socket_class.return_value = mock_socket

        checker = MongoDBHealthChecker(hosts=["localhost:27017"], tls_enabled=False)
        success, message = checker.check_tcp_connectivity("localhost", 27017)

        self.assertFalse(success)
        self.assertIn("failed", message)

    @patch('socket.socket')
    def test_tcp_connectivity_timeout(self, mock_socket_class):
        """Test TCP connectivity timeout."""
        import socket
        mock_socket = MagicMock()
        mock_socket.connect_ex.side_effect = socket.timeout("timed out")
        mock_socket_class.return_value = mock_socket

        checker = MongoDBHealthChecker(hosts=["localhost:27017"], tls_enabled=False)
        success, message = checker.check_tcp_connectivity("localhost", 27017)

        self.assertFalse(success)
        self.assertIn("timed out", message)

    @patch('socket.socket')
    def test_tcp_connectivity_dns_failure(self, mock_socket_class):
        """Test TCP connectivity with DNS failure."""
        import socket
        mock_socket = MagicMock()
        mock_socket.connect_ex.side_effect = socket.gaierror(11001, "getaddrinfo failed")
        mock_socket_class.return_value = mock_socket

        checker = MongoDBHealthChecker(hosts=["invalid.host:27017"], tls_enabled=False)
        success, message = checker.check_tcp_connectivity("invalid.host", 27017)

        self.assertFalse(success)
        self.assertIn("DNS", message)

    def test_tls_skipped_when_disabled(self):
        """Test TLS check is skipped when TLS is disabled."""
        checker = MongoDBHealthChecker(hosts=["localhost:27017"], tls_enabled=False)
        success, message = checker.check_tls_handshake("localhost", 27017)

        self.assertTrue(success)
        self.assertIn("not enabled", message)

    def test_check_all_hosts_empty(self):
        """Test check_all_hosts with no hosts."""
        checker = MongoDBHealthChecker(hosts=[])
        result = checker.check_all_hosts()

        self.assertFalse(result.success)
        self.assertIn("No hosts", result.message)

    @patch.object(MongoDBHealthChecker, 'check_single_host')
    def test_check_all_hosts_all_succeed(self, mock_check):
        """Test check_all_hosts when all hosts succeed."""
        mock_check.return_value = (True, "Connection successful")

        checker = MongoDBHealthChecker(hosts=["host1:27017", "host2:27017", "host3:27017"])
        result = checker.check_all_hosts()

        self.assertTrue(result.success)
        self.assertEqual(result.hosts_checked, 3)
        self.assertEqual(result.hosts_reachable, 3)
        self.assertIn("All", result.message)

    @patch.object(MongoDBHealthChecker, 'check_single_host')
    def test_check_all_hosts_partial_success(self, mock_check):
        """Test check_all_hosts with partial success."""
        mock_check.side_effect = [
            (True, "Success"),
            (False, "Failed"),
            (True, "Success")
        ]

        checker = MongoDBHealthChecker(hosts=["host1:27017", "host2:27017", "host3:27017"])
        result = checker.check_all_hosts()

        self.assertTrue(result.success)  # Still success if any reachable
        self.assertEqual(result.hosts_checked, 3)
        self.assertEqual(result.hosts_reachable, 2)
        self.assertIn("partially", result.message)

    @patch.object(MongoDBHealthChecker, 'check_single_host')
    def test_check_all_hosts_all_fail(self, mock_check):
        """Test check_all_hosts when all hosts fail."""
        mock_check.return_value = (False, "Connection failed")

        checker = MongoDBHealthChecker(hosts=["host1:27017", "host2:27017"])
        result = checker.check_all_hosts()

        self.assertFalse(result.success)
        self.assertEqual(result.hosts_reachable, 0)
        self.assertIn("No MongoDB hosts", result.message)


class TestFormatHealthCheckResult(unittest.TestCase):
    """Test cases for format_health_check_result function."""

    def test_format_success(self):
        """Test formatting successful result."""
        result = HealthCheckResult(
            success=True,
            message="All hosts reachable",
            hosts_checked=3,
            hosts_reachable=3,
            elapsed=5.2,
            attempts=2
        )
        output = format_health_check_result(result)

        self.assertIn("[OK]", output)
        self.assertIn("All hosts reachable", output)
        self.assertIn("5.2s", output)
        self.assertIn("2 attempt", output)

    def test_format_failure(self):
        """Test formatting failed result."""
        result = HealthCheckResult(
            success=False,
            message="No hosts reachable",
            hosts_checked=3,
            hosts_reachable=0,
            details="Connection refused to all hosts"
        )
        output = format_health_check_result(result)

        self.assertIn("[FAILED]", output)
        self.assertIn("No hosts reachable", output)
        self.assertIn("Connection refused", output)

    def test_format_verbose(self):
        """Test verbose formatting includes troubleshooting tips."""
        result = HealthCheckResult(
            success=False,
            message="Connection failed"
        )
        output = format_health_check_result(result, verbose=True)

        self.assertIn("Troubleshooting", output)
        self.assertIn("kubectl", output)


class TestGetDiagnosticInfo(unittest.TestCase):
    """Test cases for get_diagnostic_info function."""

    @patch('socket.gethostbyname')
    def test_diagnostic_dns_failure(self, mock_dns):
        """Test diagnostics with DNS failure."""
        import socket
        mock_dns.side_effect = socket.gaierror(11001, "getaddrinfo failed")

        output = get_diagnostic_info(["invalid.host:27017"])

        self.assertIn("DNS: FAILED", output)

    @patch('socket.gethostbyname')
    @patch('socket.socket')
    def test_diagnostic_tcp_success(self, mock_socket_class, mock_dns):
        """Test diagnostics with successful TCP check."""
        mock_dns.return_value = "127.0.0.1"
        mock_socket = MagicMock()
        mock_socket.connect_ex.return_value = 0
        mock_socket_class.return_value = mock_socket

        output = get_diagnostic_info(["localhost:27017"])

        self.assertIn("DNS: localhost -> 127.0.0.1", output)
        self.assertIn("TCP: Port 27017 is open", output)


if __name__ == "__main__":
    unittest.main()
