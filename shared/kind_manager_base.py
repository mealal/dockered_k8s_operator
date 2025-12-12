"""
Base Kind Manager for MongoDB Kubernetes deployment scripts.

Contains the common functionality shared between single-cluster and multi-cluster
Kind managers, including binary detection, download, and cluster operations.
"""

import logging
import subprocess
import sys
from pathlib import Path
from typing import List, Optional

from shared.utils import run_command

logger = logging.getLogger(__name__)


class BaseKindManager:
    """Base class for Kind cluster managers.

    Provides common functionality for managing Kind clusters including:
    - Kind binary detection and download
    - Running kind commands
    - Cluster existence checking
    - Cluster deletion

    Subclasses should implement cluster creation with their specific configurations.

    Attributes:
        KIND_VERSION: Version of kind to download if not found
        KIND_DOWNLOAD_URLS: Platform-specific download URLs
    """

    KIND_VERSION = "v0.20.0"
    KIND_DOWNLOAD_URLS = {
        "win32": "https://kind.sigs.k8s.io/dl/v0.20.0/kind-windows-amd64",
        "linux": "https://kind.sigs.k8s.io/dl/v0.20.0/kind-linux-amd64",
        "darwin": "https://kind.sigs.k8s.io/dl/v0.20.0/kind-darwin-amd64",
    }

    def __init__(self, kubeconfig_dir: Path):
        """Initialize the Kind manager.

        Args:
            kubeconfig_dir: Directory to store kubeconfig and kind binary
        """
        self.kubeconfig_path = kubeconfig_dir.resolve()
        self.kubeconfig_path.mkdir(parents=True, exist_ok=True)

        # Local kind binary path
        kind_ext = ".exe" if sys.platform == "win32" else ""
        self.local_kind_binary = self.kubeconfig_path / f"kind{kind_ext}"

        # Get kind binary (system or download)
        self.kind_binary = self._get_kind_binary()

    def _get_kind_binary(self) -> str:
        """Get path to kind binary, download if necessary.

        Checks for system kind first, then local binary, then downloads.

        Returns:
            Path to kind binary
        """
        # Check native kind first
        try:
            result = run_command(["kind", "version"], check=False)
            if result.returncode == 0:
                logger.info("Using system kind")
                return "kind"
        except FileNotFoundError:
            pass

        # Check local kind binary
        if self.local_kind_binary.exists():
            logger.info(f"Using local kind binary: {self.local_kind_binary}")
            return str(self.local_kind_binary)

        # Download kind
        logger.info("kind not found, downloading...")
        return self._download_kind()

    def _download_kind(self) -> str:
        """Download kind binary for current platform with retry logic.

        Returns:
            Path to downloaded kind binary

        Raises:
            RuntimeError: If download fails after retries
        """
        import time
        import urllib.request

        platform = sys.platform
        if platform not in self.KIND_DOWNLOAD_URLS:
            raise RuntimeError(f"Unsupported platform: {platform}")

        url = self.KIND_DOWNLOAD_URLS[platform]
        max_retries = 3
        retry_delay = 5

        for attempt in range(1, max_retries + 1):
            logger.info(f"Downloading kind from: {url} (attempt {attempt}/{max_retries})")
            try:
                urllib.request.urlretrieve(url, str(self.local_kind_binary))

                # Make executable on Unix
                if sys.platform != "win32":
                    import stat
                    self.local_kind_binary.chmod(
                        self.local_kind_binary.stat().st_mode | stat.S_IEXEC
                    )

                logger.info(f"kind downloaded to: {self.local_kind_binary}")
                return str(self.local_kind_binary)

            except Exception as e:
                logger.warning(f"Download attempt {attempt} failed: {e}")
                if attempt < max_retries:
                    logger.info(f"Retrying in {retry_delay} seconds...")
                    time.sleep(retry_delay)
                    retry_delay *= 2  # Exponential backoff
                else:
                    raise RuntimeError(f"Failed to download kind after {max_retries} attempts: {e}")

    def _run_kind(self, args: List[str], check: bool = True) -> subprocess.CompletedProcess:
        """Run kind command.

        Args:
            args: Arguments to pass to kind
            check: If True, raise exception on non-zero exit

        Returns:
            CompletedProcess instance
        """
        return run_command([self.kind_binary] + args, check=check, timeout=300)

    def cluster_exists(self, cluster_name: str) -> bool:
        """Check if a cluster already exists.

        Args:
            cluster_name: Name of the cluster to check

        Returns:
            True if cluster exists, False otherwise
        """
        try:
            result = self._run_kind(["get", "clusters"], check=False)
            return cluster_name in result.stdout.split('\n')
        except (subprocess.TimeoutExpired, subprocess.CalledProcessError, FileNotFoundError):
            return False

    def export_kubeconfig(self, cluster_name: str, kubeconfig_file: Path) -> None:
        """Export kubeconfig for the cluster.

        Args:
            cluster_name: Name of the cluster
            kubeconfig_file: Path to write the kubeconfig
        """
        try:
            result = self._run_kind(["get", "kubeconfig", "--name", cluster_name])
            kubeconfig_file.write_text(result.stdout)
            logger.info(f"Kubeconfig exported to: {kubeconfig_file}")
        except Exception as e:
            logger.warning(f"Could not export kubeconfig: {e}")

    def delete_cluster(self, cluster_name: str) -> bool:
        """Delete a kind cluster.

        Args:
            cluster_name: Name of the cluster to delete

        Returns:
            True if deletion succeeded, False otherwise
        """
        logger.info(f"Deleting kind cluster: {cluster_name}")
        try:
            self._run_kind(["delete", "cluster", "--name", cluster_name])
            logger.info(f"Cluster {cluster_name} deleted")
            return True
        except Exception as e:
            logger.error(f"Failed to delete cluster: {e}")
            return False

    def create_cluster_with_config(
        self,
        cluster_name: str,
        config_content: str,
        kubeconfig_file: Path
    ) -> bool:
        """Create a kind cluster with the provided configuration.

        Args:
            cluster_name: Name for the cluster
            config_content: Kind cluster configuration YAML content
            kubeconfig_file: Path to write the kubeconfig

        Returns:
            True if cluster creation succeeded, False otherwise
        """
        logger.info(f"Creating kind cluster: {cluster_name}")

        if self.cluster_exists(cluster_name):
            logger.info(f"Cluster {cluster_name} already exists")
            self.export_kubeconfig(cluster_name, kubeconfig_file)
            return True

        # Write config file
        config_file = self.kubeconfig_path / f"{cluster_name}-kind-config.yaml"
        config_file.write_text(config_content)
        logger.info(f"Created kind config: {config_file}")

        try:
            self._run_kind([
                "create", "cluster",
                "--name", cluster_name,
                "--config", str(config_file),
                "--wait", "120s"
            ])

            logger.info(f"Cluster {cluster_name} created successfully")
            self.export_kubeconfig(cluster_name, kubeconfig_file)
            return True

        except subprocess.TimeoutExpired:
            logger.error("Cluster creation timed out")
            return False
        except subprocess.CalledProcessError as e:
            logger.error(f"Failed to create cluster: {e.stderr if e.stderr else e}")
            return False
        except FileNotFoundError:
            logger.error("kind binary not found - ensure Docker is running")
            return False
