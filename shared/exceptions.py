"""
Custom exception hierarchy for MongoDB Kubernetes deployment scripts.

Provides specific exception types for different failure scenarios,
enabling better error handling, logging, and user feedback.

Usage:
    from shared.exceptions import (
        DeploymentError,
        KubernetesError,
        ConfigurationError,
    )

    try:
        deploy_operator()
    except KubernetesError as e:
        logger.error(f"Kubernetes operation failed: {e}")
        if e.kubectl_output:
            logger.debug(f"kubectl output: {e.kubectl_output}")

Exception Hierarchy:
    DeploymentError (base)
    ├── ConfigurationError      - Invalid configuration or missing values
    ├── PreflightError          - Pre-flight check failures
    ├── KubernetesError         - Kubernetes API/kubectl failures
    │   ├── ClusterError        - Cluster creation/deletion issues
    │   ├── NamespaceError      - Namespace operations
    │   ├── ResourceError       - K8s resource CRUD operations
    │   └── ServiceError        - Service-specific issues
    ├── OpsManagerError         - Ops Manager API/connectivity issues
    │   ├── AuthenticationError - Auth/API key issues
    │   └── ProjectError        - Project management issues
    ├── CertificateError        - TLS certificate generation/validation
    ├── NetworkError            - Network connectivity issues
    ├── TimeoutError            - Operation timeout
    └── CleanupError            - Cleanup operation failures
"""

from typing import Optional, Any, Dict


class DeploymentError(Exception):
    """Base exception for all deployment-related errors.

    Attributes:
        message: Human-readable error description
        details: Optional additional context or debug information
        suggestion: Optional suggestion for how to resolve the error
    """

    def __init__(
        self,
        message: str,
        details: Optional[str] = None,
        suggestion: Optional[str] = None
    ):
        """Initialize deployment error.

        Args:
            message: Human-readable error description
            details: Additional context or debug information
            suggestion: How to resolve the error
        """
        self.message = message
        self.details = details
        self.suggestion = suggestion
        super().__init__(self._format_message())

    def _format_message(self) -> str:
        """Format the full error message."""
        parts = [self.message]
        if self.details:
            parts.append(f"Details: {self.details}")
        if self.suggestion:
            parts.append(f"Suggestion: {self.suggestion}")
        return "\n".join(parts)


class ConfigurationError(DeploymentError):
    """Invalid configuration or missing required values.

    Raised when:
    - Required configuration values are missing
    - Configuration values are invalid
    - Template placeholders cannot be resolved
    - YAML parsing fails

    Attributes:
        config_key: The configuration key that caused the error
        config_value: The invalid value (if any)
    """

    def __init__(
        self,
        message: str,
        config_key: Optional[str] = None,
        config_value: Optional[Any] = None,
        **kwargs
    ):
        self.config_key = config_key
        self.config_value = config_value
        super().__init__(message, **kwargs)


class PreflightError(DeploymentError):
    """Pre-flight validation check failed.

    Raised when:
    - Required tools are not installed (docker, kubectl, etc.)
    - Required files are missing
    - Prerequisites are not met
    - Environment validation fails

    Attributes:
        check_name: Name of the failed check
        requirements: What is required to pass the check
    """

    def __init__(
        self,
        message: str,
        check_name: Optional[str] = None,
        requirements: Optional[str] = None,
        **kwargs
    ):
        self.check_name = check_name
        self.requirements = requirements
        super().__init__(message, **kwargs)


class KubernetesError(DeploymentError):
    """Kubernetes API or kubectl operation failed.

    Base class for all Kubernetes-related errors.

    Attributes:
        kubectl_output: Raw output from kubectl command
        resource_kind: Type of K8s resource (e.g., 'Pod', 'Service')
        resource_name: Name of the resource
        namespace: Namespace where the operation was attempted
    """

    def __init__(
        self,
        message: str,
        kubectl_output: Optional[str] = None,
        resource_kind: Optional[str] = None,
        resource_name: Optional[str] = None,
        namespace: Optional[str] = None,
        **kwargs
    ):
        self.kubectl_output = kubectl_output
        self.resource_kind = resource_kind
        self.resource_name = resource_name
        self.namespace = namespace
        super().__init__(message, **kwargs)


class ClusterError(KubernetesError):
    """Kubernetes cluster creation, deletion, or connection error.

    Raised when:
    - Kind cluster creation fails
    - Cluster deletion fails
    - Cannot connect to cluster
    - Kubeconfig issues

    Attributes:
        cluster_name: Name of the affected cluster
    """

    def __init__(
        self,
        message: str,
        cluster_name: Optional[str] = None,
        **kwargs
    ):
        self.cluster_name = cluster_name
        super().__init__(message, **kwargs)


class NamespaceError(KubernetesError):
    """Namespace operation failed.

    Raised when:
    - Namespace creation fails
    - Namespace deletion fails
    - Namespace doesn't exist when expected
    """
    pass


class ResourceError(KubernetesError):
    """Kubernetes resource operation failed.

    Raised when:
    - Resource creation/update/deletion fails
    - Resource not found
    - Resource in unexpected state

    Attributes:
        operation: The operation that failed (create, update, delete, get)
    """

    def __init__(
        self,
        message: str,
        operation: Optional[str] = None,
        **kwargs
    ):
        self.operation = operation
        super().__init__(message, **kwargs)


class ServiceError(KubernetesError):
    """Service-specific operation failed.

    Raised when:
    - Service creation fails
    - NodePort allocation fails
    - Service patching fails
    - Load balancer issues

    Attributes:
        service_type: Type of service (NodePort, LoadBalancer, etc.)
        port: The port that caused the issue
    """

    def __init__(
        self,
        message: str,
        service_type: Optional[str] = None,
        port: Optional[int] = None,
        **kwargs
    ):
        self.service_type = service_type
        self.port = port
        super().__init__(message, **kwargs)


class OpsManagerError(DeploymentError):
    """Ops Manager API or connectivity error.

    Base class for Ops Manager-related errors.

    Attributes:
        api_endpoint: The API endpoint that was called
        http_status: HTTP status code if applicable
        response_body: Response body if available
    """

    def __init__(
        self,
        message: str,
        api_endpoint: Optional[str] = None,
        http_status: Optional[int] = None,
        response_body: Optional[str] = None,
        **kwargs
    ):
        self.api_endpoint = api_endpoint
        self.http_status = http_status
        self.response_body = response_body
        super().__init__(message, **kwargs)


class AuthenticationError(OpsManagerError):
    """Authentication or authorization failed.

    Raised when:
    - API key is invalid
    - Credentials are missing
    - Insufficient permissions
    - Token expired
    """
    pass


class ProjectError(OpsManagerError):
    """Ops Manager project operation failed.

    Raised when:
    - Project creation fails
    - Project deletion fails
    - Project not found
    - Organization issues

    Attributes:
        project_id: ID of the affected project
        project_name: Name of the affected project
        org_id: Organization ID if applicable
    """

    def __init__(
        self,
        message: str,
        project_id: Optional[str] = None,
        project_name: Optional[str] = None,
        org_id: Optional[str] = None,
        **kwargs
    ):
        self.project_id = project_id
        self.project_name = project_name
        self.org_id = org_id
        super().__init__(message, **kwargs)


class CertificateError(DeploymentError):
    """TLS certificate generation or validation error.

    Raised when:
    - Certificate generation fails
    - Certificate validation fails
    - CA certificate issues
    - Certificate secret creation fails

    Attributes:
        cert_type: Type of certificate (CA, server, client)
        cert_path: Path to the certificate file
    """

    def __init__(
        self,
        message: str,
        cert_type: Optional[str] = None,
        cert_path: Optional[str] = None,
        **kwargs
    ):
        self.cert_type = cert_type
        self.cert_path = cert_path
        super().__init__(message, **kwargs)


class NetworkError(DeploymentError):
    """Network connectivity error.

    Raised when:
    - Cannot connect to a host/port
    - DNS resolution fails
    - Firewall blocking connection
    - TLS handshake fails

    Attributes:
        host: The host that couldn't be reached
        port: The port that couldn't be connected to
        protocol: Protocol used (TCP, TLS, etc.)
    """

    def __init__(
        self,
        message: str,
        host: Optional[str] = None,
        port: Optional[int] = None,
        protocol: Optional[str] = None,
        **kwargs
    ):
        self.host = host
        self.port = port
        self.protocol = protocol
        super().__init__(message, **kwargs)


class DeploymentTimeoutError(DeploymentError):
    """Operation timed out.

    Raised when:
    - Waiting for resource to be ready times out
    - API call times out
    - Health check times out

    Note: Named DeploymentTimeoutError to avoid conflict with builtin TimeoutError.

    Attributes:
        timeout_seconds: The timeout value that was exceeded
        operation: Description of the operation that timed out
        last_status: Last known status before timeout
    """

    def __init__(
        self,
        message: str,
        timeout_seconds: Optional[int] = None,
        operation: Optional[str] = None,
        last_status: Optional[str] = None,
        **kwargs
    ):
        self.timeout_seconds = timeout_seconds
        self.operation = operation
        self.last_status = last_status
        super().__init__(message, **kwargs)


class CleanupError(DeploymentError):
    """Cleanup operation failed.

    Raised when:
    - Resource deletion fails
    - Cannot remove cluster
    - Orphaned resources remain

    Attributes:
        resource_type: Type of resource being cleaned up
        resources_remaining: List of resources that couldn't be cleaned
    """

    def __init__(
        self,
        message: str,
        resource_type: Optional[str] = None,
        resources_remaining: Optional[list] = None,
        **kwargs
    ):
        self.resource_type = resource_type
        self.resources_remaining = resources_remaining or []
        super().__init__(message, **kwargs)


class DockerError(DeploymentError):
    """Docker operation failed.

    Raised when:
    - Docker daemon not running
    - Container start/stop fails
    - Image build fails
    - Volume/network issues

    Attributes:
        container_name: Name of the affected container
        docker_output: Raw output from Docker command
    """

    def __init__(
        self,
        message: str,
        container_name: Optional[str] = None,
        docker_output: Optional[str] = None,
        **kwargs
    ):
        self.container_name = container_name
        self.docker_output = docker_output
        super().__init__(message, **kwargs)


# Convenience function for creating errors with suggestions
def create_error_with_suggestion(
    error_class: type,
    message: str,
    suggestion: str,
    **kwargs
) -> DeploymentError:
    """Create an error instance with a suggestion for resolution.

    Args:
        error_class: The exception class to instantiate
        message: Error message
        suggestion: How to resolve the error
        **kwargs: Additional arguments for the error class

    Returns:
        Configured exception instance

    Example:
        raise create_error_with_suggestion(
            ClusterError,
            "Failed to create kind cluster",
            "Check if Docker is running: docker info"
        )
    """
    return error_class(message, suggestion=suggestion, **kwargs)


# Common error suggestions
SUGGESTIONS: Dict[str, str] = {
    # Docker issues
    "docker_not_running": "Start Docker Desktop or docker daemon",
    "docker_memory": "Increase Docker memory allocation in Docker Desktop settings (recommend 8GB+)",
    "docker_disk_space": "Free up disk space or prune Docker: docker system prune -a",

    # Tool installation
    "kubectl_not_found": "Install kubectl: https://kubernetes.io/docs/tasks/tools/",
    "kind_not_found": "Install kind: https://kind.sigs.k8s.io/docs/user/quick-start/",
    "openssl_not_found": "Install OpenSSL or ensure it's in your PATH",

    # Ops Manager issues
    "ops_manager_unreachable": "Verify Ops Manager is running: docker ps | grep ops-manager",
    "ops_manager_startup": "Wait for Ops Manager to fully start (check logs: docker logs ops-manager-1)",
    "ops_manager_login": "Open https://localhost:8443 and complete initial setup",

    # Credentials
    "credentials_missing": "Run deploy_ops_manager.py first to generate credentials",
    "credentials_invalid": "Regenerate API keys in Ops Manager UI and update ops_manager_api_key.json",
    "project_not_found": "The Ops Manager project may have been deleted. Re-run deployment.",

    # Cluster issues
    "cluster_exists": "Delete existing cluster: kind delete cluster --name <name>",
    "cluster_not_found": "Create the cluster first or check cluster name",
    "cluster_unreachable": "Check kubeconfig: kubectl cluster-info --context <context>",

    # Network issues
    "port_in_use": "Check what's using the port: netstat -an | find \"<port>\"",
    "mongodb_unreachable": "Check MongoDB pod status: kubectl get pods -n mongodb-rs",
    "tls_handshake_failed": "Verify TLS certificates are correct and not expired",
    "dns_resolution_failed": "Check DNS or add hosts to /etc/hosts",

    # Certificate issues
    "certificate_expired": "Regenerate certificates: python deploy_ops_manager.py --certs-only",
    "certificate_mismatch": "Certificate SANs don't match hostname. Regenerate with correct SANs.",
    "ca_not_trusted": "Add CA certificate to trust store or use --skip-verify",

    # Deployment issues
    "pods_not_ready": "Check pod events: kubectl describe pod <pod-name> -n <namespace>",
    "operator_not_ready": "Check operator logs: kubectl logs -n mongodb -l app.kubernetes.io/name=mongodb-kubernetes-operator",
    "agents_not_connected": "Check agent logs: kubectl logs <pod-name> -n mongodb-rs -c mongodb-enterprise-database",
    "pvc_pending": "Check storage class: kubectl get storageclass",

    # Cleanup issues
    "cleanup_failed": "Try manual cleanup: kind delete cluster --name <name>",
    "project_delete_failed": "Wait for agents to deregister or delete project manually in Ops Manager UI",
}
