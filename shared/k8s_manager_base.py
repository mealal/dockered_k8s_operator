"""
Base Kubernetes Manager for MongoDB Kubernetes deployment scripts.

Contains the common functionality shared between single-cluster and multi-cluster
Kubernetes managers, including kubectl execution and resource management.
"""

import logging
import subprocess
import time
from pathlib import Path
from typing import List, Optional

from shared.utils import run_command, convert_path_for_docker, validate_yaml

logger = logging.getLogger(__name__)

# Default kubectl image for Docker-based execution
DEFAULT_KUBECTL_IMAGE = "bitnami/kubectl:1.28"


class BaseKubernetesManager:
    """Base class for Kubernetes managers.

    Provides common functionality for managing Kubernetes resources including:
    - kubectl execution (native or via Docker)
    - Namespace creation
    - YAML application
    - Deployment and pod waiting

    Attributes:
        kubectl_image: Docker image for kubectl if native not available
    """

    def __init__(self, kubectl_image: str = DEFAULT_KUBECTL_IMAGE):
        """Initialize the Kubernetes manager.

        Args:
            kubectl_image: Docker image to use for kubectl if native not available
        """
        self.kubectl_image = kubectl_image

        # Check if native kubectl is available
        try:
            result = run_command(["kubectl", "version", "--client"], check=False)
            self._use_native = result.returncode == 0
        except FileNotFoundError:
            self._use_native = False

        if self._use_native:
            logger.info("Using native kubectl")
        else:
            logger.info(f"Using kubectl via Docker: {kubectl_image}")

    def _run_kubectl_native(
        self,
        args: List[str],
        kubeconfig: Path,
        check: bool = True,
        input_data: Optional[str] = None,
        timeout: int = 120
    ) -> subprocess.CompletedProcess:
        """Run kubectl command natively.

        Args:
            args: Arguments to pass to kubectl
            kubeconfig: Path to kubeconfig file
            check: If True, raise exception on non-zero exit
            input_data: Optional input to send to stdin
            timeout: Command timeout in seconds

        Returns:
            CompletedProcess instance
        """
        cmd = ["kubectl", "--kubeconfig", str(kubeconfig)] + args
        return run_command(cmd, check=check, input_data=input_data, timeout=timeout)

    def _run_kubectl_docker(
        self,
        args: List[str],
        kubeconfig: Path,
        check: bool = True,
        input_data: Optional[str] = None,
        timeout: int = 120
    ) -> subprocess.CompletedProcess:
        """Run kubectl via Docker container.

        Args:
            args: Arguments to pass to kubectl
            kubeconfig: Path to kubeconfig file (or directory containing kubeconfig)
            check: If True, raise exception on non-zero exit
            input_data: Optional input to send to stdin
            timeout: Command timeout in seconds

        Returns:
            CompletedProcess instance
        """
        # Mount the kubeconfig directory
        kubeconfig_mount = convert_path_for_docker(kubeconfig.parent)

        cmd = [
            "docker", "run", "--rm", "-i",
            "-v", f"{kubeconfig_mount}:/root/.kube:ro",
            "--network", "host",
            self.kubectl_image
        ] + args

        return run_command(cmd, check=check, input_data=input_data, timeout=timeout)

    def run_kubectl(
        self,
        args: List[str],
        kubeconfig: Path,
        check: bool = True,
        input_data: Optional[str] = None,
        timeout: int = 120
    ) -> subprocess.CompletedProcess:
        """Run kubectl command - native or via Docker.

        Args:
            args: Arguments to pass to kubectl
            kubeconfig: Path to kubeconfig file
            check: If True, raise exception on non-zero exit
            input_data: Optional input to send to stdin
            timeout: Command timeout in seconds

        Returns:
            CompletedProcess instance
        """
        if self._use_native:
            return self._run_kubectl_native(args, kubeconfig, check, input_data, timeout)
        else:
            return self._run_kubectl_docker(args, kubeconfig, check, input_data, timeout)

    def create_namespace(self, namespace: str, kubeconfig: Path) -> bool:
        """Create a Kubernetes namespace if it doesn't exist.

        Args:
            namespace: Name of the namespace to create
            kubeconfig: Path to kubeconfig file

        Returns:
            True if namespace exists or was created, False on error
        """
        logger.info(f"Creating namespace: {namespace}")
        try:
            result = self.run_kubectl(["get", "namespace", namespace], kubeconfig, check=False)
            if result.returncode == 0:
                logger.info(f"Namespace {namespace} already exists")
                return True

            self.run_kubectl(["create", "namespace", namespace], kubeconfig)
            logger.info(f"Namespace {namespace} created")
            return True
        except Exception as e:
            logger.error(f"Failed to create namespace: {e}")
            return False

    def apply_yaml(
        self,
        yaml_content: str,
        kubeconfig: Path,
        namespace: Optional[str] = None,
        validate: bool = True
    ) -> bool:
        """Apply YAML configuration to the cluster.

        Args:
            yaml_content: YAML content to apply
            kubeconfig: Path to kubeconfig file
            namespace: Optional namespace to apply to
            validate: If True, validate YAML before applying (default True)

        Returns:
            True if application succeeded, False on error
        """
        # Validate YAML before applying
        if validate:
            is_valid, error = validate_yaml(yaml_content)
            if not is_valid:
                logger.error(f"YAML validation failed: {error}")
                return False

        try:
            args = ["apply", "-f", "-"]
            if namespace:
                args.extend(["-n", namespace])
            self.run_kubectl(args, kubeconfig, input_data=yaml_content)
            return True
        except Exception as e:
            logger.error(f"Failed to apply YAML: {e}")
            return False

    def apply_yaml_file(
        self,
        yaml_path: Path,
        kubeconfig: Path,
        namespace: Optional[str] = None
    ) -> bool:
        """Apply YAML file to the cluster.

        Args:
            yaml_path: Path to YAML file
            kubeconfig: Path to kubeconfig file
            namespace: Optional namespace to apply to

        Returns:
            True if application succeeded, False on error
        """
        yaml_content = yaml_path.read_text(encoding='utf-8')
        return self.apply_yaml(yaml_content, kubeconfig, namespace)

    def apply_url(self, url: str, kubeconfig: Path) -> bool:
        """Apply YAML from a URL.

        Args:
            url: URL to the YAML resource
            kubeconfig: Path to kubeconfig file

        Returns:
            True if application succeeded, False on error
        """
        try:
            self.run_kubectl(["apply", "-f", url], kubeconfig)
            return True
        except Exception as e:
            logger.error(f"Failed to apply URL {url}: {e}")
            return False

    def wait_for_deployment(
        self,
        name: str,
        namespace: str,
        kubeconfig: Path,
        timeout: int = 300
    ) -> bool:
        """Wait for a deployment to be ready.

        Args:
            name: Name of the deployment
            namespace: Namespace of the deployment
            kubeconfig: Path to kubeconfig file
            timeout: Maximum wait time in seconds

        Returns:
            True if deployment is ready, False on timeout
        """
        logger.info(f"Waiting for deployment {name} to be ready...")
        try:
            self.run_kubectl([
                "wait", "--for=condition=available",
                f"deployment/{name}",
                "-n", namespace,
                f"--timeout={timeout}s"
            ], kubeconfig, timeout=timeout + 30)
            logger.info(f"Deployment {name} is ready")
            return True
        except Exception as e:
            logger.error(f"Deployment {name} not ready: {e}")
            return False

    def wait_for_pods(
        self,
        label: str,
        namespace: str,
        kubeconfig: Path,
        expected: int,
        timeout: int = 300,
        poll_interval: int = 10
    ) -> bool:
        """Wait for pods to be ready.

        Args:
            label: Label selector for pods
            namespace: Namespace of the pods
            kubeconfig: Path to kubeconfig file
            expected: Number of pods expected to be running
            timeout: Maximum wait time in seconds
            poll_interval: Interval between checks in seconds

        Returns:
            True if all pods are running, False on timeout
        """
        logger.info(f"Waiting for {expected} pods with label {label}...")
        start_time = time.time()

        while time.time() - start_time < timeout:
            result = self.run_kubectl([
                "get", "pods", "-n", namespace,
                "-l", label,
                "-o", "jsonpath={.items[*].status.phase}"
            ], kubeconfig, check=False)

            if result.returncode == 0:
                phases = result.stdout.split()
                running = sum(1 for p in phases if p == "Running")
                if running >= expected:
                    logger.info(f"All {expected} pods are running")
                    return True
                logger.info(f"Pods running: {running}/{expected}")

            time.sleep(poll_interval)

        logger.error("Timeout waiting for pods")
        return False

    def delete_resource(
        self,
        resource_type: str,
        name: str,
        namespace: str,
        kubeconfig: Path,
        ignore_not_found: bool = True
    ) -> bool:
        """Delete a Kubernetes resource.

        Args:
            resource_type: Type of resource (e.g., 'secret', 'configmap')
            name: Name of the resource
            namespace: Namespace of the resource
            kubeconfig: Path to kubeconfig file
            ignore_not_found: If True, don't fail if resource doesn't exist

        Returns:
            True if deletion succeeded, False on error
        """
        args = ["delete", resource_type, name, "-n", namespace]
        if ignore_not_found:
            args.append("--ignore-not-found")

        try:
            self.run_kubectl(args, kubeconfig, check=not ignore_not_found)
            return True
        except Exception as e:
            logger.error(f"Failed to delete {resource_type}/{name}: {e}")
            return False

    def get_resource_json(
        self,
        resource_type: str,
        name: str,
        namespace: str,
        kubeconfig: Path
    ) -> Optional[str]:
        """Get a Kubernetes resource as JSON.

        Args:
            resource_type: Type of resource
            name: Name of the resource
            namespace: Namespace of the resource
            kubeconfig: Path to kubeconfig file

        Returns:
            JSON string of the resource, or None if not found
        """
        try:
            result = self.run_kubectl([
                "get", resource_type, name,
                "-n", namespace,
                "-o", "json"
            ], kubeconfig, check=False)
            return result.stdout if result.returncode == 0 else None
        except Exception:
            return None

    def wait_for_service(
        self,
        name: str,
        namespace: str,
        kubeconfig: Path,
        timeout: int = 60,
        poll_interval: int = 5
    ) -> bool:
        """Wait for a service to exist.

        Args:
            name: Name of the service
            namespace: Namespace of the service
            kubeconfig: Path to kubeconfig file
            timeout: Maximum wait time in seconds
            poll_interval: Interval between checks in seconds

        Returns:
            True if service exists, False on timeout
        """
        logger.info(f"Waiting for service {name} to exist...")
        start_time = time.time()

        while time.time() - start_time < timeout:
            result = self.run_kubectl([
                "get", "service", name,
                "-n", namespace
            ], kubeconfig, check=False)

            if result.returncode == 0:
                logger.info(f"Service {name} exists")
                return True

            time.sleep(poll_interval)

        logger.error(f"Timeout waiting for service {name}")
        return False

    def wait_for_services_by_label(
        self,
        label: str,
        namespace: str,
        kubeconfig: Path,
        expected: int,
        timeout: int = 60,
        poll_interval: int = 5
    ) -> bool:
        """Wait for services matching a label to exist.

        Args:
            label: Label selector for services
            namespace: Namespace of the services
            kubeconfig: Path to kubeconfig file
            expected: Number of services expected
            timeout: Maximum wait time in seconds
            poll_interval: Interval between checks in seconds

        Returns:
            True if all expected services exist, False on timeout
        """
        logger.info(f"Waiting for {expected} services with label {label}...")
        start_time = time.time()

        while time.time() - start_time < timeout:
            result = self.run_kubectl([
                "get", "services", "-n", namespace,
                "-l", label,
                "-o", "jsonpath={.items[*].metadata.name}"
            ], kubeconfig, check=False)

            if result.returncode == 0:
                services = result.stdout.split()
                if len(services) >= expected:
                    logger.info(f"Found {len(services)} services: {', '.join(services)}")
                    return True
                logger.debug(f"Services found: {len(services)}/{expected}")

            time.sleep(poll_interval)

        logger.error(f"Timeout waiting for services with label {label}")
        return False

    def wait_for_statefulset(
        self,
        name: str,
        namespace: str,
        kubeconfig: Path,
        timeout: int = 300
    ) -> bool:
        """Wait for a StatefulSet to be ready.

        Uses kubectl wait to check for the ready condition.

        Args:
            name: Name of the StatefulSet
            namespace: Namespace of the StatefulSet
            kubeconfig: Path to kubeconfig file
            timeout: Maximum wait time in seconds

        Returns:
            True if StatefulSet is ready, False on timeout
        """
        logger.info(f"Waiting for StatefulSet {name} to be ready...")
        try:
            # Wait for all replicas to be ready
            self.run_kubectl([
                "rollout", "status",
                f"statefulset/{name}",
                "-n", namespace,
                f"--timeout={timeout}s"
            ], kubeconfig, timeout=timeout + 30)
            logger.info(f"StatefulSet {name} is ready")
            return True
        except Exception as e:
            logger.error(f"StatefulSet {name} not ready: {e}")
            return False

    def wait_for_secret(
        self,
        name: str,
        namespace: str,
        kubeconfig: Path,
        timeout: int = 60,
        poll_interval: int = 5
    ) -> bool:
        """Wait for a secret to exist.

        Args:
            name: Name of the secret
            namespace: Namespace of the secret
            kubeconfig: Path to kubeconfig file
            timeout: Maximum wait time in seconds
            poll_interval: Interval between checks in seconds

        Returns:
            True if secret exists, False on timeout
        """
        logger.info(f"Waiting for secret {name} to exist...")
        start_time = time.time()

        while time.time() - start_time < timeout:
            result = self.run_kubectl([
                "get", "secret", name,
                "-n", namespace
            ], kubeconfig, check=False)

            if result.returncode == 0:
                logger.info(f"Secret {name} exists")
                return True

            time.sleep(poll_interval)

        logger.error(f"Timeout waiting for secret {name}")
        return False
