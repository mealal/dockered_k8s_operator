"""
Base YAML Template Manager for MongoDB Kubernetes deployment scripts.

Contains the common functionality shared between single-cluster and multi-cluster
YAML managers, including template rendering, namespace substitution, and file management.
"""

import logging
import re
import shutil
from pathlib import Path
from typing import Callable, Dict, List, Optional

logger = logging.getLogger(__name__)


class BaseYAMLTemplateManager:
    """Base class for YAML template managers.

    Provides common functionality for template rendering, variable substitution,
    and generated file management. Subclasses should extend this with deployment-
    specific template methods.

    Attributes:
        template_dir: Directory containing YAML templates
        generated_dir: Directory for generated YAML files
    """

    def __init__(self, template_dir: Path):
        """Initialize the YAML template manager.

        Args:
            template_dir: Directory containing YAML templates
        """
        self.template_dir = template_dir
        self.generated_dir = template_dir / "generated"
        self._ensure_dirs()

    def _ensure_dirs(self) -> None:
        """Ensure template and generated directories exist."""
        self.template_dir.mkdir(parents=True, exist_ok=True)
        self.generated_dir.mkdir(parents=True, exist_ok=True)

    def _render_with_namespace(
        self,
        template_name: str,
        variables: Dict[str, str],
        namespace: Optional[str] = None,
        output_name: Optional[str] = None,
        post_process: Optional[Callable[[str], str]] = None
    ) -> Path:
        """Internal method for consistent template rendering with namespace support.

        Args:
            template_name: Name of the template file
            variables: Dictionary of variables to substitute ({{KEY}} -> value)
            namespace: Optional namespace to replace 'mongodb-rs' default
            output_name: Optional output filename (defaults to template_name)
            post_process: Optional function for additional content processing

        Returns:
            Path to the generated YAML file

        Raises:
            FileNotFoundError: If template file doesn't exist
            ValueError: If unsubstituted placeholders remain
        """
        template_path = self.template_dir / template_name
        if not template_path.exists():
            raise FileNotFoundError(f"Template not found: {template_path}")

        content = template_path.read_text(encoding='utf-8')

        # Substitute variables
        for key, value in variables.items():
            content = content.replace(f"{{{{{key}}}}}", str(value))

        # Update namespace if provided
        if namespace:
            content = re.sub(r'namespace: mongodb-rs', f'namespace: {namespace}', content)

        # Apply post-processing if provided
        if post_process:
            content = post_process(content)

        # Check for unsubstituted placeholders - fail if any remain
        remaining = re.findall(r'\{\{[A-Z_]+\}\}', content)
        if remaining:
            raise ValueError(f"Unsubstituted placeholders in {template_name}: {remaining}")

        # Write generated file
        output_path = self.generated_dir / (output_name or template_name)
        output_path.write_text(content, encoding='utf-8')
        logger.debug(f"Generated: {output_path}")

        return output_path

    def render_namespace(
        self,
        namespace: str,
        template_name: str = "namespace.yaml",
        output_name: str = "namespace.yaml"
    ) -> Path:
        """Render namespace.yaml with the specified namespace name.

        Args:
            namespace: The namespace name to use
            template_name: Name of the template file
            output_name: Name for the generated file

        Returns:
            Path to the generated YAML file
        """
        template_path = self.template_dir / template_name
        content = template_path.read_text(encoding='utf-8')

        # Replace namespace name using regex to handle any default name
        content = re.sub(r'(name: )(mongodb(?:-rs)?)\n', f'\\g<1>{namespace}\n', content)

        output_path = self.generated_dir / output_name
        output_path.write_text(content, encoding='utf-8')
        return output_path

    def render_secret(self, namespace: str, public_key: str, private_key: str) -> Path:
        """Render ops-manager-secret.yaml with credentials.

        Args:
            namespace: Kubernetes namespace
            public_key: Ops Manager API public key
            private_key: Ops Manager API private key

        Returns:
            Path to the generated YAML file
        """
        return self._render_with_namespace(
            "ops-manager-secret.yaml",
            {"PUBLIC_KEY": public_key, "PRIVATE_KEY": private_key},
            namespace=namespace
        )

    def render_configmap(
        self,
        namespace: str,
        base_url: str,
        project_name: str,
        org_id: str,
        ssl_require_valid_certs: bool = True
    ) -> Path:
        """Render ops-manager-configmap.yaml with connection details.

        Args:
            namespace: Kubernetes namespace
            base_url: Ops Manager base URL
            project_name: Ops Manager project name
            org_id: Ops Manager organization ID
            ssl_require_valid_certs: Whether to require valid SSL certificates

        Returns:
            Path to the generated YAML file
        """
        variables = {
            "BASE_URL": base_url,
            "PROJECT_NAME": project_name,
            "ORG_ID": org_id,
            "SSL_REQUIRE_VALID_CERTS": "true" if ssl_require_valid_certs else "false",
        }

        # Post-processor to remove CA ConfigMap reference when SSL verification is disabled
        def remove_ca_configmap_if_needed(content: str) -> str:
            if not ssl_require_valid_certs:
                # Remove the sslMMSCAConfigMap line and its comments
                content = re.sub(
                    r'\n\s*#[^\n]*CA certificate[^\n]*\n\s*#[^\n]*\n\s*#[^\n]*\n\s*sslMMSCAConfigMap:[^\n]*',
                    '',
                    content
                )
            return content

        return self._render_with_namespace(
            "ops-manager-configmap.yaml",
            variables,
            namespace=namespace,
            post_process=remove_ca_configmap_if_needed
        )

    def render_ca_configmap(self, namespace: str, ca_cert_path: Path) -> Path:
        """Render ops-manager-ca-configmap.yaml with CA certificate.

        Args:
            namespace: Kubernetes namespace
            ca_cert_path: Path to CA certificate file

        Returns:
            Path to the generated YAML file

        Raises:
            FileNotFoundError: If CA certificate file doesn't exist
        """
        if not ca_cert_path.exists():
            raise FileNotFoundError(f"CA certificate not found: {ca_cert_path}")

        ca_cert_content = ca_cert_path.read_text(encoding='utf-8').strip()
        # Indent the certificate for YAML embedding
        indented_cert = "\n".join("    " + line for line in ca_cert_content.split("\n"))

        # Post-processor to replace the indented placeholder with the certificate
        def embed_certificate(content: str) -> str:
            return content.replace("    {{CA_CERTIFICATE}}", indented_cert)

        return self._render_with_namespace(
            "ops-manager-ca-configmap.yaml",
            {},  # No standard variables, cert handled via post-processor
            namespace=namespace,
            post_process=embed_certificate
        )

    def render_mongodb_ca_configmap(self, rs_namespace: str, ca_cert_path: Path) -> Path:
        """Render mongodb-ca-configmap.yaml with CA certificate.

        Args:
            rs_namespace: Namespace where replica set will be deployed
            ca_cert_path: Path to CA certificate file

        Returns:
            Path to the generated YAML file

        Raises:
            FileNotFoundError: If CA certificate file doesn't exist
        """
        if not ca_cert_path.exists():
            raise FileNotFoundError(f"CA certificate not found: {ca_cert_path}")

        ca_cert_content = ca_cert_path.read_text(encoding='utf-8').strip()
        # Indent the certificate for YAML embedding (4 spaces for ca-pem value)
        indented_cert = "\n".join("    " + line for line in ca_cert_content.split("\n"))

        # Post-processor to embed the certificate with proper indentation
        def embed_certificate(content: str) -> str:
            return content.replace("{{CA_CERTIFICATE}}", indented_cert)

        return self._render_with_namespace(
            "mongodb-ca-configmap.yaml",
            {"RS_NAMESPACE": rs_namespace},
            post_process=embed_certificate
        )

    def render_operator_rbac(self, rs_namespace: str, operator_namespace: str) -> Path:
        """Render operator-rbac.yaml with namespace configuration.

        Args:
            rs_namespace: Namespace for replica sets
            operator_namespace: Namespace for the operator

        Returns:
            Path to the generated YAML file
        """
        return self._render_with_namespace(
            "operator-rbac.yaml",
            {"RS_NAMESPACE": rs_namespace, "OPERATOR_NAMESPACE": operator_namespace}
        )

    def render_database_roles(self, rs_namespace: str) -> Path:
        """Render database-roles.yaml with namespace configuration.

        Args:
            rs_namespace: Namespace for replica sets

        Returns:
            Path to the generated YAML file
        """
        return self._render_with_namespace(
            "database-roles.yaml",
            {"RS_NAMESPACE": rs_namespace}
        )

    def render_user_secret(self, namespace: str, password: str) -> Path:
        """Render mongodb-user-secret.yaml with user password.

        Args:
            namespace: Kubernetes namespace
            password: User password

        Returns:
            Path to the generated YAML file
        """
        return self._render_with_namespace(
            "mongodb-user-secret.yaml",
            {"MONGODB_USER_PASSWORD": password},
            namespace=namespace
        )

    def render_user(self, namespace: str, username: str, rs_name: str) -> Path:
        """Render mongodb-user.yaml with user configuration.

        Args:
            namespace: Kubernetes namespace
            username: MongoDB username
            rs_name: Replica set name

        Returns:
            Path to the generated YAML file
        """
        return self._render_with_namespace(
            "mongodb-user.yaml",
            {"MONGODB_USERNAME": username, "REPLICA_SET_NAME": rs_name},
            namespace=namespace
        )

    def render_kind_cluster_config(self, ports: List[int]) -> str:
        """Render kind cluster configuration YAML from template.

        Args:
            ports: List of ports to map from host to container.

        Returns:
            Rendered YAML content as string.

        Raises:
            FileNotFoundError: If kind cluster config template doesn't exist
        """
        template_path = self.template_dir / "kind-cluster-config.yaml"
        if not template_path.exists():
            raise FileNotFoundError(f"Kind cluster config template not found: {template_path}")

        content = template_path.read_text(encoding='utf-8')

        # Generate port mappings with proper indentation
        port_mappings = ""
        for port in ports:
            port_mappings += f"""      - containerPort: {port}
        hostPort: {port}
        protocol: TCP
"""
        content = content.replace("{{EXTRA_PORT_MAPPINGS}}", port_mappings)
        return content

    def get_generated_files(self) -> List[Path]:
        """Get list of all generated YAML files.

        Returns:
            Sorted list of paths to generated YAML files
        """
        if not self.generated_dir.exists():
            return []
        return sorted(self.generated_dir.glob("*.yaml"))

    def clean_generated(self) -> None:
        """Remove all generated YAML files."""
        if self.generated_dir.exists():
            shutil.rmtree(self.generated_dir)
            self.generated_dir.mkdir(parents=True, exist_ok=True)
            logger.info("Cleaned generated YAML files")
