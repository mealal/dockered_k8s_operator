#!/usr/bin/env python3
"""
MongoDB Enterprise Kubernetes Operator Multi-Cluster Deployment

This script deploys MongoDB across multiple Kubernetes clusters:
1. Creates 2 kind clusters (central + 1 member)
2. Deploys MongoDB Enterprise Kubernetes Operator to central cluster
3. Configures cross-cluster connectivity
4. Deploys a MongoDBMultiCluster resource spanning both clusters

Based on MongoDB documentation:
https://www.mongodb.com/docs/kubernetes-operator/v1.33/multi-cluster-no-service-mesh-deploy-rs/

Prerequisites:
- Docker running
- Ops Manager running and accessible
- Output from deploy_ops_manager.py (creates both SingleCluster and MultiCluster projects)

All tools (kind, kubectl) run inside Docker containers - no local installation required.
"""

from __future__ import annotations

import subprocess
import sys
import time
import argparse
import logging
import os
import re
import base64
import urllib.request
import urllib.error
from pathlib import Path
from dataclasses import dataclass, field
from typing import Optional, List, Dict

# Import shared modules
from shared.validators import positive_int, non_negative_int, valid_port, valid_namespace, valid_timeout
from shared.utils import run_command, convert_path_for_docker, check_docker, generate_secure_password
from shared.models import OpsManagerCredentials
from shared.preflight import PreFlightChecker
from shared.decorators import retry_with_backoff
from shared.yaml_manager_base import BaseYAMLTemplateManager
from shared.kind_manager_base import BaseKindManager
from shared.k8s_manager_base import BaseKubernetesManager
from shared.ops_manager_cleanup import cleanup_ops_manager_project

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger(__name__)

# Script directory for resolving relative paths
SCRIPT_DIR = Path(__file__).parent.resolve()


def run_openssl(args: list, description: str) -> bool:
    """Run an OpenSSL command with proper error logging.
    
    Args:
        args: Command arguments (without 'openssl' prefix)
        description: Human-readable description of the operation
        
    Returns:
        True if successful, False otherwise
    """
    cmd = ["openssl"] + args
    try:
        result = subprocess.run(
            cmd, check=True, capture_output=True, text=True,
            encoding='utf-8', errors='replace'
        )
        return True
    except subprocess.CalledProcessError as e:
        logger.error(f"OpenSSL {description} failed")
        if e.stderr:
            logger.error(f"OpenSSL stderr: {e.stderr.strip()}")
        if e.stdout:
            logger.debug(f"OpenSSL stdout: {e.stdout.strip()}")
        return False
    except FileNotFoundError:
        logger.error("OpenSSL not found. Please install OpenSSL and ensure it's in your PATH.")
        return False


# Constants - Docker images for kind and kubectl
KIND_NODE_IMAGE = "kindest/node:v1.28.0"
KUBECTL_IMAGE = "bitnami/kubectl:1.28"
OPERATOR_VERSION = "1.33.0"

# Official MongoDB Enterprise Kubernetes Operator installation URLs
OPERATOR_CRDS_URL = "https://raw.githubusercontent.com/mongodb/mongodb-enterprise-kubernetes/master/crds.yaml"
OPERATOR_INSTALL_URL = "https://raw.githubusercontent.com/mongodb/mongodb-enterprise-kubernetes/master/mongodb-enterprise.yaml"
# Multi-cluster operator manifest (different from single-cluster)
OPERATOR_MULTI_CLUSTER_URL = "https://raw.githubusercontent.com/mongodb/mongodb-enterprise-kubernetes/master/mongodb-enterprise-multi-cluster.yaml"

# kubectl-mongodb plugin download URLs (Note: No Windows version available from MongoDB)
KUBECTL_MONGODB_PLUGIN_VERSION = "1.33.0"
KUBECTL_MONGODB_PLUGIN_URLS = {
    # Windows is NOT supported by MongoDB - plugin not available
    "linux": f"https://github.com/mongodb/mongodb-enterprise-kubernetes/releases/download/{KUBECTL_MONGODB_PLUGIN_VERSION}/kubectl-mongodb_{KUBECTL_MONGODB_PLUGIN_VERSION}_linux_amd64.tar.gz",
    "darwin": f"https://github.com/mongodb/mongodb-enterprise-kubernetes/releases/download/{KUBECTL_MONGODB_PLUGIN_VERSION}/kubectl-mongodb_{KUBECTL_MONGODB_PLUGIN_VERSION}_darwin_amd64.tar.gz",
}

# YAML template directory for multi-cluster
K8S_MULTI_YAML_DIR = SCRIPT_DIR / "k8s-multi"

# Template file names
COREDNS_CONFIGMAP_TEMPLATE = "coredns-configmap.yaml"
KIND_CLUSTER_CONFIG_TEMPLATE = "kind-cluster-config.yaml"

# Multi-cluster specific constants
CENTRAL_CLUSTER_NAME = "mongodb-central"
MEMBER_CLUSTER_NAME = "mongodb-member-1"

# Multi-cluster operator deployment name (different from single-cluster operator)
OPERATOR_MULTI_CLUSTER_DEPLOYMENT_NAME = "mongodb-enterprise-operator-multi-cluster"


# =============================================================================
# Argument Validation Helpers (imported from shared.validators)
# =============================================================================

# positive_int, non_negative_int, valid_port, valid_namespace are imported from shared.validators


# =============================================================================
# YAML Template Manager for Multi-Cluster
# =============================================================================

class MultiClusterYAMLManager(BaseYAMLTemplateManager):
    """Manages YAML template files for multi-cluster Kubernetes resources.

    Inherits common functionality from BaseYAMLTemplateManager and adds
    multi-cluster specific methods for cross-cluster deployment.
    """

    def __init__(self, template_dir: Path = K8S_MULTI_YAML_DIR):
        super().__init__(template_dir)

    def render_multicluster(self, namespace: str, rs_name: str,
                             central_cluster: str, member_cluster: str,
                             central_members: int, member_members: int,
                             central_external_domain: str, member_external_domain: str,
                             tls_require_valid_certs: bool = True) -> Path:
        """Render mongodb-multicluster.yaml with multi-cluster configuration."""
        return self._render_with_namespace(
            "mongodb-multicluster.yaml",
            {
                "REPLICA_SET_NAME": rs_name,
                "CENTRAL_CLUSTER_NAME": central_cluster,
                "MEMBER_CLUSTER_NAME": member_cluster,
                "CENTRAL_MEMBERS": str(central_members),
                "MEMBER_MEMBERS": str(member_members),
                "CENTRAL_EXTERNAL_DOMAIN": central_external_domain,
                "MEMBER_EXTERNAL_DOMAIN": member_external_domain,
                "TLS_REQUIRE_VALID_CERTS": "true" if tls_require_valid_certs else "false",
                "SSL_REQUIRE_VALID_MMS_CERTS": "true" if tls_require_valid_certs else "false",
            },
            namespace=namespace
        )

    def render_kubeconfig_secret(self, namespace: str, kubeconfig_data: str) -> Path:
        """Render kubeconfig-secret.yaml with multi-cluster kubeconfig."""
        return self._render_with_namespace(
            "kubeconfig-secret.yaml",
            {"KUBECONFIG_DATA": kubeconfig_data},
            namespace=namespace
        )

    def render_member_list_configmap(self, namespace: str, member_clusters: List[str]) -> Path:
        """Render member-list-configmap.yaml with list of member clusters.

        The ConfigMap format requires each cluster name as a KEY with empty string value.
        Example:
            data:
              kind-mongodb-central: ""
              kind-mongodb-member-1: ""
        """
        # Generate YAML-formatted entries for each cluster (2-space indent for data section)
        entries = "\n".join([f'  {cluster}: ""' for cluster in member_clusters])
        return self._render_with_namespace(
            "member-list-configmap.yaml",
            {"MEMBER_CLUSTER_ENTRIES": entries},
            namespace=namespace
        )

    def render_member_cluster_rbac(self, namespace: str) -> Path:
        """Render member-cluster-rbac.yaml for member cluster resources."""
        return self._render_with_namespace(
            "member-cluster-rbac.yaml",
            {"RS_NAMESPACE": namespace}
        )

    def render_coredns_configmap(self, hosts_entries: str, rewrite_rules: str = "") -> str:
        """Render CoreDNS ConfigMap YAML from template.

        Args:
            hosts_entries: Pre-formatted hosts entries with proper indentation.
            rewrite_rules: Pre-formatted rewrite rules for local DNS resolution.

        Returns:
            Rendered YAML content as string.
        """
        template_path = self.template_dir / COREDNS_CONFIGMAP_TEMPLATE
        if not template_path.exists():
            raise FileNotFoundError(f"CoreDNS template not found: {template_path}")

        content = template_path.read_text(encoding='utf-8')
        content = content.replace("{{HOSTS_ENTRIES}}", hosts_entries)
        content = content.replace("{{REWRITE_RULES}}", rewrite_rules)
        return content


# =============================================================================
# Utility Functions (imported from shared modules)
# =============================================================================

# retry_with_backoff imported from shared.decorators
# PreFlightChecker imported from shared.preflight
# OpsManagerCredentials imported from shared.models
# run_command, convert_path_for_docker, generate_secure_password imported from shared.utils


@dataclass
class MultiClusterConfig:
    """Configuration for multi-cluster deployment."""
    central_cluster_name: str = CENTRAL_CLUSTER_NAME
    member_cluster_name: str = MEMBER_CLUSTER_NAME
    operator_namespace: str = "mongodb"
    rs_namespace: str = "mongodb-rs"
    ops_manager_url: str = "https://host.docker.internal:8443"
    kubeconfig_dir: str = "./.kube-multi"
    ssl_skip_verify: bool = False
    # Members per cluster (default: 5 total = 3 central + 2 member)
    central_members: int = 3
    member_members: int = 2
    # External domains for cross-cluster connectivity
    central_external_domain: str = "central.mongodb.local"
    member_external_domain: str = "member1.mongodb.local"
    # External ports for NodePort services (one per member)
    central_ports: List[int] = field(default_factory=lambda: [30100, 30101, 30102])
    member_ports: List[int] = field(default_factory=lambda: [30200, 30201])


@dataclass
class ReplicaSetConfig:
    """Configuration for the MongoDB replica set."""
    name: str = "mongodb-multi-rs"
    mongodb_username: str = "admin"
    mongodb_password: Optional[str] = None


# generate_secure_password, run_command, convert_path_for_docker imported from shared.utils


# =============================================================================
# kubectl-mongodb Plugin Manager
# =============================================================================

class KubectlMongoDBPlugin:
    """Manages the kubectl-mongodb plugin for multi-cluster setup."""

    def __init__(self, install_dir: Path):
        self.install_dir = install_dir
        self.install_dir.mkdir(parents=True, exist_ok=True)

        # Binary name depends on platform
        plugin_ext = ".exe" if sys.platform == "win32" else ""
        self.plugin_binary = self.install_dir / f"kubectl-mongodb{plugin_ext}"

    def is_installed(self) -> bool:
        """Check if the plugin is already installed."""
        return self.plugin_binary.exists()

    def download_and_install(self) -> bool:
        """Download and install the kubectl-mongodb plugin."""
        if self.is_installed():
            logger.info(f"kubectl-mongodb plugin already installed: {self.plugin_binary}")
            return True

        platform = sys.platform
        if platform not in KUBECTL_MONGODB_PLUGIN_URLS:
            logger.error(f"Unsupported platform for kubectl-mongodb plugin: {platform}")
            return False

        url = KUBECTL_MONGODB_PLUGIN_URLS[platform]
        logger.info(f"Downloading kubectl-mongodb plugin from: {url}")

        try:
            # Download the archive
            if platform == "win32":
                archive_path = self.install_dir / "kubectl-mongodb.zip"
            else:
                archive_path = self.install_dir / "kubectl-mongodb.tar.gz"

            urllib.request.urlretrieve(url, str(archive_path))
            logger.info(f"Downloaded to: {archive_path}")

            # Extract the archive
            if platform == "win32":
                import zipfile
                with zipfile.ZipFile(archive_path, 'r') as zip_ref:
                    zip_ref.extractall(self.install_dir)
            else:
                import tarfile
                with tarfile.open(archive_path, 'r:gz') as tar_ref:
                    tar_ref.extractall(self.install_dir)

            # Make executable on Unix
            if platform != "win32":
                import stat
                self.plugin_binary.chmod(
                    self.plugin_binary.stat().st_mode | stat.S_IEXEC
                )

            # Clean up archive
            archive_path.unlink()

            logger.info(f"kubectl-mongodb plugin installed: {self.plugin_binary}")
            return True

        except Exception as e:
            logger.error(f"Failed to download/install kubectl-mongodb plugin: {e}")
            return False

    def run(self, args: List[str], kubeconfig: Optional[Path] = None,
            check: bool = True, timeout: int = 300) -> subprocess.CompletedProcess:
        """Run the kubectl-mongodb plugin with given arguments."""
        if not self.is_installed():
            raise RuntimeError("kubectl-mongodb plugin is not installed")

        cmd = [str(self.plugin_binary)] + args

        env = os.environ.copy()
        if kubeconfig:
            env["KUBECONFIG"] = str(kubeconfig)

        logger.debug(f"Running: {' '.join(cmd)}")

        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                encoding='utf-8',
                errors='replace',
                timeout=timeout,
                env=env
            )

            if check and result.returncode != 0:
                logger.error(f"kubectl-mongodb failed: {result.stderr}")
                raise subprocess.CalledProcessError(result.returncode, cmd, result.stdout, result.stderr)

            return result
        except subprocess.TimeoutExpired:
            logger.error(f"kubectl-mongodb command timed out")
            raise

    def multicluster_setup(self, central_cluster: str, member_clusters: List[str],
                           namespace: str, kubeconfig: Path,
                           create_service_account_secrets: bool = True,
                           install_database_roles: bool = True) -> bool:
        """Run the multicluster setup command."""
        logger.info("Running kubectl mongodb multicluster setup...")

        args = [
            "multicluster", "setup",
            f"--central-cluster={central_cluster}",
            f"--member-clusters={','.join(member_clusters)}",
            f"--member-cluster-namespace={namespace}",
            f"--central-cluster-namespace={namespace}",
        ]

        if create_service_account_secrets:
            args.append("--create-service-account-secrets")

        if install_database_roles:
            args.append("--install-database-roles=true")

        try:
            result = self.run(args, kubeconfig=kubeconfig, timeout=600)
            logger.info("Multicluster setup completed successfully")
            if result.stdout:
                logger.debug(f"Output: {result.stdout}")
            return True
        except subprocess.CalledProcessError as e:
            logger.error(f"Multicluster setup failed: {e.stderr}")
            return False


# =============================================================================
# Multi-Cluster Kind Manager
# =============================================================================

class MultiClusterKindManager(BaseKindManager):
    """Manages multiple kind Kubernetes clusters for multi-cluster deployment.

    Inherits common functionality from BaseKindManager and adds
    multi-cluster specific operations like combined kubeconfig creation.
    """

    def __init__(self, config: MultiClusterConfig, yaml_manager: Optional[MultiClusterYAMLManager] = None):
        self.config = config
        self.yaml_manager = yaml_manager or MultiClusterYAMLManager()

        # Initialize base class
        super().__init__(Path(config.kubeconfig_dir))

        # Separate kubeconfig files for each cluster
        self.central_kubeconfig = self.kubeconfig_path / "central-config"
        self.member_kubeconfig = self.kubeconfig_path / "member-config"
        # Combined kubeconfig for multi-cluster operations
        self.combined_kubeconfig = self.kubeconfig_path / "config"

    def create_cluster(self, cluster_name: str, ports: List[int], kubeconfig_file: Path) -> bool:
        """Create a kind cluster with specified port mappings."""
        config_content = self.yaml_manager.render_kind_cluster_config(ports)
        return self.create_cluster_with_config(cluster_name, config_content, kubeconfig_file)

    def create_all_clusters(self) -> bool:
        """Create both central and member clusters."""
        # Create central cluster
        if not self.create_cluster(
            self.config.central_cluster_name,
            self.config.central_ports,
            self.central_kubeconfig
        ):
            return False

        # Create member cluster
        if not self.create_cluster(
            self.config.member_cluster_name,
            self.config.member_ports,
            self.member_kubeconfig
        ):
            return False

        # Create combined kubeconfig
        self._create_combined_kubeconfig()
        return True

    def _create_combined_kubeconfig(self) -> None:
        """Create a combined kubeconfig with both clusters."""
        import yaml

        # Load both kubeconfigs
        central_config = yaml.safe_load(self.central_kubeconfig.read_text())
        member_config = yaml.safe_load(self.member_kubeconfig.read_text())

        # Rename contexts to be more descriptive
        central_context = f"kind-{self.config.central_cluster_name}"
        member_context = f"kind-{self.config.member_cluster_name}"

        # Merge into combined config
        combined = {
            "apiVersion": "v1",
            "kind": "Config",
            "preferences": {},
            "current-context": central_context,
            "clusters": central_config["clusters"] + member_config["clusters"],
            "contexts": central_config["contexts"] + member_config["contexts"],
            "users": central_config["users"] + member_config["users"],
        }

        self.combined_kubeconfig.write_text(yaml.dump(combined, default_flow_style=False))
        logger.info(f"Combined kubeconfig created: {self.combined_kubeconfig}")

    def delete_cluster(self, cluster_name: str) -> bool:
        """Delete a kind cluster."""
        logger.info(f"Deleting kind cluster: {cluster_name}")
        try:
            self._run_kind(["delete", "cluster", "--name", cluster_name])
            logger.info(f"Cluster {cluster_name} deleted")
            return True
        except Exception as e:
            logger.error(f"Failed to delete cluster: {e}")
            return False

    def delete_all_clusters(self) -> bool:
        """Delete both central and member clusters."""
        success = True
        if not self.delete_cluster(self.config.central_cluster_name):
            success = False
        if not self.delete_cluster(self.config.member_cluster_name):
            success = False
        return success


# =============================================================================
# Kubernetes Manager for Multi-Cluster
# =============================================================================

class MultiClusterKubernetesManager(BaseKubernetesManager):
    """Manages Kubernetes resources across multiple clusters.

    Extends BaseKubernetesManager with multi-cluster convenience methods
    for running kubectl commands against central and member clusters.
    """

    def __init__(self, config: MultiClusterConfig, kind_manager: MultiClusterKindManager):
        """Initialize multi-cluster Kubernetes manager.

        Args:
            config: Multi-cluster configuration
            kind_manager: Kind manager with kubeconfig paths
        """
        super().__init__(kubectl_image=KUBECTL_IMAGE)
        self.config = config
        self.kind_manager = kind_manager

    def run_kubectl_central(self, args: List[str], check: bool = True,
                            input_data: Optional[str] = None) -> subprocess.CompletedProcess:
        """Run kubectl on central cluster.

        Args:
            args: kubectl arguments
            check: If True, raise on non-zero exit
            input_data: Optional stdin data

        Returns:
            CompletedProcess instance
        """
        return self.run_kubectl(args, self.kind_manager.central_kubeconfig, check, input_data)

    def run_kubectl_member(self, args: List[str], check: bool = True,
                           input_data: Optional[str] = None) -> subprocess.CompletedProcess:
        """Run kubectl on member cluster.

        Args:
            args: kubectl arguments
            check: If True, raise on non-zero exit
            input_data: Optional stdin data

        Returns:
            CompletedProcess instance
        """
        return self.run_kubectl(args, self.kind_manager.member_kubeconfig, check, input_data)

    def wait_for_mongodb_services(
        self,
        rs_name: str,
        namespace: str,
        expected_count: int,
        timeout: int = 120,
        poll_interval: int = 5
    ) -> bool:
        """Wait for MongoDB services to be created by the operator.

        Checks both central and member clusters for services matching the
        replica set name pattern.

        Args:
            rs_name: Name of the MongoDB replica set
            namespace: Namespace where MongoDB is deployed
            expected_count: Total expected service count across clusters
            timeout: Maximum wait time in seconds
            poll_interval: Interval between checks in seconds

        Returns:
            True if services are found, False on timeout
        """
        logger.info(f"Waiting for MongoDB services to be created (expecting {expected_count})...")
        start_time = time.time()

        while time.time() - start_time < timeout:
            total_services = 0

            # Check central cluster
            result = self.run_kubectl_central([
                "get", "services", "-n", namespace,
                "-l", f"app={rs_name}",
                "-o", "jsonpath={.items[*].metadata.name}"
            ], check=False)
            if result.returncode == 0 and result.stdout.strip():
                central_services = result.stdout.split()
                total_services += len(central_services)

            # Check member cluster
            result = self.run_kubectl_member([
                "get", "services", "-n", namespace,
                "-l", f"app={rs_name}",
                "-o", "jsonpath={.items[*].metadata.name}"
            ], check=False)
            if result.returncode == 0 and result.stdout.strip():
                member_services = result.stdout.split()
                total_services += len(member_services)

            if total_services >= expected_count:
                logger.info(f"Found {total_services} MongoDB services across clusters")
                return True

            logger.debug(f"Services found: {total_services}/{expected_count}")
            time.sleep(poll_interval)

        logger.warning(f"Timeout waiting for MongoDB services after {timeout}s")
        return False


# =============================================================================
# Multi-Cluster Operator Deployer
# =============================================================================

class MultiClusterOperatorDeployer:
    """Deploys MongoDB Enterprise Kubernetes Operator for multi-cluster setup."""

    def __init__(self, k8s: MultiClusterKubernetesManager, credentials: OpsManagerCredentials,
                 config: MultiClusterConfig, yaml_manager: Optional[MultiClusterYAMLManager] = None,
                 kubectl_mongodb_plugin: Optional[KubectlMongoDBPlugin] = None):
        self.k8s = k8s
        self.credentials = credentials
        self.config = config
        self.yaml_manager = yaml_manager or MultiClusterYAMLManager()
        self.plugin = kubectl_mongodb_plugin

    def deploy_crds(self) -> bool:
        """Deploy MongoDB CRDs to central cluster."""
        logger.info("Deploying MongoDB CRDs to central cluster...")
        try:
            self.k8s.run_kubectl_central(["apply", "-f", OPERATOR_CRDS_URL])
            logger.info("CRDs deployed successfully")
            return True
        except Exception as e:
            logger.error(f"Failed to deploy CRDs: {e}")
            return False

    def run_multicluster_setup(self) -> bool:
        """Run kubectl mongodb multicluster setup command."""
        if not self.plugin:
            logger.warning("kubectl-mongodb plugin not available, skipping multicluster setup")
            return True

        # Get cluster context names (kind uses "kind-<cluster-name>" format)
        central_context = f"kind-{self.config.central_cluster_name}"
        member_context = f"kind-{self.config.member_cluster_name}"

        return self.plugin.multicluster_setup(
            central_cluster=central_context,
            member_clusters=[member_context],
            namespace=self.config.operator_namespace,
            kubeconfig=self.k8s.kind_manager.combined_kubeconfig,
            create_service_account_secrets=True,
            install_database_roles=True
        )

    def deploy_operator(self) -> bool:
        """Deploy the MongoDB Enterprise Kubernetes Operator (multi-cluster version)."""
        logger.info(f"Deploying MongoDB Enterprise Kubernetes Operator v{OPERATOR_VERSION} (multi-cluster)...")

        try:
            # Use the multi-cluster operator manifest instead of the standard one
            self.k8s.run_kubectl_central(["apply", "-f", OPERATOR_MULTI_CLUSTER_URL])
            logger.info("Multi-cluster operator resources applied successfully")

            # Configure operator to watch the replica set namespace
            # Note: Multi-cluster operator deployment name is different
            rs_namespace = self.config.rs_namespace
            operator_namespace = self.config.operator_namespace
            logger.info(f"Configuring operator to watch namespace: {rs_namespace}")

            self.k8s.run_kubectl_central([
                "set", "env", f"deployment/{OPERATOR_MULTI_CLUSTER_DEPLOYMENT_NAME}",
                "-n", operator_namespace,
                f"WATCH_NAMESPACE={rs_namespace}"
            ])

            return True
        except Exception as e:
            logger.error(f"Failed to deploy operator: {e}")
            return False

    def create_member_list_configmap(self) -> bool:
        """Create ConfigMap with list of member clusters."""
        logger.info("Creating member list ConfigMap...")

        # Use kind context names (kind-<cluster-name>) which match the kubeconfig contexts
        member_clusters = [
            f"kind-{self.config.central_cluster_name}",
            f"kind-{self.config.member_cluster_name}"
        ]

        yaml_path = self.yaml_manager.render_member_list_configmap(
            self.config.operator_namespace,
            member_clusters
        )

        yaml_content = yaml_path.read_text(encoding='utf-8')
        return self.k8s.apply_yaml(yaml_content, self.k8s.kind_manager.central_kubeconfig)

    def create_kubeconfig_secret(self) -> bool:
        """Create secret with multi-cluster kubeconfig for operator.

        The kubeconfig must use Docker network IPs instead of localhost addresses
        because the operator pod needs to reach both clusters from within the
        Kind network, not from the host machine.
        """
        logger.info("Creating kubeconfig secret for multi-cluster access...")

        # Get Docker network IPs for Kind control planes
        central_ip = self._get_container_ip(f"{self.config.central_cluster_name}-control-plane")
        member_ip = self._get_container_ip(f"{self.config.member_cluster_name}-control-plane")

        if not central_ip or not member_ip:
            logger.error("Failed to get Docker network IPs for Kind clusters")
            return False

        logger.info(f"Kind cluster Docker IPs - Central: {central_ip}, Member: {member_ip}")

        # Read combined kubeconfig and replace localhost with Docker IPs
        kubeconfig_content = self.k8s.kind_manager.combined_kubeconfig.read_text()

        # Replace localhost API server addresses with Docker network IPs
        # Kind uses port 6443 internally, even if mapped to different ports on localhost
        kubeconfig_internal = self._replace_kubeconfig_servers(
            kubeconfig_content,
            central_ip,
            member_ip
        )

        kubeconfig_b64 = base64.b64encode(kubeconfig_internal.encode()).decode()

        yaml_path = self.yaml_manager.render_kubeconfig_secret(
            self.config.operator_namespace,
            kubeconfig_b64
        )

        yaml_content = yaml_path.read_text(encoding='utf-8')
        return self.k8s.apply_yaml(yaml_content, self.k8s.kind_manager.central_kubeconfig)

    def _get_container_ip(self, container_name: str) -> Optional[str]:
        """Get the Docker network IP of a container on the 'kind' network."""
        try:
            result = subprocess.run(
                ["docker", "inspect", "-f",
                 "{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}",
                 container_name],
                capture_output=True,
                text=True,
                encoding='utf-8',
                errors='replace',
                check=True
            )
            ip = result.stdout.strip()
            return ip if ip else None
        except subprocess.CalledProcessError as e:
            logger.error(f"Failed to get IP for container {container_name}: {e}")
            return None

    def _replace_kubeconfig_servers(self, kubeconfig: str, central_ip: str, member_ip: str) -> str:
        """Replace localhost server addresses with Docker network IPs.

        Kind clusters expose the API server on port 6443 internally, even if
        mapped to different ports on the host (e.g., 49493 -> 6443).
        """
        import re
        import yaml

        config = yaml.safe_load(kubeconfig)

        for cluster in config.get("clusters", []):
            name = cluster.get("name", "")
            cluster_data = cluster.get("cluster", {})
            server = cluster_data.get("server", "")

            # Match patterns like https://127.0.0.1:49493 or https://localhost:49493
            if "central" in name.lower() and ("127.0.0.1" in server or "localhost" in server):
                cluster_data["server"] = f"https://{central_ip}:6443"
                logger.debug(f"Replaced {name} server: {server} -> https://{central_ip}:6443")
            elif "member" in name.lower() and ("127.0.0.1" in server or "localhost" in server):
                cluster_data["server"] = f"https://{member_ip}:6443"
                logger.debug(f"Replaced {name} server: {server} -> https://{member_ip}:6443")

        return yaml.dump(config, default_flow_style=False)

    @retry_with_backoff(max_retries=3, exceptions=(subprocess.CalledProcessError,))
    def create_ops_manager_secret(self) -> bool:
        """Create Ops Manager credentials secret."""
        rs_namespace = self.config.rs_namespace
        logger.info(f"Creating Ops Manager credentials secret in {rs_namespace}...")

        # Delete existing secret first
        self.k8s.run_kubectl_central([
            "delete", "secret", "ops-manager-admin-key",
            "-n", rs_namespace,
            "--ignore-not-found"
        ], check=False)

        yaml_path = self.yaml_manager.render_secret(
            namespace=rs_namespace,
            public_key=self.credentials.public_key,
            private_key=self.credentials.private_key
        )

        yaml_content = yaml_path.read_text(encoding='utf-8')
        return self.k8s.apply_yaml(yaml_content, self.k8s.kind_manager.central_kubeconfig)

    @retry_with_backoff(max_retries=3, exceptions=(subprocess.CalledProcessError,))
    def create_ops_manager_configmap(self) -> bool:
        """Create Ops Manager connection ConfigMap."""
        rs_namespace = self.config.rs_namespace
        logger.info(f"Creating Ops Manager ConfigMap in {rs_namespace}...")

        yaml_path = self.yaml_manager.render_configmap(
            namespace=rs_namespace,
            base_url=self.config.ops_manager_url,
            project_name=self.credentials.project_name,
            org_id=self.credentials.org_id,
            ssl_require_valid_certs=not self.config.ssl_skip_verify
        )

        yaml_content = yaml_path.read_text(encoding='utf-8')
        return self.k8s.apply_yaml(yaml_content, self.k8s.kind_manager.central_kubeconfig)

    def deploy_operator_rbac(self) -> bool:
        """Create RBAC for operator in replica set namespace."""
        rs_namespace = self.config.rs_namespace
        operator_namespace = self.config.operator_namespace
        logger.info(f"Creating RBAC for operator in {rs_namespace} namespace...")

        yaml_path = self.yaml_manager.render_operator_rbac(
            rs_namespace=rs_namespace,
            operator_namespace=operator_namespace
        )

        yaml_content = yaml_path.read_text(encoding='utf-8')
        return self.k8s.apply_yaml(yaml_content, self.k8s.kind_manager.central_kubeconfig)

    def deploy_database_roles_central(self) -> bool:
        """Deploy database roles in central cluster's rs_namespace."""
        rs_namespace = self.config.rs_namespace
        logger.info(f"Deploying database roles in central cluster {rs_namespace}...")

        yaml_path = self.yaml_manager.render_database_roles(rs_namespace=rs_namespace)
        yaml_content = yaml_path.read_text(encoding='utf-8')
        return self.k8s.apply_yaml(yaml_content, self.k8s.kind_manager.central_kubeconfig)

    def deploy_member_cluster_resources(self) -> bool:
        """Deploy required resources to member cluster (no operator, just RBAC)."""
        rs_namespace = self.config.rs_namespace
        logger.info(f"Deploying resources to member cluster...")

        # Create namespace on member cluster
        if not self.k8s.create_namespace(rs_namespace, self.k8s.kind_manager.member_kubeconfig):
            return False

        # Deploy RBAC for database pods on member cluster
        yaml_path = self.yaml_manager.render_member_cluster_rbac(namespace=rs_namespace)
        yaml_content = yaml_path.read_text(encoding='utf-8')
        if not self.k8s.apply_yaml(yaml_content, self.k8s.kind_manager.member_kubeconfig):
            return False

        # Deploy database roles on member cluster
        yaml_path = self.yaml_manager.render_database_roles(rs_namespace=rs_namespace)
        yaml_content = yaml_path.read_text(encoding='utf-8')
        return self.k8s.apply_yaml(yaml_content, self.k8s.kind_manager.member_kubeconfig)

    def deploy_all(self) -> bool:
        """Deploy all operator components for multi-cluster setup."""
        steps = [
            ("Running kubectl mongodb multicluster setup", self.run_multicluster_setup),
            ("Creating operator namespace (central)", lambda: self.k8s.create_namespace(
                self.config.operator_namespace, self.k8s.kind_manager.central_kubeconfig)),
            ("Creating RS namespace (central)", lambda: self.k8s.create_namespace(
                self.config.rs_namespace, self.k8s.kind_manager.central_kubeconfig)),
            ("Deploying CRDs", self.deploy_crds),
            ("Deploying multi-cluster operator", self.deploy_operator),
            ("Creating member list ConfigMap", self.create_member_list_configmap),
            ("Creating kubeconfig secret", self.create_kubeconfig_secret),
            ("Creating operator RBAC for RS namespace", self.deploy_operator_rbac),
            ("Deploying database roles (central)", self.deploy_database_roles_central),
            ("Deploying member cluster resources", self.deploy_member_cluster_resources),
            ("Creating Ops Manager secret", self.create_ops_manager_secret),
            ("Creating Ops Manager ConfigMap", self.create_ops_manager_configmap),
        ]

        for step_name, step_func in steps:
            logger.info(f"Step: {step_name}")
            if not step_func():
                logger.error(f"Failed at step: {step_name}")
                return False
            time.sleep(2)

        # Wait for multi-cluster operator to be ready
        return self.k8s.wait_for_deployment(
            OPERATOR_MULTI_CLUSTER_DEPLOYMENT_NAME,
            self.config.operator_namespace,
            self.k8s.kind_manager.central_kubeconfig,
            timeout=180
        )


# =============================================================================
# Multi-Cluster MongoDB Deployer
# =============================================================================

class MultiClusterMongoDBDeployer:
    """Deploys MongoDB across multiple clusters."""

    def __init__(self, k8s: MultiClusterKubernetesManager, credentials: OpsManagerCredentials,
                 config: MultiClusterConfig, rs_config: ReplicaSetConfig,
                 yaml_manager: Optional[MultiClusterYAMLManager] = None):
        self.k8s = k8s
        self.credentials = credentials
        self.config = config
        self.rs_config = rs_config
        self.yaml_manager = yaml_manager or MultiClusterYAMLManager()

    def generate_mongodb_certificates(self) -> bool:
        """Generate TLS certificates for MongoDB pods across all clusters."""
        rs_namespace = self.config.rs_namespace
        rs_name = self.rs_config.name
        certs_dir = SCRIPT_DIR / "certs/mongodb-multi"
        certs_dir.mkdir(parents=True, exist_ok=True)

        logger.info(f"Generating MongoDB TLS certificates for multi-cluster {rs_name}...")

        ca_cert = SCRIPT_DIR / "certs/ca.crt"
        ca_key = SCRIPT_DIR / "certs/ca.key"

        if not ca_cert.exists() or not ca_key.exists():
            logger.error("CA certificate not found. Run deploy_ops_manager.py first.")
            return False

        # Generate SANs for all MongoDB pods across both clusters
        dns_sans = []

        # Central cluster pods
        for i in range(self.config.central_members):
            dns_sans.append(f"DNS:{rs_name}-{i}.{rs_name}-svc.{rs_namespace}.svc.cluster.local")
            # External domain for cross-cluster access
            dns_sans.append(f"DNS:{rs_name}-0-{i}.{self.config.central_external_domain}")

        # Member cluster pods
        for i in range(self.config.member_members):
            dns_sans.append(f"DNS:{rs_name}-{i}.{rs_name}-svc.{rs_namespace}.svc.cluster.local")
            # External domain for cross-cluster access
            dns_sans.append(f"DNS:{rs_name}-1-{i}.{self.config.member_external_domain}")

        # Common SANs
        dns_sans.extend([
            f"DNS:{rs_name}-svc.{rs_namespace}.svc.cluster.local",
            f"DNS:*.{rs_name}-svc.{rs_namespace}.svc.cluster.local",
            "DNS:localhost",
            f"DNS:{self.config.central_external_domain}",
            f"DNS:{self.config.member_external_domain}",
            "IP:127.0.0.1",
            "IP:::1",
        ])

        san_list = ",".join(dns_sans)

        ext_file = certs_dir / "mongodb-ext.cnf"
        ext_content = f"""[req]
distinguished_name = req_distinguished_name
req_extensions = v3_req
prompt = no

[req_distinguished_name]
CN = {rs_name}

[v3_req]
basicConstraints = CA:FALSE
keyUsage = critical, digitalSignature, keyEncipherment
extendedKeyUsage = serverAuth, clientAuth
subjectAltName = {san_list}
"""
        ext_file.write_text(ext_content)

        mongodb_key = certs_dir / "mongodb.key"
        mongodb_csr = certs_dir / "mongodb.csr"
        mongodb_cert = certs_dir / "mongodb.crt"

        # Generate private key
        if not run_openssl(
            ["genrsa", "-out", str(mongodb_key), "2048"],
            "private key generation"
        ):
            return False

        # Generate CSR
        if not run_openssl(
            ["req", "-new", "-key", str(mongodb_key),
             "-out", str(mongodb_csr), "-config", str(ext_file)],
            "CSR generation"
        ):
            return False

        # Sign certificate with CA
        if not run_openssl(
            ["x509", "-req", "-in", str(mongodb_csr),
             "-CA", str(ca_cert), "-CAkey", str(ca_key),
             "-CAcreateserial", "-out", str(mongodb_cert),
             "-days", "365", "-extensions", "v3_req",
             "-extfile", str(ext_file)],
            "certificate signing"
        ):
            return False

        logger.info(f"Generated MongoDB certificate: {mongodb_cert}")

        # Clean up temporary files (CSR and extension config)
        for temp_file in [mongodb_csr, ext_file]:
            if temp_file.exists():
                temp_file.unlink()

        return True

    def create_tls_secrets_on_cluster(self, kubeconfig: Path, cluster_name: str) -> bool:
        """Create TLS secrets on a specific cluster."""
        rs_namespace = self.config.rs_namespace
        rs_name = self.rs_config.name

        certs_dir = SCRIPT_DIR / "certs"
        mongodb_certs_dir = SCRIPT_DIR / "certs/mongodb-multi"

        logger.info(f"Creating MongoDB TLS secrets on {cluster_name}...")

        ca_cert = certs_dir / "ca.crt"
        server_cert = mongodb_certs_dir / "mongodb.crt"
        server_key = mongodb_certs_dir / "mongodb.key"

        if not all(p.exists() for p in [ca_cert, server_cert, server_key]):
            logger.error("TLS certificates not found")
            return False

        # Create CA ConfigMap
        yaml_path = self.yaml_manager.render_mongodb_ca_configmap(
            rs_namespace=rs_namespace,
            ca_cert_path=ca_cert
        )
        yaml_content = yaml_path.read_text(encoding='utf-8')
        if not self.k8s.apply_yaml(yaml_content, kubeconfig):
            return False

        # Delete existing secrets
        self.k8s.run_kubectl([
            "delete", "secret", f"mongodb-{rs_name}-cert",
            "-n", rs_namespace, "--ignore-not-found"
        ], kubeconfig, check=False)
        self.k8s.run_kubectl([
            "delete", "secret", f"mongodb-{rs_name}-agent-certs",
            "-n", rs_namespace, "--ignore-not-found"
        ], kubeconfig, check=False)

        # Create TLS secrets
        self.k8s.run_kubectl([
            "create", "secret", "tls", f"mongodb-{rs_name}-cert",
            f"--cert={server_cert}",
            f"--key={server_key}",
            "-n", rs_namespace
        ], kubeconfig)

        self.k8s.run_kubectl([
            "create", "secret", "tls", f"mongodb-{rs_name}-agent-certs",
            f"--cert={server_cert}",
            f"--key={server_key}",
            "-n", rs_namespace
        ], kubeconfig)

        logger.info(f"Created TLS secrets on {cluster_name}")
        return True

    def create_mongodb_user(self) -> bool:
        """Create MongoDB SCRAM user."""
        rs_namespace = self.config.rs_namespace
        rs_name = self.rs_config.name
        username = self.rs_config.mongodb_username
        password = self.rs_config.mongodb_password

        if not password:
            password = generate_secure_password()
            self.rs_config.mongodb_password = password
            logger.info(f"Generated MongoDB password for user '{username}'")

        logger.info(f"Creating MongoDB user '{username}'...")

        # Create password secret
        yaml_path = self.yaml_manager.render_user_secret(rs_namespace, password)
        yaml_content = yaml_path.read_text(encoding='utf-8')
        if not self.k8s.apply_yaml(yaml_content, self.k8s.kind_manager.central_kubeconfig):
            return False

        # Create MongoDBUser resource
        yaml_path = self.yaml_manager.render_user(rs_namespace, username, rs_name)
        yaml_content = yaml_path.read_text(encoding='utf-8')
        return self.k8s.apply_yaml(yaml_content, self.k8s.kind_manager.central_kubeconfig)

    def create_mongodb_ca_configmap(self, kubeconfig: Path, cluster_name: str) -> bool:
        """Create the mongodb-ca ConfigMap on a specific cluster."""
        logger.info(f"Creating mongodb-ca ConfigMap on {cluster_name}...")

        ca_cert_path = SCRIPT_DIR / "certs/ca.crt"
        if not ca_cert_path.exists():
            logger.error("CA certificate not found")
            return False

        yaml_path = self.yaml_manager.render_mongodb_ca_configmap(
            self.config.rs_namespace,
            ca_cert_path
        )

        yaml_content = yaml_path.read_text(encoding='utf-8')
        return self.k8s.apply_yaml(yaml_content, kubeconfig)

    def create_ops_manager_ca_configmap(self) -> bool:
        """Create the ops-manager-ca ConfigMap on the central cluster."""
        logger.info("Creating ops-manager-ca ConfigMap...")

        ca_cert_path = SCRIPT_DIR / "certs/ca.crt"
        if not ca_cert_path.exists():
            logger.error("CA certificate not found")
            return False

        yaml_path = self.yaml_manager.render_ca_configmap(
            self.config.rs_namespace,
            ca_cert_path
        )

        yaml_content = yaml_path.read_text(encoding='utf-8')
        return self.k8s.apply_yaml(yaml_content, self.k8s.kind_manager.central_kubeconfig)

    def deploy_multicluster_mongodb(self) -> bool:
        """Deploy the MongoDBMultiCluster resource."""
        logger.info(f"Deploying MongoDBMultiCluster: {self.rs_config.name}")

        # Generate certificates
        if not self.generate_mongodb_certificates():
            return False

        # Create MongoDB CA ConfigMap on both clusters (required for TLS)
        if not self.create_mongodb_ca_configmap(
            self.k8s.kind_manager.central_kubeconfig,
            self.config.central_cluster_name
        ):
            return False

        if not self.create_mongodb_ca_configmap(
            self.k8s.kind_manager.member_kubeconfig,
            self.config.member_cluster_name
        ):
            return False

        # Create Ops Manager CA ConfigMap (required when sslMMSCAConfigMap is set)
        if not self.create_ops_manager_ca_configmap():
            return False

        # Create TLS secrets on both clusters
        if not self.create_tls_secrets_on_cluster(
            self.k8s.kind_manager.central_kubeconfig,
            self.config.central_cluster_name
        ):
            return False

        if not self.create_tls_secrets_on_cluster(
            self.k8s.kind_manager.member_kubeconfig,
            self.config.member_cluster_name
        ):
            return False

        # Generate and apply MongoDBMultiCluster resource
        # Use kind context names (kind-<cluster-name>) which match the kubeconfig contexts
        yaml_path = self.yaml_manager.render_multicluster(
            namespace=self.config.rs_namespace,
            rs_name=self.rs_config.name,
            central_cluster=f"kind-{self.config.central_cluster_name}",
            member_cluster=f"kind-{self.config.member_cluster_name}",
            central_members=self.config.central_members,
            member_members=self.config.member_members,
            central_external_domain=self.config.central_external_domain,
            member_external_domain=self.config.member_external_domain,
            tls_require_valid_certs=not self.config.ssl_skip_verify
        )

        yaml_content = yaml_path.read_text(encoding='utf-8')
        if not self.k8s.apply_yaml(yaml_content, self.k8s.kind_manager.central_kubeconfig):
            return False

        logger.info("MongoDBMultiCluster resource created")

        # Create MongoDB user - brief delay allows operator to initialize the CR
        # before we create the dependent MongoDBUser resource
        time.sleep(5)
        if not self.create_mongodb_user():
            logger.warning("Failed to create MongoDB user, but continuing...")

        return True


# =============================================================================
# Cross-Cluster Networking Manager
# =============================================================================

class CrossClusterNetworkManager:
    """Manages cross-cluster networking for Kind clusters (CoreDNS + iptables)."""

    def __init__(self, k8s: MultiClusterKubernetesManager, config: MultiClusterConfig,
                 yaml_manager: Optional[MultiClusterYAMLManager] = None,
                 rs_name: str = "mongodb-multi-rs"):
        self.k8s = k8s
        self.config = config
        self.yaml_manager = yaml_manager or MultiClusterYAMLManager()
        self.rs_name = rs_name

    def _get_container_ip(self, container_name: str) -> Optional[str]:
        """Get Docker network IP of a container."""
        try:
            result = subprocess.run(
                ["docker", "inspect", "-f",
                 "{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}",
                 container_name],
                capture_output=True, text=True,
                encoding='utf-8', errors='replace', check=True
            )
            return result.stdout.strip() or None
        except subprocess.CalledProcessError:
            return None

    def _get_service_cluster_ips(self, kubeconfig: Path, namespace: str, rs_name: str) -> Dict[str, str]:
        """Get ClusterIP addresses for MongoDB external services."""
        service_ips = {}
        try:
            result = self.k8s.run_kubectl([
                "get", "svc", "-n", namespace,
                "-o", "jsonpath={range .items[*]}{.metadata.name}|{.spec.clusterIP}|{.spec.ports[0].nodePort}\\n{end}"
            ], kubeconfig, check=False)

            if result.returncode == 0:
                for line in result.stdout.strip().split('\n'):
                    if line and '|' in line:
                        parts = line.split('|')
                        if len(parts) >= 3:
                            svc_name, cluster_ip, nodeport = parts[0], parts[1], parts[2]
                            if 'svc-external' in svc_name and cluster_ip != 'None':
                                # Extract pod identifier (e.g., mongodb-multi-rs-0-0 from mongodb-multi-rs-0-0-svc-external)
                                pod_id = svc_name.replace('-svc-external', '')
                                service_ips[pod_id] = {
                                    'clusterIP': cluster_ip,
                                    'nodePort': nodeport
                                }
        except Exception as e:
            logger.warning(f"Failed to get service ClusterIPs: {e}")

        return service_ips

    def _get_nodeports(self, kubeconfig: Path, namespace: str) -> Dict[str, str]:
        """Get NodePort mappings for MongoDB services."""
        nodeports = {}
        try:
            # Use JSON output for reliable parsing
            result = self.k8s.run_kubectl([
                "get", "svc", "-n", namespace, "-o", "json"
            ], kubeconfig, check=False)

            if result.returncode == 0:
                import json
                services = json.loads(result.stdout)
                for svc in services.get('items', []):
                    svc_name = svc.get('metadata', {}).get('name', '')
                    if 'svc-external' in svc_name:
                        ports = svc.get('spec', {}).get('ports', [])
                        if ports and ports[0].get('nodePort'):
                            nodeport = str(ports[0]['nodePort'])
                            pod_id = svc_name.replace('-svc-external', '')
                            nodeports[pod_id] = nodeport
                            logger.debug(f"Found NodePort: {pod_id} -> {nodeport}")

                if nodeports:
                    logger.info(f"Discovered NodePorts: {nodeports}")
        except Exception as e:
            logger.warning(f"Failed to get NodePorts: {e}")

        return nodeports

    def setup_iptables_forwarding(self) -> bool:
        """Set up iptables DNAT rules for cross-cluster traffic."""
        logger.info("Setting up iptables cross-cluster forwarding...")

        central_container = f"{self.config.central_cluster_name}-control-plane"
        member_container = f"{self.config.member_cluster_name}-control-plane"

        central_ip = self._get_container_ip(central_container)
        member_ip = self._get_container_ip(member_container)

        if not central_ip or not member_ip:
            logger.error("Failed to get Docker IPs for Kind clusters")
            return False

        logger.info(f"Docker IPs - Central: {central_ip}, Member: {member_ip}")

        # Get NodePorts from both clusters (services need to exist first)
        central_nodeports = self._get_nodeports(
            self.k8s.kind_manager.central_kubeconfig,
            self.config.rs_namespace
        )
        member_nodeports = self._get_nodeports(
            self.k8s.kind_manager.member_kubeconfig,
            self.config.rs_namespace
        )

        if not central_nodeports and not member_nodeports:
            logger.warning("No NodePorts found yet - services may not be created")
            # Continue anyway - we'll set up what we can

        # Virtual IP ranges for cross-cluster routing
        # Central cluster uses 172.19.0.100-102 for member pods
        # Member cluster uses 172.19.0.200-202 for central pods

        # Set up forwarding on central cluster (to reach member pods)
        central_iptables = self._generate_iptables_script(
            member_ip,
            member_nodeports,
            "172.19.0.100",  # Start IP for member pods
            self.config.member_members
        )

        if central_iptables:
            logger.info("Configuring central cluster iptables...")
            self._run_iptables_on_container(central_container, central_iptables)

        # Set up forwarding on member cluster (to reach central pods)
        member_iptables = self._generate_iptables_script(
            central_ip,
            central_nodeports,
            "172.19.0.200",  # Start IP for central pods
            self.config.central_members
        )

        if member_iptables:
            logger.info("Configuring member cluster iptables...")
            self._run_iptables_on_container(member_container, member_iptables)

        return True

    def _generate_iptables_script(self, target_ip: str, nodeports: Dict[str, str],
                                    start_virtual_ip: str, num_pods: int) -> str:
        """Generate iptables script for cross-cluster forwarding."""
        # Parse start IP to generate virtual IPs
        ip_parts = start_virtual_ip.rsplit('.', 1)
        base_ip = ip_parts[0]
        start_last_octet = int(ip_parts[1])

        script_lines = []

        for i in range(num_pods):
            virtual_ip = f"{base_ip}.{start_last_octet + i}"

            # Add IP alias
            script_lines.append(f"ip addr add {virtual_ip}/32 dev eth0 2>/dev/null || true")

            # Find matching NodePort
            # For member pods: mongodb-multi-rs-1-0, mongodb-multi-rs-1-1
            # For central pods: mongodb-multi-rs-0-0, mongodb-multi-rs-0-1, mongodb-multi-rs-0-2
            pod_suffix = "1" if "100" in start_virtual_ip else "0"  # 100-range = member(1), 200-range = central(0)
            pod_id = f"{self.rs_name}-{pod_suffix}-{i}"

            # Try to find NodePort, use a default range if not found
            nodeport = nodeports.get(pod_id, str(30000 + i))
            logger.info(f"Routing {virtual_ip}:27017 -> {target_ip}:{nodeport} (pod: {pod_id})")

            # First delete any existing rules for this virtual IP (idempotent)
            script_lines.append(
                f"iptables -t nat -D PREROUTING -d {virtual_ip} -p tcp --dport 27017 "
                f"-j DNAT --to-destination {target_ip}:{nodeport} 2>/dev/null || true"
            )
            script_lines.append(
                f"iptables -t nat -D OUTPUT -d {virtual_ip} -p tcp --dport 27017 "
                f"-j DNAT --to-destination {target_ip}:{nodeport} 2>/dev/null || true"
            )

            # Delete any stale rules with old ports for this virtual IP
            for old_port in range(30000, 30010):
                script_lines.append(
                    f"iptables -t nat -D PREROUTING -d {virtual_ip} -p tcp --dport 27017 "
                    f"-j DNAT --to-destination {target_ip}:{old_port} 2>/dev/null || true"
                )
                script_lines.append(
                    f"iptables -t nat -D OUTPUT -d {virtual_ip} -p tcp --dport 27017 "
                    f"-j DNAT --to-destination {target_ip}:{old_port} 2>/dev/null || true"
                )

            # Add DNAT rules with correct NodePort
            script_lines.append(
                f"iptables -t nat -A PREROUTING -d {virtual_ip} -p tcp --dport 27017 "
                f"-j DNAT --to-destination {target_ip}:{nodeport}"
            )
            script_lines.append(
                f"iptables -t nat -A OUTPUT -d {virtual_ip} -p tcp --dport 27017 "
                f"-j DNAT --to-destination {target_ip}:{nodeport}"
            )

        # Add MASQUERADE rule
        script_lines.append(
            "iptables -t nat -C POSTROUTING -o eth0 -j MASQUERADE 2>/dev/null || "
            "iptables -t nat -A POSTROUTING -o eth0 -j MASQUERADE"
        )

        return "\n".join(script_lines)

    def _run_iptables_on_container(self, container_name: str, script: str) -> bool:
        """Run iptables script inside a container."""
        try:
            result = subprocess.run(
                ["docker", "exec", container_name, "bash", "-c", script],
                capture_output=True, text=True,
                encoding='utf-8', errors='replace'
            )
            if result.returncode != 0:
                logger.warning(f"iptables setup warning on {container_name}: {result.stderr}")
            return True
        except subprocess.CalledProcessError as e:
            logger.error(f"Failed to set up iptables on {container_name}: {e}")
            return False

    def _get_pod_ips(self, kubeconfig: Path, namespace: str, rs_name: str, cluster_index: int) -> Dict[str, str]:
        """Get pod IPs for MongoDB pods in a cluster."""
        pod_ips = {}
        try:
            result = self.k8s.run_kubectl([
                "get", "pods", "-n", namespace,
                "-l", "controller=mongodb-enterprise-operator",
                "-o", "jsonpath={range .items[*]}{.metadata.name}|{.status.podIP}\\n{end}"
            ], kubeconfig, check=False)

            if result.returncode == 0:
                for line in result.stdout.strip().split('\n'):
                    if line and '|' in line:
                        pod_name, pod_ip = line.split('|')
                        if pod_ip and f"{rs_name}-{cluster_index}-" in pod_name:
                            pod_ips[pod_name] = pod_ip
        except Exception as e:
            logger.warning(f"Failed to get pod IPs: {e}")

        return pod_ips

    def configure_coredns(self) -> bool:
        """Configure CoreDNS on both clusters for cross-cluster hostname resolution.

        Strategy:
        - LOCAL pods: Use CoreDNS rewrite rules to redirect external domain queries
          to the Kubernetes headless service DNS. This is dynamic - K8s automatically
          updates DNS when pods restart and get new IPs.
          Example: mongodb-multi-rs-0-0.central.mongodb.local
                   -> mongodb-multi-rs-0-0.mongodb-multi-rs-0-svc.mongodb-rs.svc.cluster.local

        - REMOTE pods: Use static virtual IPs in CoreDNS hosts block. These IPs are
          routed via iptables to NodePorts on the remote cluster.
          Example: mongodb-multi-rs-1-0.member1.mongodb.local -> 172.19.0.100

        This approach ensures DNS stays correct even when pods restart and get new IPs,
        without requiring any monitoring or refresh logic in the script.
        """
        logger.info("Configuring CoreDNS for cross-cluster DNS resolution...")

        rs_name = self.rs_name

        # Central cluster CoreDNS configuration:
        # - Rewrite LOCAL (central) pod queries to K8s headless service DNS
        # - Static entries for REMOTE (member) pods via virtual IPs
        central_rewrite_rules = []
        for i in range(self.config.central_members):
            # Rewrite: pod.central.mongodb.local -> pod.mongodb-multi-rs-0-svc.mongodb-rs.svc.cluster.local
            central_rewrite_rules.append(
                f"        rewrite name {rs_name}-0-{i}.{self.config.central_external_domain} "
                f"{rs_name}-0-{i}.{rs_name}-0-svc.{self.config.rs_namespace}.svc.cluster.local"
            )

        central_hosts = []
        for i in range(self.config.member_members):
            virtual_ip = f"172.19.0.{100+i}"
            central_hosts.append(f"            {virtual_ip} {rs_name}-1-{i}.{self.config.member_external_domain}")

        # Member cluster CoreDNS configuration:
        # - Rewrite LOCAL (member) pod queries to K8s headless service DNS
        # - Static entries for REMOTE (central) pods via virtual IPs
        member_rewrite_rules = []
        for i in range(self.config.member_members):
            # Rewrite: pod.member1.mongodb.local -> pod.mongodb-multi-rs-1-svc.mongodb-rs.svc.cluster.local
            member_rewrite_rules.append(
                f"        rewrite name {rs_name}-1-{i}.{self.config.member_external_domain} "
                f"{rs_name}-1-{i}.{rs_name}-1-svc.{self.config.rs_namespace}.svc.cluster.local"
            )

        member_hosts = []
        for i in range(self.config.central_members):
            virtual_ip = f"172.19.0.{200+i}"
            member_hosts.append(f"            {virtual_ip} {rs_name}-0-{i}.{self.config.central_external_domain}")

        # Apply CoreDNS ConfigMap to central cluster
        central_coredns = self._generate_coredns_configmap(
            "\n".join(central_hosts),
            "\n".join(central_rewrite_rules)
        )
        if not self.k8s.apply_yaml(central_coredns, self.k8s.kind_manager.central_kubeconfig):
            logger.error("Failed to apply CoreDNS config to central cluster")
            return False

        # Apply CoreDNS ConfigMap to member cluster
        member_coredns = self._generate_coredns_configmap(
            "\n".join(member_hosts),
            "\n".join(member_rewrite_rules)
        )
        if not self.k8s.apply_yaml(member_coredns, self.k8s.kind_manager.member_kubeconfig):
            logger.error("Failed to apply CoreDNS config to member cluster")
            return False

        # Restart CoreDNS pods to pick up new config
        self._restart_coredns(self.k8s.kind_manager.central_kubeconfig)
        self._restart_coredns(self.k8s.kind_manager.member_kubeconfig)

        logger.info("CoreDNS configured on both clusters")
        return True

    def _generate_coredns_configmap(self, hosts_entries: str, rewrite_rules: str = "") -> str:
        """Generate CoreDNS ConfigMap YAML from template."""
        return self.yaml_manager.render_coredns_configmap(hosts_entries, rewrite_rules)

    def _restart_coredns(self, kubeconfig: Path) -> None:
        """Restart CoreDNS deployment."""
        try:
            self.k8s.run_kubectl([
                "rollout", "restart", "deployment/coredns", "-n", "kube-system"
            ], kubeconfig, check=False)
        except Exception as e:
            logger.warning(f"Failed to restart CoreDNS: {e}")

    def setup_all(self) -> bool:
        """Set up all cross-cluster networking (iptables + CoreDNS)."""
        logger.info("Setting up cross-cluster networking...")

        # Set up iptables forwarding first
        if not self.setup_iptables_forwarding():
            logger.warning("iptables setup incomplete - will retry after services are created")

        # CoreDNS configuration (can work with fallback IPs)
        if not self.configure_coredns():
            logger.warning("CoreDNS setup incomplete - will retry after services are created")

        return True

    def finalize_networking(self) -> bool:
        """Finalize networking after services are created (update with actual NodePorts).

        Note: Caller should ensure services exist before calling this method.
        """
        logger.info("Finalizing cross-cluster networking with actual service NodePorts...")

        # Re-run iptables setup with actual NodePorts
        if not self.setup_iptables_forwarding():
            logger.error("Failed to finalize iptables forwarding")
            return False

        # Re-run CoreDNS configuration with actual ClusterIPs
        if not self.configure_coredns():
            logger.error("Failed to finalize CoreDNS configuration")
            return False

        logger.info("Cross-cluster networking finalized")
        return True


# =============================================================================
# Status Monitor
# =============================================================================

class MultiClusterStatusMonitor:
    """Monitors MongoDB multi-cluster deployment status."""

    def __init__(self, k8s: MultiClusterKubernetesManager, config: MultiClusterConfig, rs_name: str):
        self.k8s = k8s
        self.config = config
        self.rs_name = rs_name

    def get_status(self) -> dict:
        """Get current MongoDBMultiCluster resource status."""
        try:
            result = self.k8s.run_kubectl_central([
                "get", "mongodbmulticluster", self.rs_name,
                "-n", self.config.rs_namespace,
                "-o", "jsonpath={.status.phase},{.status.message}"
            ], check=False)

            if result.returncode != 0:
                return {"phase": "Unknown", "message": "Resource not found"}

            parts = result.stdout.split(",")
            return {
                "phase": parts[0] if len(parts) > 0 else "Unknown",
                "message": parts[1] if len(parts) > 1 else "",
            }
        except Exception as e:
            return {"phase": "Error", "message": str(e)}

    def wait_for_running(self, timeout: int = 600, poll_interval: int = 15) -> bool:
        """Wait for MongoDB to reach Running state."""
        logger.info(f"Waiting for MongoDBMultiCluster {self.rs_name} to reach Running state...")
        logger.info(f"Timeout: {timeout}s, checking every {poll_interval}s")

        start_time = time.time()
        last_phase = None
        last_message = None

        while time.time() - start_time < timeout:
            elapsed = int(time.time() - start_time)
            status = self.get_status()
            phase = status["phase"]
            message = status["message"]

            if phase != last_phase or message != last_message:
                logger.info(f"[{elapsed}s] Phase: {phase}")
                if message and message != last_message:
                    logger.info(f"         Message: {message[:100]}")
                last_phase = phase
                last_message = message

            if phase == "Running":
                logger.info(f"MongoDBMultiCluster {self.rs_name} is Running! (took {elapsed}s)")
                return True

            if phase == "Failed":
                logger.error(f"MongoDB deployment failed: {message}")
                return False

            time.sleep(poll_interval)

        logger.error(f"Timeout waiting for MongoDB to reach Running state after {timeout}s")
        return False


# =============================================================================
# Main
# =============================================================================

def print_instructions(config: MultiClusterConfig, rs_config: ReplicaSetConfig):
    """Print usage instructions."""
    print(f"""
{'='*70}
MULTI-CLUSTER DEPLOYMENT COMPLETE
{'='*70}

Clusters:
  Central: {config.central_cluster_name}
  Member:  {config.member_cluster_name}

Kubeconfig: {Path(config.kubeconfig_dir).resolve() / 'config'}

Namespaces:
  Operator:    {config.operator_namespace} (central only)
  ReplicaSet:  {config.rs_namespace} (both clusters)

MongoDB ReplicaSet: {rs_config.name}
  Central cluster: {config.central_members} members
  Member cluster:  {config.member_members} members

{'='*70}
KUBECTL COMMANDS
{'='*70}

# Use central cluster
kubectl --kubeconfig {Path(config.kubeconfig_dir).resolve() / 'central-config'} get pods -n {config.rs_namespace}

# Use member cluster
kubectl --kubeconfig {Path(config.kubeconfig_dir).resolve() / 'member-config'} get pods -n {config.rs_namespace}

# Check MongoDBMultiCluster status
kubectl --kubeconfig {Path(config.kubeconfig_dir).resolve() / 'central-config'} \\
  get mongodbmulticluster -n {config.rs_namespace}

# Check operator logs
kubectl --kubeconfig {Path(config.kubeconfig_dir).resolve() / 'central-config'} \\
  logs -n {config.operator_namespace} -l app.kubernetes.io/name=mongodb-enterprise-operator

{'='*70}
""")


# check_docker imported from shared.utils


def main():
    parser = argparse.ArgumentParser(
        description="Deploy MongoDB Enterprise Kubernetes Operator across multiple clusters",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Full deployment (uses same config file as single-cluster, but multiCluster project)
  python deploy_mongodb_k8s_multi.py

  # With wait
  python deploy_mongodb_k8s_multi.py --wait

  # Cleanup
  python deploy_mongodb_k8s_multi.py --cleanup

NOTE: Multi-cluster uses different defaults than single-cluster to avoid conflicts:
  - Clusters: mongodb-central, mongodb-member-1 (vs mongodb-k8s)
  - Kubeconfig: .kube-multi/ (vs .kube/)
  - Project: multiCluster (vs singleCluster) - both in same config file
  - RS name: mongodb-multi-rs (vs mongodb-rs)

All tools (kind, kubectl) run via Docker - no local installation required!
"""
    )

    # Cluster options
    cluster_group = parser.add_argument_group("Cluster Options")
    cluster_group.add_argument("--central-cluster-name", default=CENTRAL_CLUSTER_NAME,
                               help=f"Central cluster name (default: {CENTRAL_CLUSTER_NAME})")
    cluster_group.add_argument("--member-cluster-name", default=MEMBER_CLUSTER_NAME,
                               help=f"Member cluster name (default: {MEMBER_CLUSTER_NAME})")
    cluster_group.add_argument("--operator-namespace", type=valid_namespace, default="mongodb",
                               help="Namespace for MongoDB operator (default: mongodb)")
    cluster_group.add_argument("--rs-namespace", type=valid_namespace, default="mongodb-rs",
                               help="Namespace for MongoDB replica set (default: mongodb-rs)")
    cluster_group.add_argument("--kubeconfig-dir", default="./.kube-multi",
                               help="Directory for kubeconfig files")

    # Ops Manager options
    om_group = parser.add_argument_group("Ops Manager Options")
    om_group.add_argument("--api-key-file", default="./ops-manager-api-key.json",
                          help="Path to Ops Manager API key file (uses multiCluster project from config)")
    om_group.add_argument("--ops-manager-url", default="https://host.docker.internal:8443",
                          help="Ops Manager URL (from inside kind cluster)")
    om_group.add_argument("--ssl-skip-verify", action="store_true",
                          help="Skip TLS certificate validation for Ops Manager")

    # Replica set options
    rs_group = parser.add_argument_group("Replica Set Options")
    rs_group.add_argument("--replica-set-name", default="mongodb-multi-rs",
                          help="Replica set name (default: mongodb-multi-rs)")
    rs_group.add_argument("--central-members", type=positive_int, default=3,
                          help="Number of members on central cluster (default: 3)")
    rs_group.add_argument("--member-members", type=positive_int, default=2,
                          help="Number of members on member cluster (default: 2)")

    # Operation modes
    mode_group = parser.add_argument_group("Operation Modes")
    mode_group.add_argument("--cleanup", action="store_true", help="Delete all clusters")
    mode_group.add_argument("--skip-preflight", action="store_true", help="Skip pre-flight checks")
    mode_group.add_argument("--skip-operator", action="store_true", help="Skip operator deployment")
    mode_group.add_argument("--skip-mongodb", action="store_true", help="Skip MongoDB deployment")
    mode_group.add_argument("--cluster-only", action="store_true", help="Only create clusters")
    mode_group.add_argument("--wait", action="store_true", help="Wait for MongoDB to reach Running state")
    mode_group.add_argument("--wait-timeout", type=valid_timeout(60, 1800), default=600,
                            help="Timeout for --wait in seconds (default: 600, min: 60, max: 1800)")
    mode_group.add_argument("--dry-run", action="store_true", help="Show what would be done")

    parser.add_argument("-v", "--verbose", action="store_true", help="Verbose output")

    args = parser.parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    # Build configuration
    config = MultiClusterConfig(
        central_cluster_name=args.central_cluster_name,
        member_cluster_name=args.member_cluster_name,
        operator_namespace=args.operator_namespace,
        rs_namespace=args.rs_namespace,
        ops_manager_url=args.ops_manager_url,
        kubeconfig_dir=args.kubeconfig_dir,
        ssl_skip_verify=args.ssl_skip_verify,
        central_members=args.central_members,
        member_members=args.member_members,
    )

    # Warn about SSL verification bypass
    if config.ssl_skip_verify:
        logger.warning("=" * 60)
        logger.warning("SSL CERTIFICATE VERIFICATION DISABLED")
        logger.warning("This configuration is INSECURE and NOT suitable for production!")
        logger.warning("MITM attacks are possible. Use only for testing/development.")
        logger.warning("=" * 60)

    rs_config = ReplicaSetConfig(
        name=args.replica_set_name,
    )

    # Dry-run mode
    if args.dry_run:
        logger.info("=== DRY-RUN MODE ===")
        logger.info("The following actions would be performed:")
        logger.info(f"  1. Create kind cluster: {config.central_cluster_name} (central)")
        logger.info(f"  2. Create kind cluster: {config.member_cluster_name} (member)")
        if not args.skip_operator:
            logger.info(f"  3. Deploy MongoDB Operator to central cluster")
            logger.info(f"  4. Deploy member cluster resources")
        if not args.skip_mongodb:
            logger.info(f"  5. Deploy MongoDBMultiCluster: {rs_config.name}")
        logger.info("=== END DRY-RUN ===")
        return

    # Pre-flight checks
    if not args.skip_preflight and not args.cleanup and not args.cluster_only:
        preflight = PreFlightChecker(
            ops_manager_url=config.ops_manager_url,
            credentials_file=args.api_key_file,
        )
        if not preflight.check_all():
            logger.error("Pre-flight checks failed. Use --skip-preflight to bypass.")
            sys.exit(1)

    if not check_docker():
        sys.exit(1)

    # Initialize managers
    yaml_manager = MultiClusterYAMLManager()
    kind_manager = MultiClusterKindManager(config, yaml_manager)
    k8s_manager = MultiClusterKubernetesManager(config, kind_manager)
    network_manager = CrossClusterNetworkManager(k8s_manager, config, yaml_manager, rs_config.name)

    # Cleanup
    if args.cleanup:
        # Clean up Ops Manager project first (removes stale automation config)
        logger.info("Cleaning up Ops Manager project...")
        cleanup_ops_manager_project(
            api_key_file=args.api_key_file,
            project_type="multiCluster",
            verify_ssl=not config.ssl_skip_verify
        )
        kind_manager.delete_all_clusters()
        return

    # Create clusters
    logger.info("Creating Kubernetes clusters via kind...")
    if not kind_manager.create_all_clusters():
        logger.error("Failed to create kind clusters")
        sys.exit(1)

    if args.cluster_only:
        print_instructions(config, rs_config)
        return

    # Load credentials
    try:
        credentials = OpsManagerCredentials.from_file(args.api_key_file, project_type="multiCluster")
        logger.info(f"Loaded Ops Manager credentials (multiCluster project) from: {args.api_key_file}")
    except FileNotFoundError:
        logger.error(f"API key file not found: {args.api_key_file}")
        logger.error("Run deploy_ops_manager.py first to generate credentials")
        sys.exit(1)
    except (json.JSONDecodeError, KeyError, ValueError) as e:
        logger.error(f"Failed to parse credentials file: {e}")
        sys.exit(1)
    except Exception as e:
        logger.error(f"Unexpected error loading credentials: {e}")
        sys.exit(1)

    # Download and install kubectl-mongodb plugin
    kubectl_plugin = KubectlMongoDBPlugin(kind_manager.kubeconfig_path)
    if not kubectl_plugin.download_and_install():
        logger.warning("Failed to install kubectl-mongodb plugin, continuing without it...")
        kubectl_plugin = None

    # Deploy operator
    if not args.skip_operator:
        operator_deployer = MultiClusterOperatorDeployer(
            k8s_manager, credentials, config, yaml_manager, kubectl_plugin
        )
        if not operator_deployer.deploy_all():
            logger.error("Failed to deploy MongoDB operator")
            sys.exit(1)
        logger.info("MongoDB Enterprise Kubernetes Operator (multi-cluster) deployed successfully")

    # Set up initial cross-cluster networking (iptables + CoreDNS with fallback IPs)
    # This is done before MongoDB deployment to ensure DNS resolution is available
    logger.info("Setting up cross-cluster networking...")
    network_manager.setup_all()

    # Deploy MongoDB
    if not args.skip_mongodb:
        mongodb_deployer = MultiClusterMongoDBDeployer(k8s_manager, credentials, config, rs_config, yaml_manager)
        if not mongodb_deployer.deploy_multicluster_mongodb():
            logger.error("Failed to deploy MongoDB")
            sys.exit(1)
        logger.info("MongoDBMultiCluster deployment initiated")

        # Wait for services to be created, then finalize networking with actual NodePorts/ClusterIPs
        # Calculate expected service count: 1 per pod + external services
        expected_services = rs_config.members_per_cluster.get('central', 0) + rs_config.members_per_cluster.get('member1', 0)
        k8s_manager.wait_for_mongodb_services(
            rs_name=rs_config.name,
            namespace=config.rs_namespace,
            expected_count=expected_services,
            timeout=120
        )
        network_manager.finalize_networking()

        if args.wait:
            monitor = MultiClusterStatusMonitor(k8s_manager, config, rs_config.name)
            if not monitor.wait_for_running(timeout=args.wait_timeout):
                logger.error("MongoDB did not reach Running state within timeout")
                sys.exit(1)

    print_instructions(config, rs_config)


if __name__ == "__main__":
    main()
