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
    generate_secure_password,
    validate_yaml,
)
from shared.models import OpsManagerCredentials
from shared.preflight import PreFlightChecker
from shared.decorators import retry_with_backoff
from shared.yaml_manager_base import BaseYAMLTemplateManager
from shared.kind_manager_base import BaseKindManager
from shared.k8s_manager_base import BaseKubernetesManager
from shared.operator_deployer_base import BaseOperatorDeployer
from shared.ops_manager_cleanup import OpsManagerCleanup, cleanup_ops_manager_project

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
    'generate_secure_password',
    'validate_yaml',
    # Models
    'OpsManagerCredentials',
    # Preflight
    'PreFlightChecker',
    # Decorators
    'retry_with_backoff',
    # YAML Management
    'BaseYAMLTemplateManager',
    # Kind Management
    'BaseKindManager',
    # Kubernetes Management
    'BaseKubernetesManager',
    # Operator Deployment
    'BaseOperatorDeployer',
    # Ops Manager Cleanup
    'OpsManagerCleanup',
    'cleanup_ops_manager_project',
]
