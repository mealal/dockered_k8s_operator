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

        # Default project names based on project_type
        default_project_names = {
            "singleCluster": "SingleCluster",
            "multiCluster": "MultiCluster"
        }
        default_name = default_project_names.get(project_type, project_type.title())

        # Check for multi-project config (format with projects section)
        projects = data.get('projects')
        if projects and project_type in projects:
            project_data = projects[project_type]
            project_id = project_data.get('projectId')
            project_name = project_data.get('projectName', default_name)
        elif projects:
            # Projects section exists but doesn't have this project_type
            # Don't fall back to top-level projectId - it belongs to a different project
            project_id = None
            project_name = default_name
        else:
            # Legacy format without projects section - use top-level fields
            project_id = data.get('projectId') or data.get('project_id')
            project_name = data.get('projectName') or data.get('project_name') or default_name

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

    def verify_project_exists(
        self,
        ssl_verify: bool = True,
        ca_cert_path: Optional[str] = None
    ) -> bool:
        """Verify that the project exists in Ops Manager.

        Makes an API call to Ops Manager to check if the project_id is valid.

        Args:
            ssl_verify: Whether to verify SSL certificates (default True)
            ca_cert_path: Path to CA certificate for SSL verification

        Returns:
            True if project exists, False otherwise
        """
        import urllib.request
        import urllib.error
        import ssl

        if not self.project_id:
            logger.warning("No project_id configured, cannot verify project exists")
            return False

        # Build the API URL
        api_url = f"{self.base_url}/api/public/v1.0/groups/{self.project_id}"

        # Create password manager for Digest auth (required by Ops Manager API)
        password_mgr = urllib.request.HTTPPasswordMgrWithDefaultRealm()
        password_mgr.add_password(None, api_url, self.public_key, self.private_key)

        # Create handlers
        handlers = [urllib.request.HTTPDigestAuthHandler(password_mgr)]

        # Configure SSL context
        ssl_context = ssl.create_default_context()
        if not ssl_verify:
            ssl_context.check_hostname = False
            ssl_context.verify_mode = ssl.CERT_NONE
        elif ca_cert_path and Path(ca_cert_path).exists():
            ssl_context.load_verify_locations(ca_cert_path)
        handlers.append(urllib.request.HTTPSHandler(context=ssl_context))

        opener = urllib.request.build_opener(*handlers)

        try:
            request = urllib.request.Request(api_url)
            request.add_header('Accept', 'application/json')
            with opener.open(request, timeout=30) as response:
                if response.status == 200:
                    logger.info(f"Project '{self.project_name}' (ID: {self.project_id}) exists in Ops Manager")
                    return True
        except urllib.error.HTTPError as e:
            if e.code == 404:
                logger.warning(f"Project with ID '{self.project_id}' not found in Ops Manager")
            elif e.code == 401:
                logger.error("Authentication failed - check API credentials")
            else:
                logger.error(f"HTTP error checking project: {e.code} {e.reason}")
            return False
        except urllib.error.URLError as e:
            logger.error(f"Failed to connect to Ops Manager: {e.reason}")
            return False
        except Exception as e:
            logger.error(f"Error verifying project: {e}")
            return False

        return False

    def _find_project_by_name(
        self,
        ssl_verify: bool = True,
        ca_cert_path: Optional[str] = None
    ) -> Optional[str]:
        """Find a project by name in the organization.

        Args:
            ssl_verify: Whether to verify SSL certificates
            ca_cert_path: Path to CA certificate for SSL verification

        Returns:
            Project ID if found, None otherwise
        """
        import urllib.request
        import urllib.error
        import ssl

        if not self.org_id:
            return None

        # List all projects in the organization
        api_url = f"{self.base_url}/api/public/v1.0/orgs/{self.org_id}/groups"

        password_mgr = urllib.request.HTTPPasswordMgrWithDefaultRealm()
        password_mgr.add_password(None, api_url, self.public_key, self.private_key)

        handlers = [urllib.request.HTTPDigestAuthHandler(password_mgr)]

        ssl_context = ssl.create_default_context()
        if not ssl_verify:
            ssl_context.check_hostname = False
            ssl_context.verify_mode = ssl.CERT_NONE
        elif ca_cert_path and Path(ca_cert_path).exists():
            ssl_context.load_verify_locations(ca_cert_path)
        handlers.append(urllib.request.HTTPSHandler(context=ssl_context))

        opener = urllib.request.build_opener(*handlers)

        try:
            request = urllib.request.Request(api_url)
            request.add_header('Accept', 'application/json')
            with opener.open(request, timeout=30) as response:
                if response.status == 200:
                    data = json.loads(response.read().decode('utf-8'))
                    for project in data.get('results', []):
                        if project.get('name') == self.project_name:
                            return project.get('id')
        except Exception as e:
            logger.debug(f"Could not search for project by name: {e}")

        return None

    def create_project_if_not_exists(
        self,
        ssl_verify: bool = True,
        credentials_file: Optional[str] = None,
        project_type: str = "singleCluster",
        ca_cert_path: Optional[str] = None
    ) -> Optional[str]:
        """Create project in Ops Manager if it doesn't exist.

        Args:
            ssl_verify: Whether to verify SSL certificates (default True)
            credentials_file: Path to credentials JSON file to update with new project ID
            project_type: Project type key for updating credentials file
            ca_cert_path: Path to CA certificate for SSL verification

        Returns:
            Project ID if project exists or was created, None on error
        """
        import urllib.request
        import urllib.error
        import ssl

        # First check if project exists by ID
        if self.project_id and self.verify_project_exists(ssl_verify, ca_cert_path):
            return self.project_id

        # Check if project exists by name (to avoid duplicates)
        existing_project_id = self._find_project_by_name(ssl_verify, ca_cert_path)
        if existing_project_id:
            logger.info(f"Found existing project '{self.project_name}' with ID: {existing_project_id}")
            self.project_id = existing_project_id
            if credentials_file:
                self._update_credentials_file(credentials_file, existing_project_id, project_type)
            return existing_project_id

        # Project doesn't exist, create it
        logger.info(f"Creating project '{self.project_name}' in Ops Manager...")

        api_url = f"{self.base_url}/api/public/v1.0/groups"

        # Create password manager for Digest auth (required by Ops Manager API)
        password_mgr = urllib.request.HTTPPasswordMgrWithDefaultRealm()
        password_mgr.add_password(None, api_url, self.public_key, self.private_key)

        # Create handlers
        handlers = [urllib.request.HTTPDigestAuthHandler(password_mgr)]

        # Configure SSL context
        ssl_context = ssl.create_default_context()
        if not ssl_verify:
            ssl_context.check_hostname = False
            ssl_context.verify_mode = ssl.CERT_NONE
        elif ca_cert_path and Path(ca_cert_path).exists():
            ssl_context.load_verify_locations(ca_cert_path)
        handlers.append(urllib.request.HTTPSHandler(context=ssl_context))

        opener = urllib.request.build_opener(*handlers)

        # Project creation payload
        payload = json.dumps({
            "name": self.project_name,
            "orgId": self.org_id
        }).encode('utf-8')

        request = urllib.request.Request(api_url, data=payload, method='POST')
        request.add_header('Content-Type', 'application/json')
        request.add_header('Accept', 'application/json')

        try:
            with opener.open(request, timeout=30) as response:
                if response.status in (200, 201):
                    response_data = json.loads(response.read().decode('utf-8'))
                    new_project_id = response_data.get('id')
                    logger.info(f"Created project '{self.project_name}' with ID: {new_project_id}")
                    self.project_id = new_project_id

                    # Update the credentials file with the new project ID
                    if credentials_file:
                        self._update_credentials_file(
                            credentials_file, new_project_id, project_type
                        )

                    return new_project_id
        except urllib.error.HTTPError as e:
            error_body = e.read().decode('utf-8') if e.fp else ''
            logger.error(f"Failed to create project: {e.code} {e.reason} - {error_body}")
            return None
        except Exception as e:
            logger.error(f"Error creating project: {e}")
            return None

        return None

    def _update_credentials_file(
        self,
        filepath: str,
        project_id: str,
        project_type: str
    ) -> bool:
        """Update the credentials JSON file with a new project ID.

        Args:
            filepath: Path to credentials JSON file
            project_id: New project ID to save
            project_type: Project type key (singleCluster or multiCluster)

        Returns:
            True if file was updated successfully, False otherwise
        """
        try:
            # Read existing data
            with open(filepath, 'r') as f:
                data = json.load(f)

            # Update the appropriate project section
            if 'projects' in data and project_type in data['projects']:
                data['projects'][project_type]['projectId'] = project_id
                logger.info(f"Updated projects.{project_type}.projectId in {filepath}")
            else:
                # Legacy format or missing projects section
                if 'projects' not in data:
                    data['projects'] = {}
                data['projects'][project_type] = {
                    'projectId': project_id,
                    'projectName': self.project_name
                }
                logger.info(f"Created projects.{project_type} section in {filepath}")

            # Also update legacy top-level projectId if this is singleCluster
            if project_type == "singleCluster":
                data['projectId'] = project_id
                data['projectName'] = self.project_name

            # Write back
            with open(filepath, 'w') as f:
                json.dump(data, f, indent=2)

            logger.info(f"Saved new project ID to {filepath}")
            return True

        except Exception as e:
            logger.error(f"Failed to update credentials file: {e}")
            return False


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
