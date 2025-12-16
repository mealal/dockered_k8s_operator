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
from pathlib import Path
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
        verify_ssl: bool = True,
        ca_cert_path: Optional[str] = None
    ):
        """Initialize OpsManagerCleanup.

        Args:
            credentials: OpsManagerCredentials with API keys and URL
            verify_ssl: Whether to verify SSL certificates (default True)
            ca_cert_path: Path to CA certificate for SSL verification
        """
        self.base_url = credentials.base_url.rstrip('/')
        self.public_key = credentials.public_key
        self.private_key = credentials.private_key
        self.org_id = credentials.org_id
        self.verify_ssl = verify_ssl

        # Set up SSL context
        self._ssl_context = ssl.create_default_context()
        if not verify_ssl:
            self._ssl_context.check_hostname = False
            self._ssl_context.verify_mode = ssl.CERT_NONE
        elif ca_cert_path and Path(ca_cert_path).exists():
            self._ssl_context.load_verify_locations(ca_cert_path)

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
            True if successful or project doesn't exist, False on errors
        """
        url = f"{self.base_url}/api/public/v1.0/groups/{project_id}"

        # Create password manager for digest auth
        password_mgr = urllib.request.HTTPPasswordMgrWithDefaultRealm()
        password_mgr.add_password(None, url, self.public_key, self.private_key)

        # Create handlers
        handlers = [urllib.request.HTTPDigestAuthHandler(password_mgr)]
        if self._ssl_context:
            handlers.append(urllib.request.HTTPSHandler(context=self._ssl_context))

        opener = urllib.request.build_opener(*handlers)

        try:
            request = urllib.request.Request(url, method="DELETE")
            with opener.open(request, timeout=30) as response:
                if response.status in (200, 202, 204):
                    return True
                return False

        except urllib.error.HTTPError as e:
            if e.code == 404:
                # Project already deleted - treat as success
                logger.debug(f"Project {project_id} already deleted (404)")
                return True
            elif e.code == 409:
                # Conflict - project has active processes/hosts
                logger.warning(f"Cannot delete project {project_id}: has active processes")
                return False
            else:
                logger.warning(f"HTTP error {e.code} deleting project {project_id}: {e.reason}")
                return False
        except urllib.error.URLError as e:
            logger.warning(f"URL error deleting project {project_id}: {e.reason}")
            return False
        except Exception as e:
            logger.warning(f"Error deleting project {project_id}: {e}")
            return False

    def project_exists(self, project_id: str) -> bool:
        """Check if a project exists.

        Args:
            project_id: Ops Manager project ID

        Returns:
            True if project exists, False if deleted or error
        """
        result = self._make_request(f"/api/public/v1.0/groups/{project_id}")
        return result is not None

    def cleanup_projects_by_name(
        self,
        project_name: str,
        delete_projects: bool = True
    ) -> int:
        """Clean up all projects with a given name.

        DEPRECATED: This method deletes projects by name, which can accidentally
        affect other projects with the same name. Use cleanup_ops_manager_project()
        with a specific project_id from the credentials file instead.

        For each matching project:
        1. Resets automation config (clears processes/replicaSets/TLS)
        2. Optionally deletes the project

        Args:
            project_name: Name of projects to clean up
            delete_projects: Whether to delete projects after reset (default True)

        Returns:
            Number of projects cleaned up
        """
        import warnings
        warnings.warn(
            "cleanup_projects_by_name() is deprecated and will be removed in a future version. "
            "Use cleanup_ops_manager_project() with project_id instead to avoid accidentally "
            "deleting other projects with the same name.",
            DeprecationWarning,
            stacklevel=2
        )

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
    verify_ssl: bool = True,
    delete_project: bool = True,
    delete_retries: int = None,
    delete_retry_delay: int = None,
    ca_cert_path: Optional[str] = None
) -> bool:
    """Convenience function to clean up Ops Manager project by ID.

    Loads credentials from file and cleans up the specific project by its ID.
    Only affects the project with the exact ID stored in the credentials file.
    Does NOT search by name to avoid accidentally affecting other projects.

    Args:
        api_key_file: Path to API key JSON file
        project_type: "singleCluster" or "multiCluster"
        verify_ssl: Whether to verify SSL certificates
        delete_project: If True (default), delete the project entirely from Ops Manager.
                       If False, only reset automation config.
        delete_retries: Number of times to retry project deletion.
                       Default from constants.OPS_MANAGER_DELETE_RETRIES.
        delete_retry_delay: Seconds to wait between delete retries.
                           Default from constants.OPS_MANAGER_DELETE_RETRY_DELAY.
        ca_cert_path: Path to CA certificate for SSL verification

    Returns:
        True if cleanup was successful, False otherwise
    """
    import time
    from shared.constants import OPS_MANAGER_DELETE_RETRIES, OPS_MANAGER_DELETE_RETRY_DELAY

    # Use defaults from constants if not specified
    if delete_retries is None:
        delete_retries = OPS_MANAGER_DELETE_RETRIES
    if delete_retry_delay is None:
        delete_retry_delay = OPS_MANAGER_DELETE_RETRY_DELAY

    try:
        credentials = OpsManagerCredentials.from_file(api_key_file, project_type)
    except FileNotFoundError:
        logger.info(f"API key file not found: {api_key_file}, skipping Ops Manager cleanup")
        return True  # Not an error if no credentials exist
    except Exception as e:
        logger.warning(f"Could not load credentials from {api_key_file}: {e}")
        return False

    # Only clean up by project ID - never by name to avoid affecting other projects
    if not credentials.project_id:
        logger.warning(f"No project ID found for {project_type} in {api_key_file}, skipping cleanup")
        return False

    cleanup = OpsManagerCleanup(credentials, verify_ssl=verify_ssl, ca_cert_path=ca_cert_path)

    # Check if project exists first
    if not cleanup.project_exists(credentials.project_id):
        logger.info(f"Ops Manager project already deleted: {credentials.project_name} (ID: {credentials.project_id})")
        return True

    logger.info(f"Cleaning up Ops Manager project: {credentials.project_name} (ID: {credentials.project_id})")

    # Reset automation config first to clear processes
    success = cleanup.reset_automation_config(credentials.project_id)
    if not success:
        logger.warning(f"Could not reset automation config for project {credentials.project_id}")
        # Continue anyway - project might be in a state where we can still delete it

    if delete_project:
        # Wait for automation config to propagate and agents to disconnect
        logger.info("Waiting for agents to disconnect from Ops Manager...")
        time.sleep(5)

        # Retry project deletion with backoff
        # Ops Manager may take time to recognize agents are gone after cluster deletion
        for attempt in range(delete_retries):
            if cleanup.delete_project(credentials.project_id):
                # Verify the project is actually deleted
                time.sleep(1)
                if not cleanup.project_exists(credentials.project_id):
                    logger.info(f"Successfully deleted Ops Manager project: {credentials.project_name} (ID: {credentials.project_id})")
                    return True
                else:
                    logger.warning(f"Project deletion reported success but project still exists")

            if attempt < delete_retries - 1:
                wait_time = delete_retry_delay
                logger.info(f"Project deletion attempt {attempt + 1}/{delete_retries} failed, "
                           f"retrying in {wait_time}s (waiting for agents to deregister)...")
                time.sleep(wait_time)

        # Final check - maybe it was deleted
        if not cleanup.project_exists(credentials.project_id):
            logger.info(f"Ops Manager project successfully deleted: {credentials.project_name}")
            return True

        logger.error(f"FAILED to delete Ops Manager project {credentials.project_id} after {delete_retries} attempts. "
                    f"The project may still have active agents. Please delete it manually from the Ops Manager UI.")
        return False

    return success


def cleanup_stale_projects_by_name(
    api_key_file: str,
    project_name: str,
    verify_ssl: bool = True,
    ca_cert_path: Optional[str] = None
) -> int:
    """Clean up any stale projects matching a given name.

    This is used to clean up projects created by the operator when it couldn't
    find the project by ID (e.g., after a failed deployment that left a stale project).

    The operator creates projects with a tag like "MONGODB-RS" matching the
    replica set name, so this function helps clean those up.

    Args:
        api_key_file: Path to API key JSON file
        project_name: Name of projects to clean up (typically the replica set name)
        verify_ssl: Whether to verify SSL certificates
        ca_cert_path: Path to CA certificate for SSL verification

    Returns:
        Number of projects cleaned up
    """
    import time

    try:
        credentials = OpsManagerCredentials.from_file(api_key_file, "singleCluster")
    except FileNotFoundError:
        logger.debug(f"API key file not found: {api_key_file}")
        return 0
    except Exception as e:
        logger.debug(f"Could not load credentials: {e}")
        return 0

    cleanup = OpsManagerCleanup(credentials, verify_ssl=verify_ssl, ca_cert_path=ca_cert_path)
    projects = cleanup.list_projects_by_name(project_name)

    if not projects:
        return 0

    cleaned = 0
    for project in projects:
        project_id = project['id']
        logger.info(f"Cleaning up stale project: {project_name} (ID: {project_id})")

        if cleanup.reset_automation_config(project_id):
            time.sleep(2)
            if cleanup.delete_project(project_id):
                logger.info(f"Deleted stale project: {project_id}")
                cleaned += 1
            else:
                logger.warning(f"Could not delete stale project {project_id}")
        else:
            logger.warning(f"Could not reset automation config for stale project {project_id}")

    return cleaned


def delete_all_projects_by_name(
    api_key_file: str,
    project_name: str,
    verify_ssl: bool = True
) -> int:
    """Delete ALL projects with a given name from Ops Manager.

    DEPRECATED: This function deletes projects by name, which can accidentally
    affect other projects with the same name. Use cleanup_ops_manager_project()
    with delete_project=True instead.

    Args:
        api_key_file: Path to API key JSON file
        project_name: Name of projects to delete
        verify_ssl: Whether to verify SSL certificates

    Returns:
        Number of projects deleted
    """
    import warnings
    warnings.warn(
        "delete_all_projects_by_name() is deprecated and will be removed in a future version. "
        "Use cleanup_ops_manager_project() with delete_project=True instead to avoid "
        "accidentally deleting other projects with the same name.",
        DeprecationWarning,
        stacklevel=2
    )

    try:
        credentials = OpsManagerCredentials.from_file(api_key_file)
    except FileNotFoundError:
        logger.error(f"API key file not found: {api_key_file}")
        return 0
    except Exception as e:
        logger.error(f"Could not load credentials: {e}")
        return 0

    cleanup = OpsManagerCleanup(credentials, verify_ssl=verify_ssl)
    return cleanup.cleanup_projects_by_name(project_name, delete_projects=True)
