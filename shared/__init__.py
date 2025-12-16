"""
Shared utilities for MongoDB Kubernetes deployment scripts.

This package contains common code used by both single-cluster and multi-cluster
deployment scripts to avoid duplication and improve maintainability.
"""

from shared.validators import (
    positive_int,
    non_negative_int,
    valid_port,
    valid_namespace,
    valid_timeout,
)
from shared.utils import (
    run_command,
    convert_path_for_docker,
    check_docker,
    find_openssl,
    run_openssl,
    generate_secure_password,
    validate_yaml,
)
from shared.models import OpsManagerCredentials
from shared.preflight import PreFlightChecker
from shared.decorators import (
    retry_with_backoff,
    poll_with_backoff,
    wait_for_condition,
    wait_for_value,
    BackoffStrategy,
    ConstantBackoff,
    LinearBackoff,
    ExponentialBackoff,
    ProgressiveBackoff,
    PollResult,
)
from shared.yaml_manager_base import BaseYAMLTemplateManager, TemplateCache
from shared.kind_manager_base import BaseKindManager
from shared.k8s_manager_base import BaseKubernetesManager
from shared.operator_deployer_base import BaseOperatorDeployer
from shared.ops_manager_cleanup import OpsManagerCleanup, cleanup_ops_manager_project, delete_all_projects_by_name
from shared.certificate_manager import (
    CertificateManager,
    generate_single_cluster_sans,
    generate_multi_cluster_sans,
)
from shared.x509_manager import X509CertificateManager
from shared.health_check import (
    MongoDBHealthChecker,
    HealthCheckResult,
    verify_mongodb_deployment,
    format_health_check_result,
)
from shared.ui_utils import (
    mask_password,
    mask_sensitive,
    ProgressSpinner,
    ProgressBar,
    CountdownTimer,
    format_error_with_suggestion,
    format_success_message,
    print_step,
    print_section_header,
    DeploymentSummary,
    StepTracker,
    format_duration,
    format_bytes,
)
from shared import constants
from shared.exceptions import (
    DeploymentError,
    ConfigurationError,
    PreflightError,
    KubernetesError,
    ClusterError,
    NamespaceError,
    ResourceError,
    ServiceError,
    OpsManagerError,
    AuthenticationError,
    ProjectError,
    CertificateError,
    NetworkError,
    DeploymentTimeoutError,
    CleanupError,
    DockerError,
    create_error_with_suggestion,
    SUGGESTIONS,
)
from shared.cleanup import (
    CleanupManager,
    cleanup_generated_files,
    cleanup_kind_cluster,
    cleanup_docker_containers,
    cleanup_docker_network,
    cleanup_kubernetes_deployment,
    delete_mongodb_crd,
    wait_for_pods_deleted,
)
from shared.result import (
    Result,
    Ok,
    Err,
    CommandResult,
    OperationResult,
    operation_ok,
    operation_err,
)
from shared.user_manager import (
    MongoDBUserManager,
    UserCreationResult,
    create_mongodb_users,
)
from shared.logging_config import (
    setup_logging,
    get_logger,
    set_verbose,
    add_file_handler,
    create_session_log_file,
    VerboseLogging,
    log_step,
    log_success,
    log_warning,
    log_error,
)

__all__ = [
    # Validators
    'positive_int',
    'non_negative_int',
    'valid_port',
    'valid_namespace',
    'valid_timeout',
    # Utils
    'run_command',
    'convert_path_for_docker',
    'check_docker',
    'find_openssl',
    'run_openssl',
    'generate_secure_password',
    'validate_yaml',
    # Models
    'OpsManagerCredentials',
    # Preflight
    'PreFlightChecker',
    # Decorators and Polling
    'retry_with_backoff',
    'poll_with_backoff',
    'wait_for_condition',
    'wait_for_value',
    'BackoffStrategy',
    'ConstantBackoff',
    'LinearBackoff',
    'ExponentialBackoff',
    'ProgressiveBackoff',
    'PollResult',
    # YAML Management
    'BaseYAMLTemplateManager',
    'TemplateCache',
    # Kind Management
    'BaseKindManager',
    # Kubernetes Management
    'BaseKubernetesManager',
    # Operator Deployment
    'BaseOperatorDeployer',
    # Ops Manager Cleanup
    'OpsManagerCleanup',
    'cleanup_ops_manager_project',
    'delete_all_projects_by_name',
    # Certificate Management
    'CertificateManager',
    'generate_single_cluster_sans',
    'generate_multi_cluster_sans',
    # X509 Management
    'X509CertificateManager',
    # Health Check
    'MongoDBHealthChecker',
    'HealthCheckResult',
    'verify_mongodb_deployment',
    'format_health_check_result',
    # UI Utilities
    'mask_password',
    'mask_sensitive',
    'ProgressSpinner',
    'ProgressBar',
    'CountdownTimer',
    'format_error_with_suggestion',
    'format_success_message',
    'print_step',
    'print_section_header',
    'DeploymentSummary',
    'StepTracker',
    'format_duration',
    'format_bytes',
    # Constants
    'constants',
    # Exceptions
    'DeploymentError',
    'ConfigurationError',
    'PreflightError',
    'KubernetesError',
    'ClusterError',
    'NamespaceError',
    'ResourceError',
    'ServiceError',
    'OpsManagerError',
    'AuthenticationError',
    'ProjectError',
    'CertificateError',
    'NetworkError',
    'DeploymentTimeoutError',
    'CleanupError',
    'DockerError',
    'create_error_with_suggestion',
    'SUGGESTIONS',
    # Cleanup
    'CleanupManager',
    'cleanup_generated_files',
    'cleanup_kind_cluster',
    'cleanup_docker_containers',
    'cleanup_docker_network',
    'cleanup_kubernetes_deployment',
    'delete_mongodb_crd',
    'wait_for_pods_deleted',
    # Result types
    'Result',
    'Ok',
    'Err',
    'CommandResult',
    'OperationResult',
    'operation_ok',
    'operation_err',
    # User Management
    'MongoDBUserManager',
    'UserCreationResult',
    'create_mongodb_users',
    # Logging
    'setup_logging',
    'get_logger',
    'set_verbose',
    'add_file_handler',
    'create_session_log_file',
    'VerboseLogging',
    'log_step',
    'log_success',
    'log_warning',
    'log_error',
]
