"""
Helm Manager for MongoDB Kubernetes deployment scripts.

Provides Helm binary detection, download, and chart operations.
Follows the same pattern as kind_manager_base.py for binary management.
"""

import logging
import subprocess
import sys
from pathlib import Path
from typing import Dict, List, Optional

from shared.utils import run_command

logger = logging.getLogger(__name__)


class HelmManager:
    """Manages Helm binary and chart operations.

    Provides:
    - Helm binary detection and download
    - Helm repo management
    - Chart install/upgrade/uninstall
    - Template rendering

    Attributes:
        HELM_VERSION: Version of Helm to download if not found
        HELM_DOWNLOAD_URLS: Platform-specific download URLs
    """

    HELM_VERSION = "v3.20.1"
    HELM_DOWNLOAD_BASE = f"https://get.helm.sh/helm-{HELM_VERSION}"
    HELM_DOWNLOAD_URLS = {
        "win32": f"{HELM_DOWNLOAD_BASE}-windows-amd64.zip",
        "linux": f"{HELM_DOWNLOAD_BASE}-linux-amd64.tar.gz",
        "darwin": f"{HELM_DOWNLOAD_BASE}-darwin-amd64.tar.gz",
    }

    def __init__(self, binary_dir: Path):
        """Initialize the Helm manager.

        Args:
            binary_dir: Directory to store Helm binary
        """
        self.binary_dir = binary_dir.resolve()
        self.binary_dir.mkdir(parents=True, exist_ok=True)

        helm_ext = ".exe" if sys.platform == "win32" else ""
        self.local_helm_binary = self.binary_dir / f"helm{helm_ext}"

        self.helm_binary = self._get_helm_binary()

    def _get_helm_binary(self) -> str:
        """Get path to Helm binary, download if necessary."""
        # Check system helm first
        try:
            result = run_command(["helm", "version", "--short"], check=False)
            if result.returncode == 0:
                logger.info(f"Using system helm: {result.stdout.strip()}")
                return "helm"
        except FileNotFoundError:
            pass

        # Check local helm binary
        if self.local_helm_binary.exists():
            logger.info(f"Using local helm binary: {self.local_helm_binary}")
            return str(self.local_helm_binary)

        # Download helm
        logger.info("helm not found, downloading...")
        return self._download_helm()

    def _download_helm(self) -> str:
        """Download Helm binary for current platform with retry logic."""
        import io
        import time
        import urllib.request

        platform = sys.platform
        if platform not in self.HELM_DOWNLOAD_URLS:
            raise RuntimeError(f"Unsupported platform: {platform}")

        url = self.HELM_DOWNLOAD_URLS[platform]
        max_retries = 3
        retry_delay = 5

        for attempt in range(1, max_retries + 1):
            logger.info(f"Downloading helm from: {url} (attempt {attempt}/{max_retries})")
            try:
                archive_path = self.binary_dir / ("helm-archive.zip" if platform == "win32" else "helm-archive.tar.gz")
                urllib.request.urlretrieve(url, str(archive_path))

                # Extract helm binary from archive
                if platform == "win32":
                    import zipfile
                    with zipfile.ZipFile(str(archive_path), 'r') as zf:
                        # helm.exe is inside windows-amd64/helm.exe
                        for name in zf.namelist():
                            if name.endswith("helm.exe"):
                                with zf.open(name) as src, open(str(self.local_helm_binary), 'wb') as dst:
                                    dst.write(src.read())
                                break
                else:
                    import tarfile
                    with tarfile.open(str(archive_path), 'r:gz') as tf:
                        for member in tf.getmembers():
                            if member.name.endswith("/helm"):
                                member.name = "helm"
                                tf.extract(member, path=str(self.binary_dir))
                                break
                    # Make executable
                    import stat
                    self.local_helm_binary.chmod(
                        self.local_helm_binary.stat().st_mode | stat.S_IEXEC
                    )

                # Cleanup archive
                archive_path.unlink(missing_ok=True)

                logger.info(f"helm downloaded to: {self.local_helm_binary}")
                return str(self.local_helm_binary)

            except Exception as e:
                logger.warning(f"Download attempt {attempt} failed: {e}")
                if attempt < max_retries:
                    logger.info(f"Retrying in {retry_delay} seconds...")
                    time.sleep(retry_delay)
                    retry_delay *= 2
                else:
                    raise RuntimeError(f"Failed to download helm after {max_retries} attempts: {e}")

    def _run_helm(
        self,
        args: List[str],
        check: bool = True,
        kubeconfig: Optional[Path] = None,
        timeout: int = 300
    ) -> subprocess.CompletedProcess:
        """Run a helm command.

        Args:
            args: Arguments to pass to helm
            check: If True, raise exception on non-zero exit
            kubeconfig: Optional kubeconfig path
            timeout: Command timeout in seconds
        """
        cmd = [self.helm_binary]
        if kubeconfig:
            cmd.extend(["--kubeconfig", str(kubeconfig)])
        cmd.extend(args)
        return run_command(cmd, check=check, timeout=timeout)

    def repo_add(self, name: str, url: str, kubeconfig: Optional[Path] = None) -> bool:
        """Add a Helm chart repository.

        Args:
            name: Repository name
            url: Repository URL

        Returns:
            True if successful
        """
        logger.info(f"Adding helm repo '{name}' -> {url}")
        try:
            self._run_helm(["repo", "add", name, url, "--force-update"], kubeconfig=kubeconfig)
            self._run_helm(["repo", "update", name], kubeconfig=kubeconfig)
            return True
        except Exception as e:
            logger.error(f"Failed to add helm repo: {e}")
            return False

    def install(
        self,
        release_name: str,
        chart: str,
        namespace: str,
        values: Optional[Dict] = None,
        set_values: Optional[Dict[str, str]] = None,
        kubeconfig: Optional[Path] = None,
        create_namespace: bool = False,
        wait: bool = True,
        timeout: int = 300
    ) -> bool:
        """Install a Helm chart.

        Args:
            release_name: Release name
            chart: Chart reference (e.g., "mongodb/mongodb-kubernetes")
            namespace: Target namespace
            values: Values dict to write to a temp values file
            set_values: Key=value pairs for --set
            kubeconfig: Optional kubeconfig path
            create_namespace: Create namespace if it doesn't exist
            wait: Wait for resources to be ready
            timeout: Wait timeout in seconds

        Returns:
            True if successful
        """
        logger.info(f"Installing helm chart '{chart}' as '{release_name}' in namespace '{namespace}'")

        args = ["install", release_name, chart, "--namespace", namespace]

        if create_namespace:
            args.append("--create-namespace")
        if wait:
            args.extend(["--wait", "--timeout", f"{timeout}s"])

        # Handle values file
        values_file = None
        if values:
            import json
            import yaml as yaml_lib
            values_file = self.binary_dir / f"{release_name}-values.yaml"
            values_file.write_text(
                yaml_lib.dump(values, default_flow_style=False),
                encoding='utf-8'
            )
            args.extend(["-f", str(values_file)])

        # Handle --set values
        if set_values:
            for key, val in set_values.items():
                args.extend(["--set", f"{key}={val}"])

        try:
            self._run_helm(args, kubeconfig=kubeconfig, timeout=timeout + 60)
            logger.info(f"Chart '{release_name}' installed successfully")
            return True
        except Exception as e:
            logger.error(f"Failed to install chart: {e}")
            return False
        finally:
            if values_file and values_file.exists():
                values_file.unlink(missing_ok=True)

    def upgrade(
        self,
        release_name: str,
        chart: str,
        namespace: str,
        values: Optional[Dict] = None,
        set_values: Optional[Dict[str, str]] = None,
        kubeconfig: Optional[Path] = None,
        wait: bool = True,
        timeout: int = 300
    ) -> bool:
        """Upgrade a Helm release.

        Args:
            Same as install()

        Returns:
            True if successful
        """
        logger.info(f"Upgrading helm release '{release_name}'")

        args = ["upgrade", release_name, chart, "--namespace", namespace]

        if wait:
            args.extend(["--wait", "--timeout", f"{timeout}s"])

        values_file = None
        if values:
            import yaml as yaml_lib
            values_file = self.binary_dir / f"{release_name}-values.yaml"
            values_file.write_text(
                yaml_lib.dump(values, default_flow_style=False),
                encoding='utf-8'
            )
            args.extend(["-f", str(values_file)])

        if set_values:
            for key, val in set_values.items():
                args.extend(["--set", f"{key}={val}"])

        try:
            self._run_helm(args, kubeconfig=kubeconfig, timeout=timeout + 60)
            logger.info(f"Release '{release_name}' upgraded successfully")
            return True
        except Exception as e:
            logger.error(f"Failed to upgrade release: {e}")
            return False
        finally:
            if values_file and values_file.exists():
                values_file.unlink(missing_ok=True)

    def uninstall(
        self,
        release_name: str,
        namespace: str,
        kubeconfig: Optional[Path] = None
    ) -> bool:
        """Uninstall a Helm release.

        Args:
            release_name: Release name
            namespace: Namespace
            kubeconfig: Optional kubeconfig path

        Returns:
            True if successful
        """
        logger.info(f"Uninstalling helm release '{release_name}'")
        try:
            self._run_helm(
                ["uninstall", release_name, "--namespace", namespace],
                kubeconfig=kubeconfig
            )
            logger.info(f"Release '{release_name}' uninstalled")
            return True
        except Exception as e:
            logger.error(f"Failed to uninstall release: {e}")
            return False

    def release_exists(
        self,
        release_name: str,
        namespace: str,
        kubeconfig: Optional[Path] = None
    ) -> bool:
        """Check if a Helm release exists.

        Args:
            release_name: Release name
            namespace: Namespace
            kubeconfig: Optional kubeconfig path

        Returns:
            True if release exists
        """
        result = self._run_helm(
            ["status", release_name, "--namespace", namespace],
            check=False,
            kubeconfig=kubeconfig
        )
        return result.returncode == 0
