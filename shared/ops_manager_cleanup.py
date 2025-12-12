"""
Ops Manager Cleanup Module

Provides functionality to clean up Ops Manager projects by:
1. Finding all projects with a given name
2. Resetting automation config (clearing processes/replicaSets)
3. Deleting the projects

This prevents stale TLS certificate hash issues on re-deployment.
"""

import json
import logging
import ssl
import urllib.error
import urllib.request
from base64 import b64encode
from typing import List, Optional

from shared.models import OpsManagerCredentials

logger = logging.getLogger(__name__)


class OpsManagerCleanup:
    """Handles cleanup of Ops Manager projects.

    Cleans up projects by name to prevent stale automation config
    (especially TLS certificate hashes) from causing deployment issues.

    Attributes:
        base_url: Ops Manager base URL
        public_key: API public key for digest authentication
        private_key: API private key for digest authentication
        org_id: Organization ID
        verify_ssl: Whether to verify SSL certificates
    """

    def __init__(
        self,
        credentials: OpsManagerCredentials,
        verify_ssl: bool = True
    ):
        """Initialize OpsManagerCleanup.

        Args:
            credentials: OpsManagerCredentials with API keys and URL
            verify_ssl: Whether to verify SSL certificates (default True)
        """
        self.base_url = credentials.base_url.rstrip('/')
        self.public_key = credentials.public_key
        self.private_key = credentials.private_key
        self.org_id = credentials.org_id
        self.verify_ssl = verify_ssl

        # Set up SSL context
        if not verify_ssl:
            self._ssl_context = ssl.create_default_context()
            self._ssl_context.check_hostname = False
            self._ssl_context.verify_mode = ssl.CERT_NONE
        else:
            self._ssl_context = None

    def _make_request(
        self,
        endpoint: str,
        method: str = "GET",
        data: Optional[dict] = None
    ) -> Optional[dict]:
        """Make authenticated request to Ops Manager API.

        Uses HTTP Digest authentication as required by Ops Manager API.

        Args:
            endpoint: API endpoint (e.g., "/api/public/v1.0/groups")
            method: HTTP method (GET, PUT, DELETE)
            data: Optional JSON data for PUT requests

        Returns:
            Parsed JSON response, or None on error
        """
        url = f"{self.base_url}{endpoint}"

        # Create password manager for digest auth
        password_mgr = urllib.request.HTTPPasswordMgrWithDefaultRealm()
        password_mgr.add_password(None, url, self.public_key, self.private_key)

        # Create handlers
        handlers = [urllib.request.HTTPDigestAuthHandler(password_mgr)]
        if self._ssl_context:
            handlers.append(urllib.request.HTTPSHandler(context=self._ssl_context))

        opener = urllib.request.build_opener(*handlers)

        try:
            if data is not None:
                json_data = json.dumps(data).encode('utf-8')
                request = urllib.request.Request(
                    url,
                    data=json_data,
                    method=method,
                    headers={'Content-Type': 'application/json'}
                )
            else:
                request = urllib.request.Request(url, method=method)

            with opener.open(request, timeout=30) as response:
                if response.status in (200, 201, 204):
                    content = response.read().decode('utf-8')
                    return json.loads(content) if content else {}
                return None

        except urllib.error.HTTPError as e:
            logger.debug(f"HTTP error {e.code} for {method} {endpoint}: {e.reason}")
            return None
        except urllib.error.URLError as e:
            logger.debug(f"URL error for {method} {endpoint}: {e.reason}")
            return None
        except Exception as e:
            logger.debug(f"Request error for {method} {endpoint}: {e}")
            return None

    def list_projects_by_name(self, project_name: str) -> List[dict]:
        """List all projects with a given name in the organization.

        Args:
            project_name: Name of the project to search for

        Returns:
            List of project dicts with 'id' and 'name' keys
        """
        result = self._make_request("/api/public/v1.0/groups")
        if not result or 'results' not in result:
            return []

        return [
            {'id': p.get('id'), 'name': p.get('name')}
            for p in result.get('results', [])
            if p.get('name') == project_name
        ]

    def reset_automation_config(self, project_id: str) -> bool:
        """Reset automation config for a project.

        Clears processes and replicaSets while preserving other config.
        This removes stale TLS certificate references.

        Args:
            project_id: Ops Manager project ID

        Returns:
            True if successful, False otherwise
        """
        # Get current config
        config = self._make_request(f"/api/public/v1.0/groups/{project_id}/automationConfig")
        if config is None:
            logger.warning(f"Could not get automation config for project {project_id}")
            return False

        # Reset deployments
        config['processes'] = []
        config['replicaSets'] = []
        config['sharding'] = []

        # Reset auth
        if 'auth' in config:
            config['auth']['disabled'] = True
            config['auth']['usersWanted'] = []

        # Reset TLS
        if 'tls' in config:
            config['tls'] = {'clientCertificateMode': 'OPTIONAL'}

        # Apply the clean config
        result = self._make_request(
            f"/api/public/v1.0/groups/{project_id}/automationConfig",
            method="PUT",
            data=config
        )

        return result is not None

    def delete_project(self, project_id: str) -> bool:
        """Delete a project.

        Args:
            project_id: Ops Manager project ID

        Returns:
            True if successful, False otherwise
        """
        result = self._make_request(
            f"/api/public/v1.0/groups/{project_id}",
            method="DELETE"
        )
        return result is not None

    def cleanup_projects_by_name(
        self,
        project_name: str,
        delete_projects: bool = True
    ) -> int:
        """Clean up all projects with a given name.

        For each matching project:
        1. Resets automation config (clears processes/replicaSets/TLS)
        2. Optionally deletes the project

        Args:
            project_name: Name of projects to clean up
            delete_projects: Whether to delete projects after reset (default True)

        Returns:
            Number of projects cleaned up
        """
        projects = self.list_projects_by_name(project_name)

        if not projects:
            logger.debug(f"No projects found with name '{project_name}'")
            return 0

        cleaned = 0
        for project in projects:
            project_id = project['id']
            logger.info(f"Cleaning up Ops Manager project: {project_name} ({project_id})")

            # Reset automation config first
            if self.reset_automation_config(project_id):
                logger.debug(f"Reset automation config for project {project_id}")

                if delete_projects:
                    # Wait a moment for config to propagate
                    import time
                    time.sleep(1)

                    if self.delete_project(project_id):
                        logger.info(f"Deleted Ops Manager project: {project_id}")
                        cleaned += 1
                    else:
                        logger.warning(f"Could not delete project {project_id}")
                else:
                    cleaned += 1
            else:
                logger.warning(f"Could not reset automation config for project {project_id}")

        return cleaned


def cleanup_ops_manager_project(
    api_key_file: str,
    project_type: str = "multiCluster",
    verify_ssl: bool = True
) -> bool:
    """Convenience function to clean up Ops Manager projects.

    Loads credentials from file and cleans up all projects matching
    the project name for the specified project type.

    Args:
        api_key_file: Path to API key JSON file
        project_type: "singleCluster" or "multiCluster"
        verify_ssl: Whether to verify SSL certificates

    Returns:
        True if any projects were cleaned up, False otherwise
    """
    try:
        credentials = OpsManagerCredentials.from_file(api_key_file, project_type)
    except FileNotFoundError:
        logger.debug(f"API key file not found: {api_key_file}")
        return False
    except Exception as e:
        logger.debug(f"Could not load credentials: {e}")
        return False

    cleanup = OpsManagerCleanup(credentials, verify_ssl=verify_ssl)
    cleaned = cleanup.cleanup_projects_by_name(credentials.project_name)

    return cleaned > 0
