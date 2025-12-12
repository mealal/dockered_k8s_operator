"""
Data models for MongoDB Kubernetes deployment scripts.

Contains dataclasses and configuration models used by both single-cluster
and multi-cluster deployment scripts.
"""

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

logger = logging.getLogger(__name__)


@dataclass
class OpsManagerCredentials:
    """Credentials for connecting to MongoDB Ops Manager.

    Contains API credentials and connection details needed to authenticate
    with Ops Manager for deploying MongoDB resources.

    Attributes:
        public_key: API public key for authentication
        private_key: API private key for authentication
        base_url: Base URL of Ops Manager instance
        org_id: Organization ID in Ops Manager
        project_id: Project ID in Ops Manager
        project_name: Human-readable project name
    """
    public_key: str
    private_key: str
    base_url: str
    org_id: str
    project_id: str
    project_name: str = "Default"

    @classmethod
    def from_file(cls, filepath: str, project_type: str = "singleCluster") -> 'OpsManagerCredentials':
        """Load credentials from a JSON file.

        Supports both snake_case and camelCase field names for compatibility.
        Also supports multi-project config files with "projects" section.

        Args:
            filepath: Path to JSON credentials file
            project_type: Which project to load - "singleCluster" or "multiCluster"

        Returns:
            OpsManagerCredentials instance

        Raises:
            FileNotFoundError: If credentials file doesn't exist
            json.JSONDecodeError: If file is not valid JSON
            KeyError: If required fields are missing
        """
        with open(filepath) as f:
            data = json.load(f)

        # Check for multi-project config (new format)
        projects = data.get('projects')
        if projects and project_type in projects:
            project_data = projects[project_type]
            project_id = project_data.get('projectId')
            project_name = project_data.get('projectName', project_type)
        else:
            # Legacy format - use top-level project fields
            project_id = data.get('projectId') or data.get('project_id')
            project_name = data.get('projectName') or data.get('project_name', 'Default')

        # Support both camelCase and snake_case field names
        return cls(
            public_key=data.get('publicKey') or data.get('public_key'),
            private_key=data.get('privateKey') or data.get('private_key'),
            base_url=data.get('baseUrl') or data.get('base_url'),
            org_id=data.get('orgId') or data.get('org_id'),
            project_id=project_id,
            project_name=project_name
        )

    def validate(self) -> List[str]:
        """Validate credentials and return list of errors.

        Returns:
            List of validation error messages (empty if valid)
        """
        errors = []
        if not self.public_key:
            errors.append("public_key is required")
        if not self.private_key:
            errors.append("private_key is required")
        if not self.base_url:
            errors.append("base_url is required")
        if not self.org_id:
            errors.append("org_id is required")
        if not self.base_url.startswith(('http://', 'https://')):
            errors.append("base_url must start with http:// or https://")
        return errors


@dataclass
class CertificateConfig:
    """Configuration for certificate paths.

    Centralizes all certificate path configurations to avoid hardcoding
    paths throughout the codebase.

    Attributes:
        base_dir: Base directory for all certificates
    """
    base_dir: Path = Path("./certs")

    @property
    def ca_cert(self) -> Path:
        """Path to CA certificate."""
        return self.base_dir / "ca.crt"

    @property
    def ca_key(self) -> Path:
        """Path to CA private key."""
        return self.base_dir / "ca.key"

    @property
    def mongodb_certs_dir(self) -> Path:
        """Directory for MongoDB-specific certificates."""
        return self.base_dir / "mongodb"

    def ensure_dirs(self) -> None:
        """Create certificate directories if they don't exist."""
        self.base_dir.mkdir(parents=True, exist_ok=True)
        self.mongodb_certs_dir.mkdir(parents=True, exist_ok=True)

    def validate(self) -> List[str]:
        """Validate certificate configuration.

        Returns:
            List of validation error messages (empty if valid)
        """
        errors = []
        if not self.base_dir.exists():
            errors.append(f"Certificate directory does not exist: {self.base_dir}")
        if not self.ca_cert.exists():
            errors.append(f"CA certificate not found: {self.ca_cert}")
        if not self.ca_key.exists():
            errors.append(f"CA key not found: {self.ca_key}")
        return errors
