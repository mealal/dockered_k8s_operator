"""
Unified user management for MongoDB Kubernetes deployments.

Consolidates SCRAM and X509 user creation functionality into a single
manager class, reducing code duplication between single-cluster and
multi-cluster deployment scripts.

Usage:
    from shared.user_manager import MongoDBUserManager

    manager = MongoDBUserManager(
        yaml_manager=yaml_manager,
        k8s_manager=k8s_manager,
        kubeconfig=kubeconfig_path,
        rs_namespace="mongodb-rs",
        rs_name="mongodb-rs",
        certs_dir=certs_dir,
    )

    # Create SCRAM user
    manager.create_scram_user(username="admin", password="secret123")

    # Create X509 user
    subject_dn = manager.create_x509_user(common_name="x509-client")
"""

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Protocol, runtime_checkable

from shared.exceptions import (
    ConfigurationError,
    CertificateError,
)
from shared.x509_manager import X509CertificateManager

logger = logging.getLogger(__name__)


@runtime_checkable
class YAMLManagerProtocol(Protocol):
    """Protocol for YAML template managers."""

    def render_user_secret(self, namespace: str, password: str) -> Path:
        """Render user password secret template."""
        ...

    def render_user(self, namespace: str, username: str, rs_name: str) -> Path:
        """Render SCRAM user template."""
        ...

    def render_x509_user(self, namespace: str, x509_username: str, rs_name: str) -> Path:
        """Render X509 user template."""
        ...


@runtime_checkable
class K8sManagerProtocol(Protocol):
    """Protocol for Kubernetes managers."""

    def apply_yaml_file(self, yaml_path: Path, kubeconfig: Path) -> bool:
        """Apply a YAML file to the cluster."""
        ...


@dataclass
class UserCreationResult:
    """Result of a user creation operation.

    Attributes:
        success: Whether the user was created successfully
        username: The username/DN of the created user
        message: Human-readable result message
        secret_created: Whether the password secret was created (SCRAM only)
        cert_path: Path to client certificate (X509 only)
    """
    success: bool
    username: str
    message: str
    secret_created: bool = False
    cert_path: Optional[Path] = None


class MongoDBUserManager:
    """Manages MongoDB user creation for Kubernetes deployments.

    Handles both SCRAM (username/password) and X509 (certificate-based)
    user creation, generating appropriate Kubernetes resources.

    Attributes:
        yaml_manager: YAML template manager instance
        k8s_manager: Kubernetes manager instance
        kubeconfig: Path to kubeconfig file
        rs_namespace: Namespace for replica set
        rs_name: Name of the replica set
        certs_dir: Directory containing certificates
    """

    def __init__(
        self,
        yaml_manager: YAMLManagerProtocol,
        k8s_manager: K8sManagerProtocol,
        kubeconfig: Path,
        rs_namespace: str,
        rs_name: str,
        certs_dir: Path,
    ):
        """Initialize the user manager.

        Args:
            yaml_manager: YAML template manager for rendering user resources
            k8s_manager: Kubernetes manager for applying resources
            kubeconfig: Path to kubeconfig file for kubectl commands
            rs_namespace: Kubernetes namespace for the replica set
            rs_name: Name of the MongoDB replica set
            certs_dir: Directory containing CA and MongoDB certificates
        """
        self.yaml_manager = yaml_manager
        self.k8s_manager = k8s_manager
        self.kubeconfig = Path(kubeconfig)
        self.rs_namespace = rs_namespace
        self.rs_name = rs_name
        self.certs_dir = Path(certs_dir)

    def _get_ca_paths(self) -> tuple[Path, Path]:
        """Get paths to CA certificate and key.

        Returns:
            Tuple of (ca_cert_path, ca_key_path)

        Raises:
            CertificateError: If CA files don't exist
        """
        # CA is in the parent certs directory
        ca_base = self.certs_dir.parent if self.certs_dir.name in ("mongodb", "mongodb-multi") else self.certs_dir
        ca_cert = ca_base / "ca.crt"
        ca_key = ca_base / "ca.key"

        if not ca_cert.exists():
            raise CertificateError(
                "CA certificate not found",
                cert_type="CA",
                cert_path=str(ca_cert),
                suggestion="Run deploy_ops_manager.py first to generate CA certificates"
            )
        if not ca_key.exists():
            raise CertificateError(
                "CA private key not found",
                cert_type="CA",
                cert_path=str(ca_key),
                suggestion="Run deploy_ops_manager.py first to generate CA certificates"
            )

        return ca_cert, ca_key

    def create_scram_user(
        self,
        username: str,
        password: str,
    ) -> UserCreationResult:
        """Create a SCRAM (username/password) MongoDB user.

        Creates both the password secret and the MongoDBUser resource.

        Args:
            username: MongoDB username
            password: User password

        Returns:
            UserCreationResult with operation status
        """
        logger.info(f"Creating SCRAM user '{username}' in {self.rs_namespace}...")

        # Create password secret
        try:
            secret_path = self.yaml_manager.render_user_secret(
                namespace=self.rs_namespace,
                password=password
            )
            if not self.k8s_manager.apply_yaml_file(secret_path, self.kubeconfig):
                return UserCreationResult(
                    success=False,
                    username=username,
                    message="Failed to create user password secret"
                )
            logger.debug(f"Created password secret for user '{username}'")
        except Exception as e:
            logger.error(f"Error creating password secret: {e}")
            return UserCreationResult(
                success=False,
                username=username,
                message=f"Failed to create password secret: {e}"
            )

        # Create MongoDBUser resource
        try:
            user_path = self.yaml_manager.render_user(
                namespace=self.rs_namespace,
                username=username,
                rs_name=self.rs_name
            )
            if not self.k8s_manager.apply_yaml_file(user_path, self.kubeconfig):
                return UserCreationResult(
                    success=False,
                    username=username,
                    message="Failed to create MongoDBUser resource",
                    secret_created=True  # Secret was created
                )
        except Exception as e:
            logger.error(f"Error creating MongoDBUser resource: {e}")
            return UserCreationResult(
                success=False,
                username=username,
                message=f"Failed to create MongoDBUser: {e}",
                secret_created=True
            )

        logger.info(f"Successfully created SCRAM user '{username}'")
        return UserCreationResult(
            success=True,
            username=username,
            message=f"Created SCRAM user '{username}'",
            secret_created=True
        )

    def create_x509_user(
        self,
        common_name: str = "x509-client",
        org: str = "MongoDB",
        ou: str = "clients",
        validity_days: int = 365,
    ) -> UserCreationResult:
        """Create an X509 (certificate-based) MongoDB user.

        Generates a client certificate and creates the MongoDBUser resource.

        Args:
            common_name: Common Name for the client certificate
            org: Organization name for the certificate
            ou: Organizational Unit for the certificate
            validity_days: Certificate validity period in days

        Returns:
            UserCreationResult with operation status and certificate path
        """
        logger.info(f"Creating X509 user with CN='{common_name}' in {self.rs_namespace}...")

        # Get CA paths
        try:
            ca_cert, ca_key = self._get_ca_paths()
        except CertificateError as e:
            return UserCreationResult(
                success=False,
                username=f"CN={common_name},OU={ou},O={org}",
                message=str(e)
            )

        # Generate client certificate
        x509_manager = X509CertificateManager(
            ca_cert_path=ca_cert,
            ca_key_path=ca_key,
            output_dir=self.certs_dir
        )

        subject_dn = x509_manager.generate_client_certificate(
            cn=common_name,
            org=org,
            ou=ou,
            validity_days=validity_days
        )

        if not subject_dn:
            return UserCreationResult(
                success=False,
                username=f"CN={common_name},OU={ou},O={org}",
                message="Failed to generate client certificate"
            )

        # Create MongoDBUser resource for X509
        try:
            user_path = self.yaml_manager.render_x509_user(
                namespace=self.rs_namespace,
                x509_username=subject_dn,
                rs_name=self.rs_name
            )
            if not self.k8s_manager.apply_yaml_file(user_path, self.kubeconfig):
                return UserCreationResult(
                    success=False,
                    username=subject_dn,
                    message="Failed to create X509 MongoDBUser resource",
                    cert_path=x509_manager.client_pem_path
                )
        except Exception as e:
            logger.error(f"Error creating X509 MongoDBUser resource: {e}")
            return UserCreationResult(
                success=False,
                username=subject_dn,
                message=f"Failed to create X509 MongoDBUser: {e}",
                cert_path=x509_manager.client_pem_path
            )

        logger.info(f"Successfully created X509 user '{subject_dn}'")
        return UserCreationResult(
            success=True,
            username=subject_dn,
            message=f"Created X509 user '{subject_dn}'",
            cert_path=x509_manager.client_pem_path
        )

    def create_all_users(
        self,
        scram_username: str,
        scram_password: str,
        x509_common_name: str = "x509-client",
    ) -> tuple[UserCreationResult, UserCreationResult]:
        """Create both SCRAM and X509 users.

        Convenience method to create both authentication types at once.

        Args:
            scram_username: Username for SCRAM authentication
            scram_password: Password for SCRAM authentication
            x509_common_name: Common Name for X509 certificate

        Returns:
            Tuple of (scram_result, x509_result)
        """
        scram_result = self.create_scram_user(scram_username, scram_password)
        x509_result = self.create_x509_user(common_name=x509_common_name)
        return scram_result, x509_result


def create_mongodb_users(
    yaml_manager: YAMLManagerProtocol,
    k8s_manager: K8sManagerProtocol,
    kubeconfig: Path,
    rs_namespace: str,
    rs_name: str,
    certs_dir: Path,
    scram_username: str,
    scram_password: str,
    x509_common_name: str = "x509-client",
) -> tuple[bool, Optional[str]]:
    """Convenience function to create both SCRAM and X509 users.

    This is a simplified interface for the common use case of creating
    both user types during deployment.

    Args:
        yaml_manager: YAML template manager
        k8s_manager: Kubernetes manager
        kubeconfig: Path to kubeconfig file
        rs_namespace: Replica set namespace
        rs_name: Replica set name
        certs_dir: Directory for certificates
        scram_username: SCRAM username
        scram_password: SCRAM password
        x509_common_name: X509 certificate CN

    Returns:
        Tuple of (overall_success, x509_subject_dn)
        overall_success is True if at least SCRAM user was created
        x509_subject_dn is the DN if X509 user was created, else None
    """
    manager = MongoDBUserManager(
        yaml_manager=yaml_manager,
        k8s_manager=k8s_manager,
        kubeconfig=kubeconfig,
        rs_namespace=rs_namespace,
        rs_name=rs_name,
        certs_dir=certs_dir,
    )

    scram_result, x509_result = manager.create_all_users(
        scram_username=scram_username,
        scram_password=scram_password,
        x509_common_name=x509_common_name,
    )

    if not scram_result.success:
        logger.warning(f"SCRAM user creation failed: {scram_result.message}")

    if not x509_result.success:
        logger.warning(f"X509 user creation failed: {x509_result.message}")

    return (
        scram_result.success,
        x509_result.username if x509_result.success else None
    )
