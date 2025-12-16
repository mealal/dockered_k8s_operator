"""
Consolidated cleanup utilities for MongoDB Kubernetes deployments.

Provides centralized cleanup functions for:
- Generated YAML files
- TLS certificates (MongoDB-specific, not CA)
- Kubeconfig files
- Docker containers and networks
- Kind clusters
- Ops Manager projects

Usage:
    from shared.cleanup import (
        CleanupManager,
        cleanup_generated_files,
        cleanup_kubernetes_deployment,
    )

    # Quick cleanup for single-cluster
    cleanup_generated_files(script_dir, multi_cluster=False)

    # Full cleanup with manager
    manager = CleanupManager(script_dir)
    manager.cleanup_all(multi_cluster=False)
"""

import logging
import shutil
from pathlib import Path
from typing import Optional, List, Callable

from shared.constants import (
    CERTS_DIR,
    MONGODB_CERTS_SUBDIR,
    MONGODB_MULTI_CERTS_SUBDIR,
    SINGLE_CLUSTER_KUBE_DIR,
    MULTI_CLUSTER_KUBE_DIR,
    SINGLE_CLUSTER_TEMPLATES,
    MULTI_CLUSTER_TEMPLATES,
    DEFAULT_CREDENTIALS_FILE,
)
from shared.exceptions import CleanupError

logger = logging.getLogger(__name__)


class CleanupManager:
    """Manages cleanup operations for deployment artifacts.

    Consolidates cleanup logic for generated files, certificates,
    kubeconfig files, and provides hooks for custom cleanup operations.

    Attributes:
        script_dir: Base directory of the deployment scripts
        dry_run: If True, log what would be deleted without deleting
    """

    def __init__(self, script_dir: Path, dry_run: bool = False):
        """Initialize cleanup manager.

        Args:
            script_dir: Base directory of the deployment scripts
            dry_run: If True, only log what would be deleted
        """
        self.script_dir = Path(script_dir).resolve()
        self.dry_run = dry_run
        self._cleanup_hooks: List[Callable[[], None]] = []

    def add_cleanup_hook(self, hook: Callable[[], None]) -> None:
        """Add a custom cleanup hook to be executed during cleanup.

        Args:
            hook: Callable that performs cleanup, takes no arguments
        """
        self._cleanup_hooks.append(hook)

    def _delete_file(self, path: Path) -> bool:
        """Delete a single file.

        Args:
            path: Path to file to delete

        Returns:
            True if deleted, False otherwise
        """
        if not path.exists():
            return False

        if self.dry_run:
            logger.info(f"[DRY-RUN] Would delete file: {path}")
            return True

        try:
            path.unlink()
            logger.debug(f"Deleted file: {path}")
            return True
        except Exception as e:
            logger.warning(f"Failed to delete file {path}: {e}")
            return False

    def _delete_directory(self, path: Path, recursive: bool = True) -> bool:
        """Delete a directory.

        Args:
            path: Path to directory to delete
            recursive: If True, delete contents recursively

        Returns:
            True if deleted, False otherwise
        """
        if not path.exists():
            return False

        if self.dry_run:
            logger.info(f"[DRY-RUN] Would delete directory: {path}")
            return True

        try:
            if recursive:
                shutil.rmtree(path)
            else:
                path.rmdir()
            logger.debug(f"Deleted directory: {path}")
            return True
        except Exception as e:
            logger.warning(f"Failed to delete directory {path}: {e}")
            return False

    def _delete_directory_contents(self, path: Path) -> int:
        """Delete contents of a directory without deleting the directory itself.

        Args:
            path: Path to directory

        Returns:
            Number of items deleted
        """
        if not path.exists():
            return 0

        deleted = 0
        for item in path.iterdir():
            if item.is_file():
                if self._delete_file(item):
                    deleted += 1
            elif item.is_dir():
                if self._delete_directory(item):
                    deleted += 1

        return deleted

    def cleanup_generated_yaml(self, multi_cluster: bool = False) -> int:
        """Clean up generated YAML files.

        Args:
            multi_cluster: If True, clean multi-cluster templates

        Returns:
            Number of items deleted
        """
        template_subdir = MULTI_CLUSTER_TEMPLATES if multi_cluster else SINGLE_CLUSTER_TEMPLATES
        generated_dir = self.script_dir / template_subdir / "generated"

        if not generated_dir.exists():
            logger.debug(f"Generated YAML directory does not exist: {generated_dir}")
            return 0

        deleted = self._delete_directory_contents(generated_dir)
        if deleted > 0:
            logger.info(f"Cleaned up {deleted} generated YAML files in: {generated_dir}")
        return deleted

    def cleanup_mongodb_certs(self, multi_cluster: bool = False) -> bool:
        """Clean up MongoDB TLS certificates (NOT the CA certificate).

        Args:
            multi_cluster: If True, clean multi-cluster certs

        Returns:
            True if cleaned up, False otherwise
        """
        cert_subdir = MONGODB_MULTI_CERTS_SUBDIR if multi_cluster else MONGODB_CERTS_SUBDIR
        mongodb_certs_dir = self.script_dir / CERTS_DIR / cert_subdir

        if not mongodb_certs_dir.exists():
            logger.debug(f"MongoDB certs directory does not exist: {mongodb_certs_dir}")
            return False

        result = self._delete_directory(mongodb_certs_dir)
        if result:
            logger.info(f"Cleaned up MongoDB TLS certs: {mongodb_certs_dir}")
        return result

    def cleanup_kubeconfig(self, multi_cluster: bool = False) -> int:
        """Clean up kubeconfig files.

        Args:
            multi_cluster: If True, clean multi-cluster kubeconfig

        Returns:
            Number of items deleted
        """
        kube_subdir = MULTI_CLUSTER_KUBE_DIR if multi_cluster else SINGLE_CLUSTER_KUBE_DIR
        kubeconfig_dir = self.script_dir / kube_subdir

        if not kubeconfig_dir.exists():
            logger.debug(f"Kubeconfig directory does not exist: {kubeconfig_dir}")
            return 0

        deleted = self._delete_directory_contents(kubeconfig_dir)
        if deleted > 0:
            logger.info(f"Cleaned up {deleted} kubeconfig files in: {kubeconfig_dir}")
        return deleted

    def cleanup_all_certs(self) -> int:
        """Clean up ALL certificates including CA (use with caution).

        This removes the entire certs/ directory including:
        - CA certificate and key (needed for Ops Manager)
        - MongoDB single-cluster certs
        - MongoDB multi-cluster certs

        Returns:
            Number of items deleted
        """
        certs_dir = self.script_dir / CERTS_DIR

        if not certs_dir.exists():
            return 0

        deleted = self._delete_directory_contents(certs_dir)
        if deleted > 0:
            logger.info(f"Cleaned up {deleted} certificate files/directories in: {certs_dir}")
        return deleted

    def cleanup_credentials_file(self) -> bool:
        """Clean up the API credentials JSON file.

        Returns:
            True if deleted, False otherwise
        """
        credentials_file = self.script_dir / DEFAULT_CREDENTIALS_FILE

        if not credentials_file.exists():
            return False

        result = self._delete_file(credentials_file)
        if result:
            logger.info(f"Deleted API credentials file: {credentials_file}")
        return result

    def cleanup_deployment(self, multi_cluster: bool = False) -> dict:
        """Clean up all deployment artifacts for a cluster type.

        Cleans up:
        - Generated YAML files
        - MongoDB TLS certificates
        - Kubeconfig files

        Does NOT clean up:
        - CA certificate (shared between deployments)
        - API credentials file (shared between deployments)

        Args:
            multi_cluster: If True, clean multi-cluster artifacts

        Returns:
            Dict with counts of deleted items by category
        """
        results = {
            "yaml_files": self.cleanup_generated_yaml(multi_cluster),
            "mongodb_certs": 1 if self.cleanup_mongodb_certs(multi_cluster) else 0,
            "kubeconfig_files": self.cleanup_kubeconfig(multi_cluster),
        }

        # Run custom hooks
        for hook in self._cleanup_hooks:
            try:
                hook()
            except Exception as e:
                logger.warning(f"Cleanup hook failed: {e}")

        total = sum(results.values())
        cluster_type = "multi-cluster" if multi_cluster else "single-cluster"
        logger.info(f"Cleanup complete for {cluster_type}: {total} items removed")

        return results

    def cleanup_all(self) -> dict:
        """Clean up ALL deployment artifacts (both single and multi-cluster).

        Returns:
            Dict with cleanup results for each cluster type
        """
        return {
            "single_cluster": self.cleanup_deployment(multi_cluster=False),
            "multi_cluster": self.cleanup_deployment(multi_cluster=True),
        }


def cleanup_generated_files(
    script_dir: Path,
    multi_cluster: bool = False,
    kubeconfig_dir: Optional[str] = None,
    dry_run: bool = False
) -> None:
    """Convenience function to clean up generated deployment files.

    This is a drop-in replacement for the cleanup_generated_files()
    functions in deploy_mongodb_k8s.py and deploy_mongodb_k8s_multi.py.

    Args:
        script_dir: Base directory of the deployment scripts
        multi_cluster: If True, clean multi-cluster artifacts
        kubeconfig_dir: Override kubeconfig directory (deprecated, ignored)
        dry_run: If True, only log what would be deleted
    """
    if kubeconfig_dir:
        logger.debug("kubeconfig_dir parameter is deprecated and ignored")

    manager = CleanupManager(script_dir, dry_run=dry_run)
    manager.cleanup_deployment(multi_cluster=multi_cluster)


def cleanup_kind_cluster(
    kind_path: str,
    cluster_name: str,
    dry_run: bool = False
) -> bool:
    """Clean up a Kind cluster.

    Args:
        kind_path: Path to kind executable
        cluster_name: Name of the cluster to delete
        dry_run: If True, only log what would be done

    Returns:
        True if successful, False otherwise
    """
    import subprocess

    if dry_run:
        logger.info(f"[DRY-RUN] Would delete kind cluster: {cluster_name}")
        return True

    try:
        result = subprocess.run(
            [kind_path, "delete", "cluster", "--name", cluster_name],
            capture_output=True,
            text=True,
            timeout=120
        )
        if result.returncode == 0:
            logger.info(f"Deleted kind cluster: {cluster_name}")
            return True
        else:
            logger.warning(f"Failed to delete kind cluster {cluster_name}: {result.stderr}")
            return False
    except subprocess.TimeoutExpired:
        logger.error(f"Timeout deleting kind cluster: {cluster_name}")
        return False
    except Exception as e:
        logger.error(f"Error deleting kind cluster {cluster_name}: {e}")
        return False


def cleanup_docker_containers(
    container_names: List[str],
    dry_run: bool = False
) -> int:
    """Clean up Docker containers by name.

    Args:
        container_names: List of container names to remove
        dry_run: If True, only log what would be done

    Returns:
        Number of containers removed
    """
    import subprocess

    removed = 0
    for name in container_names:
        if dry_run:
            logger.info(f"[DRY-RUN] Would remove container: {name}")
            removed += 1
            continue

        try:
            result = subprocess.run(
                ["docker", "rm", "-f", name],
                capture_output=True,
                text=True,
                timeout=30
            )
            if result.returncode == 0:
                logger.debug(f"Removed container: {name}")
                removed += 1
        except Exception as e:
            logger.warning(f"Failed to remove container {name}: {e}")

    if removed > 0:
        logger.info(f"Removed {removed} Docker container(s)")
    return removed


def cleanup_docker_network(
    network_name: str,
    dry_run: bool = False
) -> bool:
    """Clean up a Docker network.

    Args:
        network_name: Name of the network to remove
        dry_run: If True, only log what would be done

    Returns:
        True if successful, False otherwise
    """
    import subprocess

    if dry_run:
        logger.info(f"[DRY-RUN] Would remove network: {network_name}")
        return True

    try:
        result = subprocess.run(
            ["docker", "network", "rm", network_name],
            capture_output=True,
            text=True,
            timeout=30
        )
        if result.returncode == 0:
            logger.info(f"Removed Docker network: {network_name}")
            return True
        else:
            logger.debug(f"Could not remove network {network_name}: {result.stderr}")
            return False
    except Exception as e:
        logger.warning(f"Failed to remove network {network_name}: {e}")
        return False


def delete_mongodb_crd(
    kubeconfig: Path,
    crd_name: str,
    namespace: str,
    crd_type: str = "mongodb",
    timeout: int = None,
    dry_run: bool = False
) -> bool:
    """Delete a MongoDB CRD resource and wait for it to be removed.

    This should be called BEFORE deleting the Kubernetes cluster to allow
    the operator to properly deregister from Ops Manager.

    Args:
        kubeconfig: Path to kubeconfig file
        crd_name: Name of the MongoDB resource (e.g., "mongodb-rs")
        namespace: Kubernetes namespace
        crd_type: CRD type: "mongodb" or "mongodbmulticluster"
        timeout: Seconds to wait for deletion to complete
        dry_run: If True, only log what would be done

    Returns:
        True if deleted (or doesn't exist), False on error
    """
    import subprocess
    import time
    from shared.constants import CRD_DELETION_TIMEOUT, CRD_POLL_INTERVAL

    # Use default timeout from constants if not specified
    if timeout is None:
        timeout = CRD_DELETION_TIMEOUT

    if dry_run:
        logger.info(f"[DRY-RUN] Would delete {crd_type}/{crd_name} in namespace {namespace}")
        return True

    kubeconfig_str = str(kubeconfig)

    # First check if the resource exists
    try:
        check_result = subprocess.run(
            ["kubectl", "--kubeconfig", kubeconfig_str,
             "get", crd_type, crd_name, "-n", namespace],
            capture_output=True, text=True, timeout=30
        )
        if check_result.returncode != 0:
            # Resource doesn't exist - that's fine
            logger.info(f"{crd_type}/{crd_name} not found in namespace {namespace} - nothing to delete")
            return True
    except subprocess.TimeoutExpired:
        logger.warning(f"Timeout checking if {crd_type}/{crd_name} exists")
        return False
    except Exception as e:
        logger.warning(f"Error checking {crd_type}/{crd_name}: {e}")
        return False

    # Delete the resource
    logger.info(f"Deleting {crd_type}/{crd_name} from namespace {namespace}...")
    try:
        delete_result = subprocess.run(
            ["kubectl", "--kubeconfig", kubeconfig_str,
             "delete", crd_type, crd_name, "-n", namespace, "--wait=false"],
            capture_output=True, text=True, timeout=60
        )
        if delete_result.returncode != 0:
            logger.warning(f"Failed to delete {crd_type}/{crd_name}: {delete_result.stderr}")
            # Continue anyway - might already be deleted
    except subprocess.TimeoutExpired:
        logger.warning(f"Timeout deleting {crd_type}/{crd_name}")
    except Exception as e:
        logger.warning(f"Error deleting {crd_type}/{crd_name}: {e}")

    # Wait for deletion to complete (operator needs to deregister from Ops Manager)
    logger.info(f"Waiting for operator to clean up {crd_type}/{crd_name} (up to {timeout}s)...")
    start_time = time.time()
    poll_interval = CRD_POLL_INTERVAL

    while time.time() - start_time < timeout:
        try:
            check_result = subprocess.run(
                ["kubectl", "--kubeconfig", kubeconfig_str,
                 "get", crd_type, crd_name, "-n", namespace],
                capture_output=True, text=True, timeout=30
            )
            if check_result.returncode != 0:
                # Resource is gone
                elapsed = int(time.time() - start_time)
                logger.info(f"{crd_type}/{crd_name} deleted successfully (took {elapsed}s)")
                return True

            # Log progress
            elapsed = int(time.time() - start_time)
            logger.debug(f"[{elapsed}s] {crd_type}/{crd_name} still exists, waiting...")
            time.sleep(poll_interval)

        except subprocess.TimeoutExpired:
            logger.debug("kubectl get timed out, continuing to wait...")
            time.sleep(poll_interval)
        except Exception as e:
            logger.debug(f"Error checking status: {e}")
            time.sleep(poll_interval)

    # Timeout - try to force delete
    logger.warning(f"Timeout waiting for {crd_type}/{crd_name} deletion, attempting force delete...")
    try:
        subprocess.run(
            ["kubectl", "--kubeconfig", kubeconfig_str,
             "delete", crd_type, crd_name, "-n", namespace,
             "--grace-period=0", "--force"],
            capture_output=True, text=True, timeout=60
        )
    except Exception:
        pass

    # Final check
    try:
        check_result = subprocess.run(
            ["kubectl", "--kubeconfig", kubeconfig_str,
             "get", crd_type, crd_name, "-n", namespace],
            capture_output=True, text=True, timeout=30
        )
        if check_result.returncode != 0:
            logger.info(f"{crd_type}/{crd_name} deleted after force delete")
            return True
    except Exception:
        pass

    logger.error(f"Could not delete {crd_type}/{crd_name} after {timeout}s")
    return False


def wait_for_pods_deleted(
    kubeconfig: Path,
    namespace: str,
    label_selector: str,
    timeout: int = None,
    dry_run: bool = False
) -> bool:
    """Wait for pods matching a label selector to be deleted.

    Args:
        kubeconfig: Path to kubeconfig file
        namespace: Kubernetes namespace
        label_selector: Label selector (e.g., "app=mongodb-rs")
        timeout: Seconds to wait (default: POD_DELETION_TIMEOUT from constants)
        dry_run: If True, only log what would be done

    Returns:
        True if all pods are gone, False on timeout
    """
    import subprocess
    import time
    from shared.constants import POD_DELETION_TIMEOUT, POD_POLL_INTERVAL

    if timeout is None:
        timeout = POD_DELETION_TIMEOUT

    if dry_run:
        logger.info(f"[DRY-RUN] Would wait for pods with label {label_selector} to be deleted")
        return True

    kubeconfig_str = str(kubeconfig)
    start_time = time.time()
    poll_interval = POD_POLL_INTERVAL

    logger.info(f"Waiting for pods with label '{label_selector}' to be deleted (up to {timeout}s)...")

    while time.time() - start_time < timeout:
        try:
            result = subprocess.run(
                ["kubectl", "--kubeconfig", kubeconfig_str,
                 "get", "pods", "-n", namespace, "-l", label_selector,
                 "-o", "jsonpath={.items[*].metadata.name}"],
                capture_output=True, text=True, timeout=30
            )
            pods = result.stdout.strip()
            if not pods:
                elapsed = int(time.time() - start_time)
                logger.info(f"All pods deleted (took {elapsed}s)")
                return True

            elapsed = int(time.time() - start_time)
            logger.debug(f"[{elapsed}s] Pods still exist: {pods}")
            time.sleep(poll_interval)

        except subprocess.TimeoutExpired:
            time.sleep(poll_interval)
        except Exception as e:
            logger.debug(f"Error checking pods: {e}")
            time.sleep(poll_interval)

    logger.warning(f"Timeout waiting for pods to be deleted after {timeout}s")
    return False


def cleanup_kubernetes_deployment(
    script_dir: Path,
    multi_cluster: bool = False,
    kind_path: Optional[str] = None,
    cluster_names: Optional[List[str]] = None,
    cleanup_ops_manager_project: bool = True,
    verify_ssl: bool = True,
    dry_run: bool = False
) -> dict:
    """Full cleanup of a Kubernetes MongoDB deployment.

    Performs comprehensive cleanup including:
    1. Deletes Kind cluster(s)
    2. Cleans up generated YAML files
    3. Removes MongoDB TLS certificates
    4. Cleans up kubeconfig files
    5. Optionally cleans up Ops Manager project

    Args:
        script_dir: Base directory of the deployment scripts
        multi_cluster: If True, clean multi-cluster deployment
        kind_path: Path to kind executable (auto-detected if None)
        cluster_names: List of cluster names to delete
        cleanup_ops_manager_project: If True, also clean up Ops Manager project
        verify_ssl: Whether to verify SSL for Ops Manager API
        dry_run: If True, only log what would be done

    Returns:
        Dict with cleanup results
    """
    from shared.ops_manager_cleanup import cleanup_ops_manager_project as cleanup_om

    results = {
        "clusters_deleted": 0,
        "files_cleaned": {},
        "ops_manager_cleaned": False,
    }

    # Delete Kind cluster(s)
    if kind_path and cluster_names:
        for name in cluster_names:
            if cleanup_kind_cluster(kind_path, name, dry_run):
                results["clusters_deleted"] += 1

    # Clean up files
    manager = CleanupManager(script_dir, dry_run=dry_run)
    results["files_cleaned"] = manager.cleanup_deployment(multi_cluster)

    # Clean up Ops Manager project
    if cleanup_ops_manager_project:
        credentials_file = script_dir / DEFAULT_CREDENTIALS_FILE
        if credentials_file.exists():
            project_type = "multiCluster" if multi_cluster else "singleCluster"
            if dry_run:
                logger.info(f"[DRY-RUN] Would clean up Ops Manager project ({project_type})")
                results["ops_manager_cleaned"] = True
            else:
                ca_cert_path = str(script_dir / CERTS_DIR / "ca.crt")
                results["ops_manager_cleaned"] = cleanup_om(
                    str(credentials_file),
                    project_type=project_type,
                    verify_ssl=verify_ssl,
                    ca_cert_path=ca_cert_path
                )

    return results
