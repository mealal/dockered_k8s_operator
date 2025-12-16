#!/usr/bin/env python3
"""
MongoDB Enterprise Kubernetes Operator Deployment

This script:
1. Deploys a Kubernetes cluster in Docker using kind (via Docker container)
2. Deploys the MongoDB Enterprise Kubernetes Operator
3. Configures the operator to connect to Ops Manager
4. Deploys a 3-node MongoDB replica set

Prerequisites:
- Docker running
- Output from deploy_ops_manager.py (ops-manager-api-key.json)
- Ops Manager running and accessible

All tools (kind, kubectl) run inside Docker containers - no local installation required.
"""

from __future__ import annotations

import subprocess
import json
import sys
import time
import argparse
import logging
import os
import re
from pathlib import Path
from dataclasses import dataclass
from typing import Optional, List, Dict

# Import shared modules
from shared.validators import positive_int, non_negative_int, valid_port, valid_namespace, valid_timeout
from shared.utils import run_command, convert_path_for_docker, check_docker, find_openssl, run_openssl, generate_secure_password
from shared.models import OpsManagerCredentials
from shared.preflight import PreFlightChecker
from shared.decorators import retry_with_backoff
from shared.yaml_manager_base import BaseYAMLTemplateManager
from shared.kind_manager_base import BaseKindManager
from shared.k8s_manager_base import BaseKubernetesManager
from shared.operator_deployer_base import BaseOperatorDeployer
from shared.ops_manager_cleanup import cleanup_ops_manager_project
from shared.certificate_manager import CertificateManager, generate_single_cluster_sans
from shared.x509_manager import X509CertificateManager
from shared.health_check import MongoDBHealthChecker, format_health_check_result
from shared.ui_utils import (
    mask_password,
    format_error_with_suggestion,
    print_step,
    CountdownTimer,
)
from shared import constants
from shared.cleanup import cleanup_generated_files as _cleanup_generated_files

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger(__name__)

# Script directory for resolving relative paths
SCRIPT_DIR = Path(__file__).parent.resolve()

# Constants - imported from shared.constants for single source of truth
KIND_NODE_IMAGE = constants.KIND_NODE_IMAGE
KUBECTL_IMAGE = constants.KUBECTL_DOCKER_IMAGE
OPERATOR_VERSION = constants.DEFAULT_OPERATOR_VERSION
OPERATOR_CRDS_URL = constants.OPERATOR_CRDS_URL
OPERATOR_INSTALL_URL = constants.OPERATOR_INSTALL_URL

# YAML template directory
K8S_YAML_DIR = SCRIPT_DIR / constants.SINGLE_CLUSTER_TEMPLATES

# Template file names
KIND_CLUSTER_CONFIG_TEMPLATE = "kind-cluster-config.yaml"


# =============================================================================
# Argument Validation Helpers (imported from shared.validators)
# =============================================================================

# positive_int, non_negative_int, valid_port, valid_namespace are imported from shared.validators


# =============================================================================
# Cleanup Helpers
# =============================================================================

def cleanup_generated_files(kubeconfig_dir: str = "./.kube") -> None:
    """Clean up generated files from K8s single-cluster deployment.

    This is a wrapper around shared.cleanup.cleanup_generated_files for
    backwards compatibility. Uses the consolidated cleanup module.

    Cleans up:
    - k8s/generated/ directory contents
    - certs/mongodb/ directory contents (MongoDB TLS certs)
    - .kube/ directory contents (kubeconfig files, kind configs)

    Does NOT clean up:
    - certs/ca.crt and certs/ca.key (managed by deploy_ops_manager.py)

    Args:
        kubeconfig_dir: Directory containing kubeconfig files (deprecated, ignored)
    """
    _cleanup_generated_files(SCRIPT_DIR, multi_cluster=False)


# =============================================================================
# YAML Template Manager
# =============================================================================

class YAMLTemplateManager(BaseYAMLTemplateManager):
    """Manages YAML template files for single-cluster Kubernetes resources.

    Inherits common functionality from BaseYAMLTemplateManager and adds
    single-cluster specific methods for replica set configuration.
    """

    def __init__(self, template_dir: Path = K8S_YAML_DIR):
        super().__init__(template_dir)

    def render_operator_namespace(self, namespace: str) -> Path:
        """Render operator namespace.yaml."""
        return self.render_namespace(namespace, "namespace.yaml", "namespace.yaml")

    def render_rs_namespace(self, namespace: str) -> Path:
        """Render replica set namespace.yaml."""
        return self.render_namespace(namespace, "mongodb-rs-namespace.yaml", "mongodb-rs-namespace.yaml")

    def render_replicaset(self, namespace: str) -> Path:
        """Render mongodb-replicaset.yaml.

        Security features (TLS, SCRAM+X509 auth, external access via NodePort) are always enabled.
        Static configuration values (rs_name, members, version, resources, ports, auth modes, horizons)
        are defined in the template and extracted at runtime where needed.

        Args:
            namespace: Kubernetes namespace for the deployment (used for namespace: field replacement)
        """
        return self._render_with_namespace(
            "mongodb-replicaset.yaml",
            {},  # REPLICA_SET_NAME, TLS_REQUIRE_VALID_CERTS, SSL_REQUIRE_VALID_MMS_CERTS hardcoded in template
            namespace=namespace
        )

    def get_external_hosts(self) -> str:
        """Extract external hosts from the replicaset template.

        Parses the connectivity.replicaSetHorizons from the template to get
        the external hostname:port combinations for connection strings.

        Returns:
            Comma-separated list of external hosts (e.g., "localhost:30000,localhost:30001,localhost:30002")
        """
        template_path = self.template_dir / "mongodb-replicaset.yaml"
        content = template_path.read_text(encoding='utf-8')

        # Extract external hosts from replicaSetHorizons
        # Pattern matches: - "external": "localhost:30000"
        pattern = r'"external":\s*"([^"]+)"'
        matches = re.findall(pattern, content)

        return ",".join(matches) if matches else "localhost:27017"

    def get_members_count(self) -> int:
        """Extract members count from the replicaset template.

        Returns:
            Number of replica set members
        """
        template_path = self.template_dir / "mongodb-replicaset.yaml"
        content = template_path.read_text(encoding='utf-8')

        # Pattern matches: members: 3
        pattern = r'^\s*members:\s*(\d+)'
        match = re.search(pattern, content, re.MULTILINE)

        return int(match.group(1)) if match else 3

    def get_external_ports(self) -> List[int]:
        """Extract external ports from the replicaset template.

        Parses the connectivity.replicaSetHorizons from the template to get
        the port numbers for external access.

        Returns:
            List of port numbers (e.g., [30000, 30001, 30002])
        """
        template_path = self.template_dir / "mongodb-replicaset.yaml"
        content = template_path.read_text(encoding='utf-8')

        # Extract ports from replicaSetHorizons
        # Pattern matches: - "external": "localhost:30000"
        pattern = r'"external":\s*"[^:]+:(\d+)"'
        matches = re.findall(pattern, content)

        return [int(port) for port in matches] if matches else [27017]


# =============================================================================
# Utility Functions (imported from shared modules)
# =============================================================================

# retry_with_backoff imported from shared.decorators
# PreFlightChecker imported from shared.preflight
# OpsManagerCredentials imported from shared.models


@dataclass
class ClusterConfig:
    """Configuration for the Kubernetes cluster.

    Namespace values are hardcoded to match the YAML templates:
    - operator_namespace: 'mongodb' (k8s/namespace.yaml)
    - rs_namespace: 'eksrsoppoc1d' (k8s/mongodb-rs-namespace.yaml)
    """
    name: str = "mongodb-k8s"
    # Hardcoded values matching templates - do not change without updating templates
    operator_namespace: str = "mongodb"      # Matches k8s/namespace.yaml
    rs_namespace: str = "eksrsoppoc1d"       # Matches k8s/mongodb-rs-namespace.yaml
    # User-configurable values
    worker_nodes: int = 1
    ops_manager_url: str = "https://host.docker.internal:8443"
    kubeconfig_dir: str = "./.kube"
    ssl_skip_verify: bool = False            # Skip TLS cert validation for Ops Manager

    @property
    def namespace(self) -> str:
        """Backward compatibility: returns operator namespace."""
        return self.operator_namespace


# generate_secure_password imported from shared.utils


@dataclass
class ReplicaSetConfig:
    """Configuration for the MongoDB replica set.

    Security features (TLS, SCRAM+X509 auth, external access via NodePort) are always enabled.
    Static configuration values (name, username, members, version, resources, ports) are
    hardcoded in the YAML template at k8s/mongodb-replicaset.yaml for consistency.
    """
    # Hardcoded values matching templates - do not change without updating templates
    name: str = "mongodb-rs"  # Matches k8s/mongodb-replicaset.yaml
    mongodb_username: str = "admin"  # Matches k8s/mongodb-user.yaml
    # User-configurable values
    mongodb_password: Optional[str] = None
    x509_subject_dn: Optional[str] = None  # Set after X509 user creation


class MongoDBStatusMonitor:
    """Monitors MongoDB deployment status with progress updates."""

    def __init__(self, k8s: 'KubernetesManager', namespace: str, rs_name: str):
        self.k8s = k8s
        self.namespace = namespace
        self.rs_name = rs_name

    def get_status(self) -> dict:
        """Get current MongoDB resource status."""
        try:
            result = self.k8s.run_kubectl([
                "get", "mongodb", self.rs_name,
                "-n", self.namespace,
                "-o", "jsonpath={.status.phase},{.status.message},{.spec.members}"
            ], check=False)

            if result.returncode != 0:
                return {"phase": "Unknown", "message": "Resource not found", "members": 0}

            parts = result.stdout.split(",")
            return {
                "phase": parts[0] if len(parts) > 0 else "Unknown",
                "message": parts[1] if len(parts) > 1 else "",
                "members": int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 0
            }
        except Exception as e:
            return {"phase": "Error", "message": str(e), "members": 0}

    def get_pod_status(self) -> dict:
        """Get status of MongoDB pods."""
        try:
            result = self.k8s.run_kubectl([
                "get", "pods",
                "-n", self.namespace,
                "-l", f"app={self.rs_name}-svc",
                "-o", "jsonpath={range .items[*]}{.metadata.name}:{.status.phase}:{.status.containerStatuses[0].ready} {end}"
            ], check=False)

            pods = {}
            if result.returncode == 0 and result.stdout.strip():
                for pod_info in result.stdout.strip().split():
                    parts = pod_info.split(":")
                    if len(parts) >= 3:
                        pods[parts[0]] = {
                            "phase": parts[1],
                            "ready": parts[2].lower() == "true"
                        }
            return pods
        except subprocess.TimeoutExpired:
            logger.debug("Timeout getting pod status")
            return {}
        except subprocess.CalledProcessError as e:
            logger.debug(f"Failed to get pod status: {e}")
            return {}
        except (ValueError, IndexError, KeyError) as e:
            logger.debug(f"Error parsing pod status: {e}")
            return {}

    def wait_for_running(self, timeout: int = 600, poll_interval: int = 15) -> bool:
        """Wait for MongoDB to reach Running state with progress updates."""
        logger.info(f"Waiting for MongoDB {self.rs_name} to reach Running state...")
        logger.info(f"Timeout: {timeout}s, checking every {poll_interval}s")

        start_time = time.time()
        last_phase = None
        last_message = None

        while time.time() - start_time < timeout:
            elapsed = int(time.time() - start_time)
            status = self.get_status()
            phase = status["phase"]
            message = status["message"]
            pods = self.get_pod_status()

            # Log status change
            if phase != last_phase or message != last_message:
                ready_pods = sum(1 for p in pods.values() if p.get("ready"))
                total_pods = len(pods)
                logger.info(f"[{elapsed}s] Phase: {phase} | Pods: {ready_pods}/{total_pods} ready")
                if message and message != last_message:
                    logger.info(f"         Message: {message[:100]}")
                last_phase = phase
                last_message = message

            if phase == "Running":
                logger.info(f"MongoDB {self.rs_name} is Running! (took {elapsed}s)")
                return True

            if phase == "Failed":
                logger.error(f"MongoDB deployment failed: {message}")
                return False

            time.sleep(poll_interval)

        logger.error(f"Timeout waiting for MongoDB to reach Running state after {timeout}s")
        return False


# run_command and convert_path_for_docker imported from shared.utils


class KindManager(BaseKindManager):
    """Manages kind Kubernetes cluster for single-cluster deployment.

    Inherits common functionality from BaseKindManager and adds
    single-cluster specific configuration handling.
    """

    def __init__(self, config: ClusterConfig, yaml_manager: Optional[YAMLTemplateManager] = None):
        self.config = config
        self.yaml_manager = yaml_manager or YAMLTemplateManager()
        self.kubeconfig_file = Path(config.kubeconfig_dir).resolve() / "config"

        # Initialize base class
        super().__init__(Path(config.kubeconfig_dir))

    def cluster_exists(self, cluster_name: str = None) -> bool:
        """Check if a cluster already exists.

        Args:
            cluster_name: Name of cluster to check (defaults to configured cluster)
        """
        name = cluster_name if cluster_name is not None else self.config.name
        return super().cluster_exists(name)

    def create_cluster(self) -> bool:
        """Create the kind cluster with port mappings from template."""
        # Extract ports from template
        external_ports = self.yaml_manager.get_external_ports()

        # Generate config content
        config_content = self.yaml_manager.render_kind_cluster_config(external_ports)

        # Add worker nodes if configured
        for _ in range(self.config.worker_nodes):
            config_content += "  - role: worker\n"

        return self.create_cluster_with_config(
            self.config.name,
            config_content,
            self.kubeconfig_file
        )

    def delete_cluster(self) -> bool:
        """Delete the kind cluster."""
        return super().delete_cluster(self.config.name)


class KubernetesManager(BaseKubernetesManager):
    """Manages Kubernetes resources for single-cluster deployment.

    Inherits common functionality from BaseKubernetesManager and adds
    single-cluster specific convenience methods.
    """

    def __init__(self, config: ClusterConfig):
        super().__init__(kubectl_image=KUBECTL_IMAGE)
        self.config = config
        self.kubeconfig_path = Path(config.kubeconfig_dir).resolve()
        self.kubeconfig_file = self.kubeconfig_path / "config"

    def run_kubectl(self, args: List[str], kubeconfig: Path = None,
                    check: bool = True, input_data: Optional[str] = None,
                    timeout: int = 120) -> subprocess.CompletedProcess:
        """Run kubectl command using the configured kubeconfig.

        Args:
            args: kubectl arguments
            kubeconfig: Ignored - uses configured kubeconfig (for API compatibility)
            check: If True, raise on non-zero exit
            input_data: Optional stdin data
            timeout: Command timeout in seconds
        """
        return super().run_kubectl(args, self.kubeconfig_file, check, input_data, timeout)

    def create_namespace(self, namespace: Optional[str] = None) -> bool:
        """Create a Kubernetes namespace if it doesn't exist."""
        ns = namespace or self.config.operator_namespace
        logger.info(f"Creating namespace: {ns}")
        try:
            result = self.run_kubectl(["get", "namespace", ns], check=False)
            if result.returncode == 0:
                logger.info(f"Namespace {ns} already exists")
                return True
            self.run_kubectl(["create", "namespace", ns])
            logger.info(f"Namespace {ns} created")
            return True
        except Exception as e:
            logger.error(f"Failed to create namespace: {e}")
            return False

    def create_operator_namespace(self) -> bool:
        """Create the operator namespace (mongodb)."""
        return self.create_namespace(self.config.operator_namespace)

    def create_rs_namespace(self) -> bool:
        """Create the replica set namespace (mongodb-rs)."""
        return self.create_namespace(self.config.rs_namespace)

    def apply_yaml(self, yaml_content: str, namespace: Optional[str] = None) -> bool:
        """Apply YAML configuration."""
        return super().apply_yaml(yaml_content, self.kubeconfig_file, namespace)

    def wait_for_deployment(self, name: str, namespace: str, timeout: int = 300) -> bool:
        """Wait for a deployment to be ready."""
        return super().wait_for_deployment(name, namespace, self.kubeconfig_file, timeout)

    def wait_for_pods(self, label: str, namespace: str, expected: int, timeout: int = 300) -> bool:
        """Wait for pods to be ready."""
        return super().wait_for_pods(label, namespace, self.kubeconfig_file, expected, timeout)


class MongoDBOperatorDeployer:
    """Deploys MongoDB Enterprise Kubernetes Operator."""

    def __init__(self, k8s: KubernetesManager, credentials: OpsManagerCredentials,
                 cluster_config: ClusterConfig, yaml_manager: Optional[YAMLTemplateManager] = None):
        self.k8s = k8s
        self.credentials = credentials
        self.cluster_config = cluster_config
        self.yaml_manager = yaml_manager or YAMLTemplateManager()

    @retry_with_backoff(max_retries=3, exceptions=(subprocess.CalledProcessError,))
    def create_ops_manager_secret(self) -> bool:
        """Create or update secret for Ops Manager API credentials.

        This method is idempotent - it will update the secret if it already exists.
        Uses YAML template from k8s/ops-manager-secret.yaml
        Note: Secret is created in rs_namespace where MongoDB pods run.
        """
        rs_namespace = self.cluster_config.rs_namespace
        logger.info(f"Creating/updating Ops Manager credentials secret in {rs_namespace}...")

        # Delete existing secret first to ensure clean state (secrets can't be easily updated)
        self.k8s.run_kubectl([
            "delete", "secret", "ops-manager-admin-key",
            "-n", rs_namespace,
            "--ignore-not-found"
        ], check=False)

        # Generate YAML from template - use rs_namespace for MongoDB pods
        yaml_path = self.yaml_manager.render_secret(
            namespace=rs_namespace,
            public_key=self.credentials.public_key,
            private_key=self.credentials.private_key
        )

        logger.info(f"Generated secret YAML: {yaml_path}")
        yaml_content = yaml_path.read_text(encoding='utf-8')
        return self.k8s.apply_yaml(yaml_content)

    @retry_with_backoff(max_retries=3, exceptions=(subprocess.CalledProcessError,))
    def create_ops_manager_configmap(self) -> bool:
        """Create or update ConfigMap for Ops Manager connection.

        This method is idempotent - kubectl apply will update if exists.
        Uses YAML template from k8s/ops-manager-configmap.yaml
        Note: ConfigMap is created in rs_namespace where MongoDB pods run.
        """
        rs_namespace = self.cluster_config.rs_namespace
        logger.info(f"Creating/updating Ops Manager ConfigMap in {rs_namespace}...")

        # Validate project_id is set - prevents operator from creating duplicate projects
        if not self.credentials.project_id:
            logger.error("Project ID is not set. Cannot create ConfigMap without valid project ID.")
            return False

        logger.info(f"Using project: {self.credentials.project_name} (ID: {self.credentials.project_id})")

        # Generate YAML from template - use rs_namespace for MongoDB pods
        # Use both projectName and projectId to ensure correct project
        yaml_path = self.yaml_manager.render_configmap(
            namespace=rs_namespace,
            base_url=self.cluster_config.ops_manager_url,
            project_id=self.credentials.project_id,
            org_id=self.credentials.org_id,
            project_name=self.credentials.project_name,
            ssl_require_valid_certs=not self.cluster_config.ssl_skip_verify
        )

        logger.info(f"Generated configmap YAML: {yaml_path}")
        yaml_content = yaml_path.read_text(encoding='utf-8')
        return self.k8s.apply_yaml(yaml_content)

    @retry_with_backoff(max_retries=3, exceptions=(subprocess.CalledProcessError,))
    def create_ops_manager_ca_configmap(self) -> bool:
        """Create or update ConfigMap for Ops Manager CA certificate.

        This method is idempotent - kubectl apply will update if exists.
        Uses YAML template from k8s/ops-manager-ca-configmap.yaml
        Note: ConfigMap is created in rs_namespace where MongoDB pods run.
        """
        rs_namespace = self.cluster_config.rs_namespace
        logger.info(f"Creating/updating Ops Manager CA ConfigMap in {rs_namespace}...")

        # Read the CA certificate from the certs directory
        ca_cert_path = SCRIPT_DIR / "certs/ca.crt"
        if not ca_cert_path.exists():
            logger.error("CA certificate not found. Run deploy_ops_manager.py first.")
            return False

        # Generate YAML from template - use rs_namespace for MongoDB pods
        yaml_path = self.yaml_manager.render_ca_configmap(
            namespace=rs_namespace,
            ca_cert_path=ca_cert_path
        )

        logger.info(f"Generated CA configmap YAML: {yaml_path}")
        yaml_content = yaml_path.read_text(encoding='utf-8')
        return self.k8s.apply_yaml(yaml_content)

    def deploy_crds(self) -> bool:
        """Deploy MongoDB CRDs from official GitHub repository."""
        logger.info("Deploying MongoDB CRDs from official repository...")

        try:
            self.k8s.run_kubectl(["apply", "-f", OPERATOR_CRDS_URL])
            logger.info("CRDs deployed successfully")
            return True
        except Exception as e:
            logger.error(f"Failed to deploy CRDs: {e}")
            return False

    def deploy_operator(self) -> bool:
        """Deploy the MongoDB Enterprise Kubernetes Operator from official GitHub repository."""
        logger.info(f"Deploying MongoDB Enterprise Kubernetes Operator v{OPERATOR_VERSION}...")
        logger.info(f"Using official installation from: {OPERATOR_INSTALL_URL}")

        try:
            # Apply the official operator YAML from GitHub (installs to 'mongodb' namespace by default)
            self.k8s.run_kubectl(["apply", "-f", OPERATOR_INSTALL_URL])
            logger.info("Operator resources applied successfully")

            # Configure operator to watch the replica set namespace
            rs_namespace = self.cluster_config.rs_namespace
            operator_namespace = self.cluster_config.operator_namespace
            logger.info(f"Configuring operator to watch namespace: {rs_namespace}")

            # Set WATCH_NAMESPACE environment variable
            self.k8s.run_kubectl([
                "set", "env", "deployment/mongodb-enterprise-operator",
                "-n", operator_namespace,
                f"WATCH_NAMESPACE={rs_namespace}"
            ])
            logger.info(f"Operator configured to watch {rs_namespace} namespace")

            return True
        except Exception as e:
            logger.error(f"Failed to deploy operator: {e}")
            return False

    def deploy_operator_rbac_for_rs_namespace(self) -> bool:
        """Create RBAC for operator to access the replica set namespace.

        Uses YAML template from k8s/operator-rbac.yaml
        """
        rs_namespace = self.cluster_config.rs_namespace
        operator_namespace = self.cluster_config.operator_namespace
        logger.info(f"Creating RBAC for operator in {rs_namespace} namespace...")

        # Generate YAML from template
        yaml_path = self.yaml_manager.render_operator_rbac()

        logger.info(f"Generated operator RBAC YAML: {yaml_path}")
        yaml_content = yaml_path.read_text(encoding='utf-8')
        return self.k8s.apply_yaml(yaml_content)

    def deploy_database_roles(self) -> bool:
        """Deploy roles for MongoDB database pods in rs_namespace.

        Uses YAML template from k8s/database-roles.yaml
        """
        rs_namespace = self.cluster_config.rs_namespace
        logger.info(f"Deploying MongoDB database roles in {rs_namespace}...")

        # Generate YAML from template
        yaml_path = self.yaml_manager.render_database_roles()

        logger.info(f"Generated database roles YAML: {yaml_path}")
        yaml_content = yaml_path.read_text(encoding='utf-8')
        return self.k8s.apply_yaml(yaml_content)

    def deploy_all(self) -> bool:
        """Deploy all operator components.

        Creates two namespaces:
        - operator_namespace (mongodb): For the operator deployment
        - rs_namespace (mongodb-rs): For replica set, secrets, and configmaps
        """
        # Note: The official mongodb-enterprise.yaml already includes:
        # - Service accounts (operator, appdb, database-pods, ops-manager)
        # - ClusterRoles and RoleBindings
        # - The operator deployment (in 'mongodb' namespace by default)
        steps = [
            ("Creating operator namespace", lambda: self.k8s.create_operator_namespace()),
            ("Creating replica set namespace", lambda: self.k8s.create_rs_namespace()),
            ("Deploying CRDs", self.deploy_crds),
            ("Deploying operator", self.deploy_operator),
            ("Creating operator RBAC for RS namespace", self.deploy_operator_rbac_for_rs_namespace),
            ("Deploying database roles in RS namespace", self.deploy_database_roles),
            ("Creating Ops Manager secret", self.create_ops_manager_secret),
        ]

        # Only create CA ConfigMap if SSL verification is enabled
        if not self.cluster_config.ssl_skip_verify:
            steps.append(("Creating Ops Manager CA ConfigMap", self.create_ops_manager_ca_configmap))

        steps.append(("Creating Ops Manager ConfigMap", self.create_ops_manager_configmap))

        for step_name, step_func in steps:
            logger.info(f"Step: {step_name}")
            if not step_func():
                logger.error(f"Failed at step: {step_name}")
                return False
            time.sleep(2)

        # Wait for operator to be ready (in operator namespace)
        return self.k8s.wait_for_deployment(
            "mongodb-enterprise-operator",
            self.cluster_config.operator_namespace,
            timeout=180
        )


class MongoDBReplicaSetDeployer:
    """Deploys MongoDB replica set."""

    def __init__(self, k8s: KubernetesManager, credentials: OpsManagerCredentials,
                 cluster_config: ClusterConfig, rs_config: ReplicaSetConfig,
                 yaml_manager: Optional[YAMLTemplateManager] = None):
        self.k8s = k8s
        self.credentials = credentials
        self.cluster_config = cluster_config
        self.rs_config = rs_config
        self.yaml_manager = yaml_manager or YAMLTemplateManager()

    def generate_mongodb_certificates(self) -> bool:
        """Generate TLS certificates for MongoDB pods.

        Creates certificates with SANs for the MongoDB pod hostnames:
        - mongodb-rs-0.mongodb-rs-svc.eksrsoppoc1d.svc.cluster.local
        - mongodb-rs-1.mongodb-rs-svc.eksrsoppoc1d.svc.cluster.local
        - etc.

        External access is always enabled with localhost, also includes:
        - External domain (localhost)
        - IP SANs for loopback addresses
        """
        rs_namespace = self.cluster_config.rs_namespace
        rs_name = self.rs_config.name
        certs_dir = SCRIPT_DIR / "certs/mongodb"

        logger.info(f"Generating MongoDB TLS certificates for {rs_name}...")

        # Use shared certificate manager
        cert_manager = CertificateManager(SCRIPT_DIR / "certs")

        # Generate SANs using shared helper
        members = self.yaml_manager.get_members_count()
        dns_sans, ip_sans = generate_single_cluster_sans(
            rs_name=rs_name,
            rs_namespace=rs_namespace,
            members_count=members,
            include_localhost=True
        )

        return cert_manager.generate_mongodb_certificate(
            output_dir=certs_dir,
            cn=rs_name,
            dns_sans=dns_sans,
            ip_sans=ip_sans
        )

    def create_mongodb_tls_secrets(self) -> bool:
        """Create TLS certificate secrets for MongoDB.

        Creates:
        - mongodb-ca ConfigMap with CA certificate
        - mongodb-<rs-name>-cert Secret with server certificate
        - mongodb-<rs-name>-agent-certs Secret with agent certificate
        """
        rs_namespace = self.cluster_config.rs_namespace
        rs_name = self.rs_config.name

        # First generate MongoDB-specific certificates
        if not self.generate_mongodb_certificates():
            logger.error("Failed to generate MongoDB certificates")
            return False

        certs_dir = SCRIPT_DIR / "certs"
        mongodb_certs_dir = SCRIPT_DIR / "certs/mongodb"

        logger.info(f"Creating MongoDB TLS secrets in {rs_namespace}...")

        # Use the CA from the main certs directory
        ca_cert = certs_dir / "ca.crt"
        # Use MongoDB-specific certificates
        server_cert = mongodb_certs_dir / "mongodb.crt"
        server_key = mongodb_certs_dir / "mongodb.key"

        if not all(p.exists() for p in [ca_cert, server_cert, server_key]):
            logger.error("TLS certificates not found")
            logger.error(f"Required files: {ca_cert}, {server_cert}, {server_key}")
            return False

        # Create CA ConfigMap using template (mongodb-ca with key 'ca-pem')
        yaml_path = self.yaml_manager.render_mongodb_ca_configmap(
            ca_cert_path=ca_cert
        )
        logger.info(f"Generated mongodb-ca ConfigMap YAML: {yaml_path}")
        yaml_content = yaml_path.read_text(encoding='utf-8')
        if not self.k8s.apply_yaml(yaml_content):
            logger.error("Failed to create mongodb-ca ConfigMap")
            return False
        logger.info("Created mongodb-ca ConfigMap")

        # Create server certificate secret
        cert_content = server_cert.read_text(encoding='utf-8')
        key_content = server_key.read_text(encoding='utf-8')

        # Delete existing secrets first
        self.k8s.run_kubectl([
            "delete", "secret", f"mongodb-{rs_name}-cert",
            "-n", rs_namespace, "--ignore-not-found"
        ], check=False)
        self.k8s.run_kubectl([
            "delete", "secret", f"mongodb-{rs_name}-agent-certs",
            "-n", rs_namespace, "--ignore-not-found"
        ], check=False)

        # Create TLS secret for MongoDB server
        self.k8s.run_kubectl([
            "create", "secret", "tls", f"mongodb-{rs_name}-cert",
            f"--cert={server_cert}",
            f"--key={server_key}",
            "-n", rs_namespace
        ])
        logger.info(f"Created mongodb-{rs_name}-cert Secret")

        # Create TLS secret for agent (same cert for simplicity)
        self.k8s.run_kubectl([
            "create", "secret", "tls", f"mongodb-{rs_name}-agent-certs",
            f"--cert={server_cert}",
            f"--key={server_key}",
            "-n", rs_namespace
        ])
        logger.info(f"Created mongodb-{rs_name}-agent-certs Secret")

        return True

    def _indent_text(self, text: str, spaces: int) -> str:
        """Indent text by specified number of spaces."""
        indent = " " * spaces
        return "\n".join(indent + line for line in text.strip().split("\n"))

    def generate_client_certificate(self, cn: str = "x509-client") -> Optional[str]:
        """Generate a client certificate for X509 authentication.

        Uses the shared X509CertificateManager to generate client certificates.

        Args:
            cn: Common Name for the client certificate

        Returns:
            The certificate subject DN in RFC2253 format (e.g., CN=x509-client,OU=clients,O=MongoDB),
            or None if generation failed.
        """
        x509_manager = X509CertificateManager(
            ca_cert_path=SCRIPT_DIR / "certs/ca.crt",
            ca_key_path=SCRIPT_DIR / "certs/ca.key",
            output_dir=SCRIPT_DIR / "certs/mongodb"
        )
        return x509_manager.generate_client_certificate(cn=cn)

    def create_x509_user(self, x509_subject_dn: str) -> bool:
        """Create MongoDB X509 user via the operator.

        Args:
            x509_subject_dn: The certificate subject DN in RFC2253 format

        Returns:
            True if user creation succeeded, False otherwise
        """
        rs_namespace = self.cluster_config.rs_namespace
        rs_name = self.rs_config.name

        logger.info(f"Creating X509 user '{x509_subject_dn}' in {rs_namespace}...")

        # Create MongoDBUser resource for X509 authentication
        yaml_path = self.yaml_manager.render_x509_user(rs_namespace, x509_subject_dn, rs_name)
        yaml_content = yaml_path.read_text(encoding='utf-8')
        if not self.k8s.apply_yaml(yaml_content):
            logger.error("Failed to create X509 MongoDBUser resource")
            return False
        logger.info(f"Created X509 MongoDBUser '{x509_subject_dn}'")

        return True

    def create_mongodb_user(self) -> bool:
        """Create MongoDB SCRAM user and password secret."""
        rs_namespace = self.cluster_config.rs_namespace
        rs_name = self.rs_config.name
        username = self.rs_config.mongodb_username
        password = self.rs_config.mongodb_password

        if not password:
            password = generate_secure_password()
            self.rs_config.mongodb_password = password
            logger.info(f"Generated MongoDB password for user '{username}'")

        logger.info(f"Creating MongoDB user '{username}' in {rs_namespace}...")

        # Create password secret
        yaml_path = self.yaml_manager.render_user_secret(rs_namespace, password)
        yaml_content = yaml_path.read_text(encoding='utf-8')
        if not self.k8s.apply_yaml(yaml_content):
            logger.error("Failed to create user password secret")
            return False
        logger.info("Created mongodb-admin-password Secret")

        # Create MongoDBUser resource
        yaml_path = self.yaml_manager.render_user(rs_namespace)
        yaml_content = yaml_path.read_text(encoding='utf-8')
        if not self.k8s.apply_yaml(yaml_content):
            logger.error("Failed to create MongoDBUser resource")
            return False
        logger.info(f"Created MongoDBUser '{username}'")

        return True

    def deploy_replica_set(self) -> bool:
        """Deploy the MongoDB replica set.

        Uses YAML template from k8s/mongodb-replicaset.yaml
        Deploys to rs_namespace (mongodb-rs by default).

        Security features (TLS, SCRAM+X509 auth, external access via NodePort) are always enabled.
        """
        rs_namespace = self.cluster_config.rs_namespace
        logger.info(f"Deploying MongoDB replica set: {self.rs_config.name} in namespace {rs_namespace}")

        # Create TLS secrets (always enabled)
        if not self.create_mongodb_tls_secrets():
            logger.error("Failed to create TLS secrets")
            return False

        # Generate YAML from template - use rs_namespace for MongoDB pods
        yaml_path = self.yaml_manager.render_replicaset(
            namespace=rs_namespace
        )

        logger.info(f"Generated replica set YAML: {yaml_path}")
        yaml_content = yaml_path.read_text(encoding='utf-8')

        if not self.k8s.apply_yaml(yaml_content):
            return False

        logger.info("Replica set CR created, waiting for pods...")

        # Create SCRAM and X509 users (always enabled)
        # Brief delay allows operator to initialize the CR before we create dependent MongoDBUser resources
        time.sleep(5)
        if not self.create_mongodb_user():
            logger.warning("Failed to create SCRAM MongoDB user, but continuing...")

        # Create X509 user
        x509_subject_dn = self.generate_client_certificate()
        if x509_subject_dn:
            self.rs_config.x509_subject_dn = x509_subject_dn
            if not self.create_x509_user(x509_subject_dn):
                logger.warning("Failed to create X509 MongoDB user, but continuing...")
        else:
            logger.warning("Failed to generate client certificate for X509 auth")

        return True

    def precreate_nodeport_services(self) -> bool:
        """Pre-create NodePort services with fixed ports before MongoDB deployment.

        The MongoDB operator reuses existing services if they match the expected naming
        convention. By pre-creating services with fixed NodePorts, the operator preserves
        the port assignments instead of assigning random ports.

        Port numbers are extracted from the replicaset template's connectivity.replicaSetHorizons.
        """
        rs_namespace = self.cluster_config.rs_namespace
        rs_name = self.rs_config.name

        # Extract ports from template
        external_ports = self.yaml_manager.get_external_ports()
        logger.info(f"Pre-creating NodePort services with ports {external_ports}...")

        for i, target_port in enumerate(external_ports):
            svc_name = f"{rs_name}-{i}-svc-external"
            pod_name = f"{rs_name}-{i}"

            # Generate service YAML
            # Use port 10901 for MongoDB (custom port configured in additionalMongodConfig)
            svc_yaml = f"""apiVersion: v1
kind: Service
metadata:
  name: {svc_name}
  namespace: {rs_namespace}
  labels:
    controller: mongodb-enterprise-operator
    statefulset.kubernetes.io/pod-name: {pod_name}
spec:
  type: NodePort
  ports:
  - name: mongodb
    port: 10901
    targetPort: 10901
    nodePort: {target_port}
  selector:
    controller: mongodb-enterprise-operator
    statefulset.kubernetes.io/pod-name: {pod_name}
"""

            # Apply the service
            apply_cmd = [
                "kubectl", "--kubeconfig", str(self.k8s.kubeconfig_file),
                "apply", "-f", "-"
            ]

            try:
                result = run_command(apply_cmd, input_data=svc_yaml, check=False, timeout=30)
                if result.returncode != 0:
                    logger.warning(f"Failed to create service {svc_name}: {result.stderr}")
                    continue
                logger.info(f"Created {svc_name} with nodePort {target_port}")
            except Exception as e:
                logger.warning(f"Error creating service {svc_name}: {e}")
                continue

        return True


def print_kubectl_instructions(cluster_config: ClusterConfig):
    """Print instructions for using kubectl."""
    kubeconfig_path = Path(cluster_config.kubeconfig_dir).resolve() / "config"
    docker_kubeconfig = convert_path_for_docker(Path(cluster_config.kubeconfig_dir).resolve())
    op_ns = cluster_config.operator_namespace
    rs_ns = cluster_config.rs_namespace

    print(f"""
{'='*70}
KUBECTL INSTRUCTIONS
{'='*70}

Kubeconfig file: {kubeconfig_path}

Namespaces:
  - Operator namespace: {op_ns}
  - ReplicaSet namespace: {rs_ns}

1. EXEC INTO KIND CONTROL PLANE (RECOMMENDED - kubectl pre-installed):

   # Get a shell in the kind control plane container
   docker exec -it {cluster_config.name}-control-plane bash

   # Inside the container, kubectl is pre-installed:
   kubectl get pods -n {rs_ns}
   kubectl get mongodb -n {rs_ns}
   kubectl describe mongodb mongodb-rs -n {rs_ns}
   kubectl logs -n {op_ns} -l app.kubernetes.io/name=mongodb-enterprise-operator

2. USING KUBECTL VIA DOCKER (no installation required):

   # On Windows (PowerShell):
   docker run --rm -it `
     -v {docker_kubeconfig}:/root/.kube:ro `
     --network host `
     {KUBECTL_IMAGE} `
     get pods -n {rs_ns}

   # On Linux/macOS:
   docker run --rm -it \\
     -v {docker_kubeconfig}:/root/.kube:ro \\
     --network host \\
     {KUBECTL_IMAGE} \\
     get pods -n {rs_ns}

{'='*70}
USEFUL COMMANDS (run inside kind control plane)
{'='*70}

# Check cluster status
kubectl cluster-info

# View operator (in {op_ns} namespace)
kubectl get all -n {op_ns}
kubectl logs -n {op_ns} -l app.kubernetes.io/name=mongodb-enterprise-operator

# View MongoDB resources (in {rs_ns} namespace)
kubectl get all -n {rs_ns}
kubectl get mongodb -n {rs_ns}
kubectl describe mongodb -n {rs_ns}

# View MongoDB pod logs
kubectl logs -n {rs_ns} -l app=mongodb-rs-svc -c mongodb-enterprise-database

# Port-forward to MongoDB (for local connection)
kubectl port-forward -n {rs_ns} svc/mongodb-rs-svc 27017:27017

{'='*70}
""")


# check_docker imported from shared.utils


def main():
    parser = argparse.ArgumentParser(
        description="Deploy MongoDB Enterprise Kubernetes Operator with replica set",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Full deployment
  python deploy_mongodb_k8s.py

  # Custom cluster name
  python deploy_mongodb_k8s.py --cluster-name my-cluster

  # Cleanup
  python deploy_mongodb_k8s.py --cleanup

  # Show kubectl instructions only
  python deploy_mongodb_k8s.py --instructions-only

All tools (kind, kubectl) run via Docker - no local installation required!
"""
    )

    # Cluster options
    # Note: operator-namespace and rs-namespace are hardcoded in templates
    # (mongodb and mongodb-rs respectively) for consistency
    cluster_group = parser.add_argument_group("Cluster Options")
    cluster_group.add_argument("--cluster-name", default="mongodb-k8s", help="Kind cluster name")
    cluster_group.add_argument("--worker-nodes", type=non_negative_int, default=1,
                               help="Number of worker nodes (default: 1)")
    cluster_group.add_argument("--kubeconfig-dir", default="./.kube", help="Directory for kubeconfig")

    # Ops Manager options
    om_group = parser.add_argument_group("Ops Manager Options")
    om_group.add_argument("--api-key-file", default="./ops-manager-api-key.json",
                          help="Path to Ops Manager API key file")
    om_group.add_argument("--ops-manager-url", default="https://host.docker.internal:8443",
                          help="Ops Manager URL (from inside kind cluster)")
    om_group.add_argument("--ssl-skip-verify", action="store_true",
                          help="Skip TLS certificate validation for Ops Manager (INSECURE - testing only)")

    # Authentication options
    # Note: replica-set-name and mongodb-username are now hardcoded in templates
    # (mongodb-rs and admin respectively) for consistency
    auth_group = parser.add_argument_group("Authentication Options")
    auth_group.add_argument("--mongodb-password", default=None,
                            help="MongoDB admin password (auto-generated if not provided)")

    # Operation modes
    mode_group = parser.add_argument_group("Operation Modes")
    mode_group.add_argument("--cleanup", action="store_true", help="Delete the kind cluster")
    mode_group.add_argument("--skip-operator", action="store_true", help="Skip operator deployment")
    mode_group.add_argument("--skip-replica-set", action="store_true", help="Skip replica set deployment")
    mode_group.add_argument("--instructions-only", action="store_true", help="Only print kubectl instructions")
    mode_group.add_argument("--cluster-only", action="store_true", help="Only create the kind cluster")
    mode_group.add_argument("--skip-preflight", action="store_true", help="Skip pre-flight validation checks")
    mode_group.add_argument("--no-wait", action="store_true",
                            help="Don't wait for MongoDB to reach Running state (default: wait)")
    mode_group.add_argument("--wait-timeout", type=valid_timeout(60, 1800), default=600,
                            help="Timeout for waiting in seconds (default: 600, min: 60, max: 1800)")
    mode_group.add_argument("--skip-health-check", action="store_true",
                            help="Skip MongoDB connectivity health check after deployment")
    mode_group.add_argument("--dry-run", action="store_true",
                            help="Show what would be done without making changes")
    mode_group.add_argument("--show-password", action="store_true",
                            help="Show passwords in plain text (default: masked)")

    # Logging
    parser.add_argument("-v", "--verbose", action="store_true", help="Verbose output")

    args = parser.parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    # Build configuration
    # Note: operator_namespace and rs_namespace use hardcoded defaults
    # matching the YAML templates (mongodb and mongodb-rs)
    cluster_config = ClusterConfig(
        name=args.cluster_name,
        worker_nodes=args.worker_nodes,
        ops_manager_url=args.ops_manager_url,
        kubeconfig_dir=args.kubeconfig_dir,
        ssl_skip_verify=args.ssl_skip_verify
    )

    # Warn about SSL verification bypass
    if cluster_config.ssl_skip_verify:
        logger.warning("=" * 60)
        logger.warning("SSL CERTIFICATE VERIFICATION DISABLED")
        logger.warning("This configuration is INSECURE and NOT suitable for production!")
        logger.warning("MITM attacks are possible. Use only for testing/development.")
        logger.warning("=" * 60)

    # Security features (TLS, SCRAM+X509 auth, external access via NodePort) are always enabled
    # Static values (name, username, members, version, ports, resources) are defined in YAML templates
    rs_config = ReplicaSetConfig(
        mongodb_password=args.mongodb_password
    )

    # Instructions only mode
    if args.instructions_only:
        print_kubectl_instructions(cluster_config)
        return

    # Dry-run mode
    if args.dry_run:
        logger.info("=== DRY-RUN MODE ===")
        logger.info("The following actions would be performed:")
        logger.info(f"  1. Create kind cluster: {cluster_config.name}")
        logger.info(f"  2. Create operator namespace: {cluster_config.operator_namespace}")
        logger.info(f"  3. Create replica set namespace: {cluster_config.rs_namespace}")
        if not args.skip_operator:
            logger.info(f"  4. Deploy MongoDB Operator v{OPERATOR_VERSION}")
            logger.info(f"     - Deploy CRDs from: {OPERATOR_CRDS_URL}")
            logger.info(f"     - Deploy operator from: {OPERATOR_INSTALL_URL}")
            logger.info(f"     - Create database roles in {cluster_config.rs_namespace}")
            logger.info(f"     - Create Ops Manager secret and ConfigMaps in {cluster_config.rs_namespace}")
        if not args.skip_replica_set:
            logger.info(f"  5. Deploy MongoDB ReplicaSet: {rs_config.name} in {cluster_config.rs_namespace}")
        if args.wait:
            logger.info(f"  6. Wait for MongoDB to reach Running state (timeout: {args.wait_timeout}s)")
        logger.info("=== END DRY-RUN ===")
        return

    # Run pre-flight checks (unless skipped)
    if not args.skip_preflight and not args.cleanup and not args.cluster_only:
        preflight = PreFlightChecker(
            ops_manager_url=cluster_config.ops_manager_url,
            credentials_file=args.api_key_file,
            ca_cert_path="./certs/ca.crt"
        )
        if not preflight.check_all():
            logger.error("Pre-flight checks failed. Use --skip-preflight to bypass.")
            sys.exit(1)
        logger.info("All pre-flight checks passed!")

    # Check Docker is running (basic check even if preflight is skipped)
    if not check_docker():
        sys.exit(1)

    # Initialize managers
    kind_manager = KindManager(cluster_config)
    k8s_manager = KubernetesManager(cluster_config)

    # Handle cleanup - correct order is:
    # 1. Delete MongoDB CRD resource (MongoDB)
    # 2. Wait for operator to deregister from Ops Manager
    # 3. Delete Ops Manager project
    # 4. Delete Kubernetes cluster
    if args.cleanup:
        from shared.cleanup import delete_mongodb_crd

        # Step 1: Delete MongoDB CRD resource BEFORE deleting the cluster
        # This allows the operator to properly deregister from Ops Manager
        kubeconfig_path = Path(cluster_config.kubeconfig_dir) / "config"
        if kubeconfig_path.exists():
            logger.info("Step 1/4: Deleting MongoDB CRD resource...")
            delete_mongodb_crd(
                kubeconfig=kubeconfig_path,
                crd_name=rs_config.name,
                namespace=cluster_config.rs_namespace,
                crd_type="mongodb",
                timeout=180  # 3 minutes to wait for operator cleanup
            )
        else:
            logger.info("Kubeconfig not found, skipping MongoDB CRD deletion")

        # Step 2: Wait additional time for Ops Manager to recognize deregistration
        # The operator needs time to report back to Ops Manager
        import time
        from shared.constants import AGENT_DEREGISTRATION_WAIT
        logger.info(f"Step 2/4: Waiting for Ops Manager to register agent removal ({AGENT_DEREGISTRATION_WAIT}s)...")
        time.sleep(AGENT_DEREGISTRATION_WAIT)

        # Step 3: Clean up Ops Manager project (reset automation config and delete project)
        logger.info("Step 3/4: Cleaning up Ops Manager project...")
        ca_cert_path = str(SCRIPT_DIR / "certs/ca.crt") if not cluster_config.ssl_skip_verify else None
        if not cleanup_ops_manager_project(
            api_key_file=args.api_key_file,
            project_type="singleCluster",
            verify_ssl=not cluster_config.ssl_skip_verify,
            delete_project=True,
            ca_cert_path=ca_cert_path
        ):
            logger.warning("Ops Manager project cleanup failed. "
                          "You may need to delete the project manually from Ops Manager UI "
                          "before redeploying to avoid stale certificate errors.")

        # Step 4: Delete kind cluster (now safe because operator has cleaned up)
        logger.info("Step 4/4: Deleting Kubernetes cluster...")
        kind_manager.delete_cluster()

        # Clean up generated files (YAML files, MongoDB TLS certs, kubeconfig files)
        cleanup_generated_files(kubeconfig_dir=cluster_config.kubeconfig_dir)

        return

    # Create cluster
    logger.info("Creating Kubernetes cluster via kind...")
    if not kind_manager.create_cluster():
        logger.error("Failed to create kind cluster")
        sys.exit(1)

    if args.cluster_only:
        print_kubectl_instructions(cluster_config)
        return

    # Load credentials
    try:
        credentials = OpsManagerCredentials.from_file(args.api_key_file, project_type="singleCluster")
        logger.info(f"Loaded Ops Manager credentials from: {args.api_key_file}")
    except FileNotFoundError:
        print(format_error_with_suggestion(
            f"API key file not found: {args.api_key_file}",
            "Run 'python deploy_ops_manager.py' first to set up Ops Manager and generate API credentials",
            "The deployment scripts require Ops Manager to be running and configured"
        ))
        sys.exit(1)
    except json.JSONDecodeError as e:
        print(format_error_with_suggestion(
            f"Invalid JSON in credentials file: {args.api_key_file}",
            "Check that the file contains valid JSON. You may need to regenerate it.",
            str(e)
        ))
        sys.exit(1)
    except KeyError as e:
        print(format_error_with_suggestion(
            f"Missing required field in credentials file: {e}",
            "Ensure the credentials file has all required fields (publicKey, privateKey, baseUrl, orgId)",
            "File may be corrupted or from an older version"
        ))
        sys.exit(1)

    # Verify project exists in Ops Manager, create if needed
    ssl_verify = not cluster_config.ssl_skip_verify
    ca_cert_path = str(SCRIPT_DIR / "certs/ca.crt") if ssl_verify else None
    project_id = credentials.create_project_if_not_exists(
        ssl_verify=ssl_verify,
        credentials_file=args.api_key_file,
        project_type="singleCluster",
        ca_cert_path=ca_cert_path
    )
    if not project_id:
        print(format_error_with_suggestion(
            "Failed to verify or create Ops Manager project",
            "Check that Ops Manager is running and accessible at: " + credentials.base_url,
            "Verify API credentials are correct and have project creation permissions"
        ))
        sys.exit(1)
    logger.info(f"Using Ops Manager project: {credentials.project_name} (ID: {project_id})")

    # Initialize YAML template manager
    yaml_manager = YAMLTemplateManager()
    logger.info(f"YAML templates directory: {yaml_manager.template_dir.resolve()}")
    logger.info(f"Generated YAML directory: {yaml_manager.generated_dir.resolve()}")

    # Deploy operator
    if not args.skip_operator:
        operator_deployer = MongoDBOperatorDeployer(k8s_manager, credentials, cluster_config, yaml_manager)
        if not operator_deployer.deploy_all():
            logger.error("Failed to deploy MongoDB operator")
            sys.exit(1)
        logger.info("MongoDB Enterprise Kubernetes Operator deployed successfully")

    # Deploy replica set
    if not args.skip_replica_set:
        rs_deployer = MongoDBReplicaSetDeployer(k8s_manager, credentials, cluster_config, rs_config, yaml_manager)

        # Pre-create NodePort services with fixed ports BEFORE MongoDB deployment
        # The operator will reuse these services instead of creating new ones with random ports
        logger.info("Pre-creating NodePort services with fixed ports...")
        rs_deployer.precreate_nodeport_services()

        if not rs_deployer.deploy_replica_set():
            logger.error("Failed to deploy replica set")
            sys.exit(1)

        logger.info("Replica set deployment initiated")

        # Wait for MongoDB to be Running (default behavior, unless --no-wait)
        if not args.no_wait:
            monitor = MongoDBStatusMonitor(k8s_manager, cluster_config.rs_namespace, rs_config.name)
            if not monitor.wait_for_running(timeout=args.wait_timeout):
                logger.error("MongoDB did not reach Running state within timeout")
                sys.exit(1)

            # Run health check to verify MongoDB is accessible
            if not args.skip_health_check:
                external_hosts = yaml_manager.get_external_hosts()
                hosts = [h.strip() for h in external_hosts.split(',')]
                ca_cert = SCRIPT_DIR / "certs/ca.crt"

                logger.info("Running MongoDB connectivity health check...")
                health_checker = MongoDBHealthChecker(
                    hosts=hosts,
                    tls_enabled=True,
                    ca_cert_path=ca_cert
                )
                result = health_checker.wait_for_healthy(timeout=60)
                print(format_health_check_result(result))

                if not result.success:
                    logger.warning("MongoDB connectivity check failed - deployment may not be fully accessible")
        else:
            logger.info("Note: It may take several minutes for all pods to be ready")
            logger.info("Use default behavior (no --no-wait) to wait for MongoDB to reach Running state")

    # Print summary and instructions (security features are always enabled)
    # Mask password unless --show-password is specified
    display_password = rs_config.mongodb_password if args.show_password else mask_password(rs_config.mongodb_password)
    auth_info = f"""
Authentication: ENABLED (SCRAM + X509)
SCRAM User: {rs_config.mongodb_username}
SCRAM Password: {display_password}
"""
    if not args.show_password:
        auth_info += "(use --show-password to reveal)\n"
    if rs_config.x509_subject_dn:
        auth_info += f"X509 User DN: {rs_config.x509_subject_dn}\n"
        auth_info += f"X509 Client Cert: ./certs/mongodb/client.pem\n"

    tls_info = "TLS: ENABLED\n"

    print(f"""
{'='*70}
DEPLOYMENT COMPLETE
{'='*70}

Cluster Name: {cluster_config.name}
Operator Namespace: {cluster_config.operator_namespace}
ReplicaSet Namespace: {cluster_config.rs_namespace}
Replica Set: {rs_config.name}
{auth_info}{tls_info}
Ops Manager URL: {credentials.base_url}
Project ID: {credentials.project_id}
Organization ID: {credentials.org_id}

Kubeconfig: {Path(cluster_config.kubeconfig_dir).resolve() / 'config'}

YAML Templates: {yaml_manager.template_dir.resolve()}
Generated YAML: {yaml_manager.generated_dir.resolve()}

{'='*70}
""")

    # Print connection instructions (security features are always enabled)
    certs_path = (SCRIPT_DIR / "certs").resolve()
    # Extract external hosts from template
    external_hosts = yaml_manager.get_external_hosts()
    print(f"""
{'='*70}
CONNECTION INSTRUCTIONS (External Access via NodePort)
{'='*70}

Connect directly to MongoDB via NodePort:

1. SCRAM Authentication (username/password):
   mongosh "mongodb://{external_hosts}/?replicaSet={rs_config.name}&tls=true&tlsCAFile={certs_path}/ca.crt" \\
     --username {rs_config.mongodb_username} \\
     --authenticationDatabase admin

2. X509 Authentication (client certificate):
   mongosh "mongodb://{external_hosts}/?replicaSet={rs_config.name}&tls=true&tlsCAFile={certs_path}/ca.crt&authMechanism=MONGODB-X509&authSource=\\$external" \\
     --tlsCertificateKeyFile {certs_path}/mongodb/client.pem

Note: For development, you may need to use --tlsAllowInvalidHostnames if
the certificate doesn't include the external domain as a SAN.

Check external services:
  kubectl --kubeconfig {Path(cluster_config.kubeconfig_dir).resolve() / 'config'} \\
    get svc -n {cluster_config.rs_namespace} | grep external

{'='*70}
""")

    print_kubectl_instructions(cluster_config)


if __name__ == "__main__":
    main()
