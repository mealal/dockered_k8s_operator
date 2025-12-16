"""
Base YAML Template Manager for MongoDB Kubernetes deployment scripts.

Contains the common functionality shared between single-cluster and multi-cluster
YAML managers, including template rendering, namespace substitution, and file management.

Features:
- Template caching for improved performance
- Variable substitution with {{PLACEHOLDER}} syntax
- Namespace-aware rendering
- Post-processing hooks for custom transformations
"""

import logging
import re
import shutil
from pathlib import Path
from threading import Lock
from typing import Callable, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


class TemplateCache:
    """Thread-safe cache for template file contents.

    Caches template files to avoid repeated disk reads during deployment.
    Supports automatic invalidation based on file modification time.

    Attributes:
        max_size: Maximum number of templates to cache
    """

    def __init__(self, max_size: int = 50):
        """Initialize the template cache.

        Args:
            max_size: Maximum number of templates to cache (LRU eviction)
        """
        self._cache: Dict[Path, Tuple[str, float]] = {}  # path -> (content, mtime)
        self._lock = Lock()
        self._max_size = max_size
        self._access_order: List[Path] = []  # For LRU tracking

    def get(self, path: Path) -> Optional[str]:
        """Get cached template content if valid.

        Args:
            path: Path to template file

        Returns:
            Cached content if valid, None otherwise
        """
        with self._lock:
            if path not in self._cache:
                return None

            content, cached_mtime = self._cache[path]

            # Check if file has been modified
            try:
                current_mtime = path.stat().st_mtime
                if current_mtime > cached_mtime:
                    # File modified, invalidate cache
                    del self._cache[path]
                    self._access_order.remove(path)
                    return None
            except OSError:
                # File no longer exists, invalidate
                del self._cache[path]
                if path in self._access_order:
                    self._access_order.remove(path)
                return None

            # Update access order for LRU
            self._access_order.remove(path)
            self._access_order.append(path)

            return content

    def put(self, path: Path, content: str) -> None:
        """Cache template content.

        Args:
            path: Path to template file
            content: Template content to cache
        """
        with self._lock:
            # Evict oldest if at capacity
            while len(self._cache) >= self._max_size and self._access_order:
                oldest = self._access_order.pop(0)
                self._cache.pop(oldest, None)

            try:
                mtime = path.stat().st_mtime
                self._cache[path] = (content, mtime)
                self._access_order.append(path)
            except OSError:
                pass  # Don't cache if we can't get mtime

    def invalidate(self, path: Optional[Path] = None) -> None:
        """Invalidate cache entry or entire cache.

        Args:
            path: Specific path to invalidate, or None for all
        """
        with self._lock:
            if path is None:
                self._cache.clear()
                self._access_order.clear()
            elif path in self._cache:
                del self._cache[path]
                self._access_order.remove(path)

    def stats(self) -> Dict[str, int]:
        """Get cache statistics.

        Returns:
            Dict with 'size' and 'max_size' keys
        """
        with self._lock:
            return {
                "size": len(self._cache),
                "max_size": self._max_size,
            }


# Global template cache instance (shared across managers)
_template_cache = TemplateCache()


class BaseYAMLTemplateManager:
    """Base class for YAML template managers.

    Provides common functionality for template rendering, variable substitution,
    and generated file management. Subclasses should extend this with deployment-
    specific template methods.

    Attributes:
        template_dir: Directory containing YAML templates
        generated_dir: Directory for generated YAML files
    """

    def __init__(self, template_dir: Path, use_cache: bool = True):
        """Initialize the YAML template manager.

        Args:
            template_dir: Directory containing YAML templates
            use_cache: Whether to use template caching (default: True)
        """
        self.template_dir = template_dir
        self.generated_dir = template_dir / "generated"
        self.use_cache = use_cache
        self._ensure_dirs()

    def _read_template(self, template_path: Path) -> str:
        """Read template content, using cache if enabled.

        Args:
            template_path: Path to template file

        Returns:
            Template content

        Raises:
            FileNotFoundError: If template doesn't exist
        """
        if not template_path.exists():
            raise FileNotFoundError(f"Template not found: {template_path}")

        # Try cache first
        if self.use_cache:
            cached = _template_cache.get(template_path)
            if cached is not None:
                logger.debug(f"Cache hit for template: {template_path.name}")
                return cached

        # Read from disk
        content = template_path.read_text(encoding='utf-8')

        # Cache for future use
        if self.use_cache:
            _template_cache.put(template_path, content)
            logger.debug(f"Cached template: {template_path.name}")

        return content

    def clear_cache(self) -> None:
        """Clear the template cache."""
        _template_cache.invalidate()
        logger.debug("Template cache cleared")

    def get_cache_stats(self) -> Dict[str, int]:
        """Get template cache statistics.

        Returns:
            Dict with cache size and max size
        """
        return _template_cache.stats()

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
        content = self._read_template(template_path)

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
        # Match both uppercase ({{FOO}}) and mixed-case ({{Foo_Bar}}) placeholders
        remaining = re.findall(r'\{\{[A-Za-z_][A-Za-z0-9_]*\}\}', content)
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
        content = self._read_template(template_path)

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
        project_id: str,
        org_id: str,
        project_name: str,
        ssl_require_valid_certs: bool = True
    ) -> Path:
        """Render ops-manager-configmap.yaml with connection details.

        Args:
            namespace: Kubernetes namespace
            base_url: Ops Manager base URL
            project_id: Ops Manager project ID
            org_id: Ops Manager organization ID
            project_name: Ops Manager project name (used by operator to find project)
            ssl_require_valid_certs: Whether to require valid SSL certificates

        Returns:
            Path to the generated YAML file
        """
        variables = {
            "BASE_URL": base_url,
            "PROJECT_ID": project_id,
            "PROJECT_NAME": project_name,
            "ORG_ID": org_id,
            # SSL_REQUIRE_VALID_CERTS now hardcoded in template as 'false'
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

    def render_mongodb_ca_configmap(self, ca_cert_path: Path) -> Path:
        """Render mongodb-ca-configmap.yaml with CA certificate.

        Namespace (mongodb-rs) is hardcoded in the template for consistency.

        Args:
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
            {},  # RS_NAMESPACE now hardcoded in template
            post_process=embed_certificate
        )

    def render_operator_rbac(self) -> Path:
        """Render operator-rbac.yaml.

        Namespaces (mongodb-rs for replica sets, mongodb for operator) are
        hardcoded in the template for consistency.

        Returns:
            Path to the generated YAML file
        """
        return self._render_with_namespace(
            "operator-rbac.yaml",
            {}  # RS_NAMESPACE, OPERATOR_NAMESPACE hardcoded in template
        )

    def render_database_roles(self) -> Path:
        """Render database-roles.yaml.

        Namespace (mongodb-rs) is hardcoded in the template for consistency.

        Returns:
            Path to the generated YAML file
        """
        return self._render_with_namespace(
            "database-roles.yaml",
            {}  # RS_NAMESPACE hardcoded in template
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

    def render_user(self, namespace: str) -> Path:
        """Render mongodb-user.yaml.

        Username ('admin') and replica set name are hardcoded in the template
        for consistency.

        Args:
            namespace: Kubernetes namespace (used for namespace: field replacement)

        Returns:
            Path to the generated YAML file
        """
        return self._render_with_namespace(
            "mongodb-user.yaml",
            {},  # MONGODB_USERNAME, REPLICA_SET_NAME hardcoded in template
            namespace=namespace
        )

    def render_x509_user(self, namespace: str, x509_username: str, rs_name: str) -> Path:
        """Render mongodb-x509-user.yaml with X509 user configuration.

        Args:
            namespace: Kubernetes namespace
            x509_username: The certificate subject DN in RFC2253 format
            rs_name: MongoDB replica set name

        Returns:
            Path to the generated YAML file
        """
        return self._render_with_namespace(
            "mongodb-x509-user.yaml",
            {"X509_USERNAME": x509_username, "REPLICA_SET_NAME": rs_name},
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
        content = self._read_template(template_path)

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
