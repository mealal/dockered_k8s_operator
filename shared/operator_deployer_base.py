"""
Base Operator Deployer for MongoDB Kubernetes deployment scripts.

Contains the common functionality shared between single-cluster and multi-cluster
operator deployers, including CRD deployment, secret creation, and RBAC setup.
"""

import logging
import subprocess
from pathlib import Path
from typing import Optional

from shared.decorators import retry_with_backoff
from shared.models import OpsManagerCredentials
from shared.k8s_manager_base import BaseKubernetesManager
from shared.yaml_manager_base import BaseYAMLTemplateManager

logger = logging.getLogger(__name__)

# Official MongoDB Enterprise Kubernetes Operator URLs
OPERATOR_CRDS_URL = "https://raw.githubusercontent.com/mongodb/mongodb-enterprise-kubernetes/master/crds.yaml"
OPERATOR_INSTALL_URL = "https://raw.githubusercontent.com/mongodb/mongodb-enterprise-kubernetes/master/mongodb-enterprise.yaml"
OPERATOR_MULTI_CLUSTER_URL = "https://raw.githubusercontent.com/mongodb/mongodb-enterprise-kubernetes/master/mongodb-enterprise-multi-cluster.yaml"


class BaseOperatorDeployer:
    """Base class for MongoDB operator deployers.

    Provides common functionality for deploying the MongoDB Enterprise Kubernetes
    Operator including:
    - CRD deployment
    - Ops Manager secret and configmap creation
    - RBAC configuration
    - Database roles deployment

    Subclasses should implement cluster-specific deployment logic.

    Attributes:
        credentials: Ops Manager API credentials
        rs_namespace: Namespace for replica set resources
        operator_namespace: Namespace for operator deployment
        ssl_skip_verify: Whether to skip SSL verification
    """

    def __init__(
        self,
        k8s: BaseKubernetesManager,
        credentials: OpsManagerCredentials,
        yaml_manager: BaseYAMLTemplateManager,
        rs_namespace: str,
        operator_namespace: str,
        ops_manager_url: str,
        ssl_skip_verify: bool = False
    ):
        """Initialize the operator deployer.

        Args:
            k8s: Kubernetes manager for running kubectl commands
            credentials: Ops Manager API credentials
            yaml_manager: YAML template manager
            rs_namespace: Namespace for replica set resources
            operator_namespace: Namespace for operator deployment
            ops_manager_url: URL of Ops Manager instance
            ssl_skip_verify: Whether to skip SSL verification
        """
        self.k8s = k8s
        self.credentials = credentials
        self.yaml_manager = yaml_manager
        self.rs_namespace = rs_namespace
        self.operator_namespace = operator_namespace
        self.ops_manager_url = ops_manager_url
        self.ssl_skip_verify = ssl_skip_verify

    def deploy_crds(self, kubeconfig: Path) -> bool:
        """Deploy MongoDB CRDs from official GitHub repository.

        Args:
            kubeconfig: Path to kubeconfig file

        Returns:
            True if CRDs deployed successfully, False on error
        """
        logger.info("Deploying MongoDB CRDs from official repository...")
        try:
            self.k8s.run_kubectl(["apply", "-f", OPERATOR_CRDS_URL], kubeconfig)
            logger.info("CRDs deployed successfully")
            return True
        except Exception as e:
            logger.error(f"Failed to deploy CRDs: {e}")
            return False

    @retry_with_backoff(max_retries=3, exceptions=(subprocess.CalledProcessError,))
    def create_ops_manager_secret(self, kubeconfig: Path) -> bool:
        """Create or update secret for Ops Manager API credentials.

        This method is idempotent - it will update the secret if it already exists.

        Args:
            kubeconfig: Path to kubeconfig file

        Returns:
            True if secret created successfully, False on error
        """
        logger.info(f"Creating/updating Ops Manager credentials secret in {self.rs_namespace}...")

        # Delete existing secret first to ensure clean state
        self.k8s.delete_resource(
            "secret", "ops-manager-admin-key",
            self.rs_namespace, kubeconfig,
            ignore_not_found=True
        )

        # Generate YAML from template
        yaml_path = self.yaml_manager.render_secret(
            namespace=self.rs_namespace,
            public_key=self.credentials.public_key,
            private_key=self.credentials.private_key
        )

        logger.info(f"Generated secret YAML: {yaml_path}")
        return self.k8s.apply_yaml_file(yaml_path, kubeconfig)

    @retry_with_backoff(max_retries=3, exceptions=(subprocess.CalledProcessError,))
    def create_ops_manager_configmap(self, kubeconfig: Path) -> bool:
        """Create or update ConfigMap for Ops Manager connection.

        This method is idempotent - kubectl apply will update if exists.

        Args:
            kubeconfig: Path to kubeconfig file

        Returns:
            True if ConfigMap created successfully, False on error
        """
        logger.info(f"Creating/updating Ops Manager ConfigMap in {self.rs_namespace}...")

        yaml_path = self.yaml_manager.render_configmap(
            namespace=self.rs_namespace,
            base_url=self.ops_manager_url,
            project_name=self.credentials.project_name,
            org_id=self.credentials.org_id,
            ssl_require_valid_certs=not self.ssl_skip_verify
        )

        logger.info(f"Generated configmap YAML: {yaml_path}")
        return self.k8s.apply_yaml_file(yaml_path, kubeconfig)

    @retry_with_backoff(max_retries=3, exceptions=(subprocess.CalledProcessError,))
    def create_ops_manager_ca_configmap(
        self,
        kubeconfig: Path,
        ca_cert_path: Optional[Path] = None
    ) -> bool:
        """Create or update ConfigMap for Ops Manager CA certificate.

        Args:
            kubeconfig: Path to kubeconfig file
            ca_cert_path: Path to CA certificate (default: ./certs/ca.crt)

        Returns:
            True if ConfigMap created successfully, False on error
        """
        ca_cert = ca_cert_path or Path("./certs/ca.crt")
        logger.info(f"Creating/updating Ops Manager CA ConfigMap in {self.rs_namespace}...")

        if not ca_cert.exists():
            logger.error(f"CA certificate not found at {ca_cert}. Run deploy_ops_manager.py first.")
            return False

        yaml_path = self.yaml_manager.render_ca_configmap(
            namespace=self.rs_namespace,
            ca_cert_path=ca_cert
        )

        logger.info(f"Generated CA configmap YAML: {yaml_path}")
        return self.k8s.apply_yaml_file(yaml_path, kubeconfig)

    def deploy_operator_rbac(self, kubeconfig: Path) -> bool:
        """Create RBAC for operator to access the replica set namespace.

        Args:
            kubeconfig: Path to kubeconfig file

        Returns:
            True if RBAC created successfully, False on error
        """
        logger.info(f"Creating RBAC for operator in {self.rs_namespace} namespace...")

        yaml_path = self.yaml_manager.render_operator_rbac(
            rs_namespace=self.rs_namespace,
            operator_namespace=self.operator_namespace
        )

        logger.info(f"Generated operator RBAC YAML: {yaml_path}")
        return self.k8s.apply_yaml_file(yaml_path, kubeconfig)

    def deploy_database_roles(self, kubeconfig: Path) -> bool:
        """Deploy roles for MongoDB database pods.

        Args:
            kubeconfig: Path to kubeconfig file

        Returns:
            True if roles deployed successfully, False on error
        """
        logger.info(f"Deploying MongoDB database roles in {self.rs_namespace}...")

        yaml_path = self.yaml_manager.render_database_roles(rs_namespace=self.rs_namespace)

        logger.info(f"Generated database roles YAML: {yaml_path}")
        return self.k8s.apply_yaml_file(yaml_path, kubeconfig)

    def deploy_mongodb_ca_configmap(
        self,
        kubeconfig: Path,
        ca_cert_path: Optional[Path] = None
    ) -> bool:
        """Deploy CA certificate ConfigMap for MongoDB TLS.

        Args:
            kubeconfig: Path to kubeconfig file
            ca_cert_path: Path to CA certificate (default: ./certs/ca.crt)

        Returns:
            True if ConfigMap created successfully, False on error
        """
        ca_cert = ca_cert_path or Path("./certs/ca.crt")
        logger.info(f"Creating mongodb-ca ConfigMap in {self.rs_namespace}...")

        if not ca_cert.exists():
            logger.error(f"CA certificate not found at {ca_cert}")
            return False

        yaml_path = self.yaml_manager.render_mongodb_ca_configmap(
            rs_namespace=self.rs_namespace,
            ca_cert_path=ca_cert
        )

        logger.info(f"Generated mongodb-ca ConfigMap YAML: {yaml_path}")
        return self.k8s.apply_yaml_file(yaml_path, kubeconfig)

    def create_mongodb_user_secret(self, kubeconfig: Path, password: str) -> bool:
        """Create secret for MongoDB user password.

        Args:
            kubeconfig: Path to kubeconfig file
            password: User password

        Returns:
            True if secret created successfully, False on error
        """
        logger.info(f"Creating MongoDB user secret in {self.rs_namespace}...")

        yaml_path = self.yaml_manager.render_user_secret(
            namespace=self.rs_namespace,
            password=password
        )

        return self.k8s.apply_yaml_file(yaml_path, kubeconfig)

    def create_mongodb_user(
        self,
        kubeconfig: Path,
        username: str,
        rs_name: str
    ) -> bool:
        """Create MongoDB user resource.

        Args:
            kubeconfig: Path to kubeconfig file
            username: MongoDB username
            rs_name: Replica set name

        Returns:
            True if user created successfully, False on error
        """
        logger.info(f"Creating MongoDB user {username} in {self.rs_namespace}...")

        yaml_path = self.yaml_manager.render_user(
            namespace=self.rs_namespace,
            username=username,
            rs_name=rs_name
        )

        return self.k8s.apply_yaml_file(yaml_path, kubeconfig)
