"""
Pre-flight validation checks for MongoDB Kubernetes deployment.

Performs validation checks before deployment to catch common issues
early and provide helpful error messages.
"""

import json
import logging
import ssl
import urllib.error
import urllib.request
from pathlib import Path
from typing import List

logger = logging.getLogger(__name__)


class PreFlightChecker:
    """Pre-flight validation checks before deployment.

    Validates that all prerequisites are met before starting deployment:
    - Docker is running
    - Ops Manager credentials file exists and is valid
    - Ops Manager is accessible
    - Required files exist

    Attributes:
        ops_manager_url: URL of Ops Manager instance
        credentials_file: Path to credentials JSON file
        ca_cert_path: Path to CA certificate for HTTPS
        errors: List of critical errors found
        warnings: List of non-critical warnings
    """

    def __init__(
        self,
        ops_manager_url: str,
        credentials_file: str,
        ca_cert_path: str = "./certs/ca.crt"
    ):
        """Initialize pre-flight checker.

        Args:
            ops_manager_url: URL of Ops Manager instance
            credentials_file: Path to credentials JSON file
            ca_cert_path: Path to CA certificate for HTTPS validation
        """
        # For connectivity check, use localhost instead of host.docker.internal
        # (host.docker.internal is only available inside Docker containers)
        self.ops_manager_url = ops_manager_url.replace(
            "host.docker.internal", "localhost"
        )
        self.credentials_file = credentials_file
        self.ca_cert_path = ca_cert_path
        self.errors: List[str] = []
        self.warnings: List[str] = []

    def check_all(self) -> bool:
        """Run all pre-flight checks.

        Returns:
            True if all critical checks pass, False otherwise
        """
        logger.info("Running pre-flight checks...")

        self._check_docker()
        self._check_credentials_file()
        self._check_ops_manager_connectivity()

        # Report results
        if self.warnings:
            for warning in self.warnings:
                logger.warning(f"Pre-flight warning: {warning}")

        if self.errors:
            for error in self.errors:
                logger.error(f"Pre-flight error: {error}")
            logger.error("Pre-flight checks failed. Please fix the errors above.")
            return False

        logger.info("All pre-flight checks passed")
        return True

    def _check_docker(self) -> bool:
        """Check if Docker is running."""
        import subprocess
        try:
            result = subprocess.run(
                ["docker", "info"],
                capture_output=True,
                text=True,
                encoding='utf-8',
                errors='replace',
                timeout=30
            )
            if result.returncode != 0:
                self.errors.append("Docker is not running. Please start Docker Desktop.")
                return False
            return True
        except FileNotFoundError:
            self.errors.append("Docker is not installed.")
            return False
        except subprocess.TimeoutExpired:
            self.errors.append("Docker command timed out. Docker may be starting up.")
            return False

    def _check_credentials_file(self) -> bool:
        """Check if credentials file exists and is valid JSON."""
        if not Path(self.credentials_file).exists():
            self.errors.append(
                f"Credentials file not found: {self.credentials_file}\n"
                "  Run deploy_ops_manager.py first to generate API keys."
            )
            return False

        try:
            with open(self.credentials_file) as f:
                data = json.load(f)

            # Support both camelCase and snake_case field names
            required_field_variants = [
                ('public_key', 'publicKey'),
                ('private_key', 'privateKey'),
                ('base_url', 'baseUrl'),
                ('org_id', 'orgId'),
                ('project_id', 'projectId'),
            ]
            missing = [
                snake for snake, camel in required_field_variants
                if snake not in data and camel not in data
            ]

            if missing:
                self.errors.append(
                    f"Credentials file missing required fields: {missing}"
                )
                return False

            return True
        except json.JSONDecodeError as e:
            self.errors.append(f"Credentials file is not valid JSON: {e}")
            return False

    def _check_ops_manager_connectivity(self) -> bool:
        """Check if Ops Manager is accessible."""
        try:
            # Create SSL context
            ctx = ssl.create_default_context()

            # Try to use custom CA cert if available
            if Path(self.ca_cert_path).exists():
                ctx.load_verify_locations(self.ca_cert_path)
            else:
                # For self-signed certs without CA file
                ctx.check_hostname = False
                ctx.verify_mode = ssl.CERT_NONE
                self.warnings.append(
                    f"CA certificate not found at {self.ca_cert_path}. "
                    "SSL verification disabled for connectivity check."
                )

            # Try to connect to Ops Manager
            url = f"{self.ops_manager_url}/api/public/v1.0"
            req = urllib.request.Request(url, method='GET')
            req.add_header('Accept', 'application/json')

            with urllib.request.urlopen(req, context=ctx, timeout=10) as response:
                if response.status == 200:
                    return True

        except urllib.error.HTTPError as e:
            # 401 is expected without auth - means server is reachable
            if e.code == 401:
                return True
            self.warnings.append(
                f"Ops Manager returned HTTP {e.code}. Server may be initializing."
            )
            return True
        except urllib.error.URLError as e:
            self.errors.append(
                f"Cannot connect to Ops Manager at {self.ops_manager_url}: {e.reason}\n"
                "  Ensure Ops Manager container is running."
            )
            return False
        except Exception as e:
            self.warnings.append(f"Ops Manager connectivity check failed: {e}")
            return True

        return True

    def _check_template_files(self, template_dir: Path) -> bool:
        """Check if required template files exist.

        Args:
            template_dir: Directory containing YAML templates

        Returns:
            True if all required templates exist
        """
        required_templates = [
            "namespace.yaml",
            "ops-manager-secret.yaml",
            "ops-manager-configmap.yaml",
        ]

        missing = []
        for template in required_templates:
            if not (template_dir / template).exists():
                missing.append(template)

        if missing:
            self.errors.append(
                f"Missing template files in {template_dir}: {missing}"
            )
            return False

        return True
