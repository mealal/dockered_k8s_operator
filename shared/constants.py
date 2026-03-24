"""
Centralized constants for MongoDB Kubernetes deployment scripts.

Contains all timeouts, ports, paths, and other configuration values
that were previously scattered throughout the codebase.

Usage:
    from shared import constants
    from shared.constants import API_TIMEOUT, MONGODB_PORT

    # Access values directly
    timeout = constants.API_TIMEOUT

    # Use helper functions
    certs_dir = constants.get_certs_dir(script_dir)
    kubeconfig = constants.get_kubeconfig_dir(script_dir, multi_cluster=True)

Sections:
    - Timeouts: All timeout values in seconds
    - Ports: Network ports for services
    - Multi-Cluster Virtual IPs: Cross-cluster routing configuration
    - Kubernetes Resources: Default names for K8s resources
    - MongoDB Configuration: Version and replica set defaults
    - Certificate Configuration: TLS certificate settings
    - Paths: Directory and file path constants
    - External Domains: DNS domain names for multi-cluster
    - Docker Configuration: Container names and settings
    - Progress Display: UI animation settings
    - Password Generation: Character sets for secure passwords
"""

from pathlib import Path

# =============================================================================
# Timeouts (in seconds)
# =============================================================================

# API and network timeouts
API_TIMEOUT = 30
HEALTH_CHECK_TIMEOUT = 10
CONNECTION_TIMEOUT = 10
KUBECTL_TIMEOUT = 30

# Deployment wait timeouts
OPS_MANAGER_STARTUP_TIMEOUT = 300
MONGODB_STARTUP_TIMEOUT = 600
OPERATOR_READY_TIMEOUT = 180
SERVICE_CREATION_TIMEOUT = 120
NAMESPACE_CREATION_TIMEOUT = 60

# Cleanup timeouts
CRD_DELETION_TIMEOUT = 180  # Time to wait for MongoDB CRD deletion
POD_DELETION_TIMEOUT = 120  # Time to wait for pods to terminate
OPS_MANAGER_CLEANUP_TIMEOUT = 120  # Time for Ops Manager project deletion
AGENT_DEREGISTRATION_WAIT = 30  # Time to wait for agents to deregister

# Retry configuration
OPS_MANAGER_DELETE_RETRIES = 12
OPS_MANAGER_DELETE_RETRY_DELAY = 10

# Polling intervals
HEALTH_CHECK_INTERVAL = 10
MONGODB_STATUS_POLL_INTERVAL = 15
CRD_POLL_INTERVAL = 5
POD_POLL_INTERVAL = 5

# =============================================================================
# Ports
# =============================================================================

# Ops Manager ports
OPS_MANAGER_HTTP_PORT = 8080
OPS_MANAGER_HTTPS_PORT = 8443
OPS_MANAGER_HEALTH_PORT = 8090

# MongoDB ports
MONGODB_PORT = 27017
MONGODB_AGENT_PORT = 27018

# Default NodePort ranges for Kind clusters
SINGLE_CLUSTER_NODEPORT_START = 30000
MULTI_CLUSTER_CENTRAL_NODEPORT_START = 30100
MULTI_CLUSTER_MEMBER_NODEPORT_START = 30200

# =============================================================================
# Multi-Cluster Virtual IPs (for cross-cluster routing)
# =============================================================================

# These IPs are used for iptables routing between Kind clusters
# They must be within the Docker network range but not conflict with real IPs
CENTRAL_VIRTUAL_IP_BASE = "172.19.0.200"
MEMBER_VIRTUAL_IP_BASE = "172.19.0.100"

# =============================================================================
# Kubernetes Resources
# =============================================================================

# Default namespace names
DEFAULT_OPERATOR_NAMESPACE = "mongodb"
DEFAULT_RS_NAMESPACE = "mongodb-rs"

# Default cluster names
DEFAULT_SINGLE_CLUSTER_NAME = "mongodb-k8s"
DEFAULT_CENTRAL_CLUSTER_NAME = "mongodb-central"
DEFAULT_MEMBER_CLUSTER_NAME = "mongodb-member-1"

# Default replica set names
DEFAULT_SINGLE_RS_NAME = "mongodb-rs"
DEFAULT_MULTI_RS_NAME = "mongodb-multi-rs"

# =============================================================================
# Operator Configuration (MCK - MongoDB Controllers for Kubernetes)
# =============================================================================

# MCK operator version (replaces MEKO 1.33.0)
DEFAULT_OPERATOR_VERSION = "1.7.0"

# Helm chart configuration
HELM_REPO_NAME = "mongodb"
HELM_REPO_URL = "https://mongodb.github.io/helm-charts"
HELM_CHART_NAME = "mongodb/mongodb-kubernetes"

# Operator deployment names (MCK naming convention)
OPERATOR_DEPLOYMENT_NAME = "mongodb-kubernetes-operator"
OPERATOR_RELEASE_NAME = "mongodb-kubernetes-operator"

# Operator labels
OPERATOR_LABEL_SELECTOR = "app.kubernetes.io/name=mongodb-kubernetes-operator"

# Service account names (MCK naming convention)
OPERATOR_SERVICE_ACCOUNT = "mongodb-kubernetes-operator"
DATABASE_PODS_SERVICE_ACCOUNT = "mongodb-kubernetes-database-pods"
APPDB_SERVICE_ACCOUNT = "mongodb-kubernetes-appdb"

# Multi-cluster kubeconfig secret name (MCK default)
MULTI_CLUSTER_KUBECONFIG_SECRET = "mongodb-kubernetes-operator-multi-cluster-kubeconfig"

# =============================================================================
# kubectl-mongodb Plugin Configuration
# =============================================================================

# Plugin version (from MCK releases)
KUBECTL_MONGODB_PLUGIN_VERSION = "1.7.0"

# Plugin download URLs by platform
# Note: Windows is NOT officially supported by MongoDB - plugin not available
KUBECTL_MONGODB_PLUGIN_URLS = {
    "linux": f"https://github.com/mongodb/mongodb-kubernetes/releases/download/MCK-{KUBECTL_MONGODB_PLUGIN_VERSION}/kubectl-mongodb_{KUBECTL_MONGODB_PLUGIN_VERSION}_linux_amd64.tar.gz",
    "darwin": f"https://github.com/mongodb/mongodb-kubernetes/releases/download/MCK-{KUBECTL_MONGODB_PLUGIN_VERSION}/kubectl-mongodb_{KUBECTL_MONGODB_PLUGIN_VERSION}_darwin_amd64.tar.gz",
}

# =============================================================================
# Docker Images
# =============================================================================

# kubectl Docker image for running kubectl commands (version-pinned for stability)
KUBECTL_DOCKER_IMAGE = "bitnami/kubectl:1.28"

# Kind node image (version-pinned for stability with tested K8s version)
KIND_NODE_IMAGE = "kindest/node:v1.28.0"

# =============================================================================
# MongoDB Configuration
# =============================================================================

# Default MongoDB version
DEFAULT_MONGODB_VERSION = "7.0.25-ent"

# Default replica set member counts
DEFAULT_SINGLE_CLUSTER_MEMBERS = 3
DEFAULT_MULTI_CENTRAL_MEMBERS = 3
DEFAULT_MULTI_MEMBER_MEMBERS = 2

# =============================================================================
# Certificate Configuration
# =============================================================================

# Certificate validity periods (in days)
CA_CERT_VALIDITY_DAYS = 3650  # 10 years
SERVER_CERT_VALIDITY_DAYS = 365  # 1 year
CLIENT_CERT_VALIDITY_DAYS = 365  # 1 year

# Certificate subject defaults
DEFAULT_CERT_ORG = "MongoDB"
DEFAULT_CERT_OU = "clients"
DEFAULT_CERT_LOCALITY = "New York"
DEFAULT_CERT_STATE = "NY"
DEFAULT_CERT_COUNTRY = "US"

# =============================================================================
# Paths (relative to script directory)
# =============================================================================

# Certificate directories
CERTS_DIR = "certs"
MONGODB_CERTS_SUBDIR = "mongodb"
MONGODB_MULTI_CERTS_SUBDIR = "mongodb-multi"

# Kubeconfig directories
SINGLE_CLUSTER_KUBE_DIR = ".kube"
MULTI_CLUSTER_KUBE_DIR = ".kube-multi"

# Credentials file
DEFAULT_CREDENTIALS_FILE = "ops-manager-api-key.json"

# YAML template directories
SINGLE_CLUSTER_TEMPLATES = "k8s"
MULTI_CLUSTER_TEMPLATES = "k8s-multi"

# =============================================================================
# External Domains (for cross-cluster DNS)
# =============================================================================

DEFAULT_CENTRAL_EXTERNAL_DOMAIN = "central.mongodb.local"
DEFAULT_MEMBER_EXTERNAL_DOMAIN = "member1.mongodb.local"

# =============================================================================
# Docker Configuration
# =============================================================================

# Ops Manager Docker image
OPS_MANAGER_IMAGE_NAME = "ops-manager"
OPS_MANAGER_CONTAINER_NAME = "ops-manager"
MONGODB_APPDB_CONTAINER_NAME = "mongodb-appdb"

# Docker network
DEFAULT_DOCKER_NETWORK = "bridge"

# =============================================================================
# Progress Display
# =============================================================================

# Characters for progress indication
PROGRESS_SPINNER = ['|', '/', '-', '\\']
PROGRESS_BAR_WIDTH = 40

# =============================================================================
# Password Generation
# =============================================================================

# Password requirements
MIN_PASSWORD_LENGTH = 16
PASSWORD_SPECIAL_CHARS = "!@#$%^&*"
PASSWORD_DIGITS = "0123456789"
PASSWORD_UPPERCASE = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
PASSWORD_LOWERCASE = "abcdefghijklmnopqrstuvwxyz"


def get_certs_dir(script_dir: Path) -> Path:
    """Get the certificates directory path.

    Args:
        script_dir: Base directory of the deployment scripts

    Returns:
        Path to the certificates directory (e.g., /path/to/project/certs)
    """
    return script_dir / CERTS_DIR


def get_mongodb_certs_dir(script_dir: Path, multi_cluster: bool = False) -> Path:
    """Get the MongoDB certificates directory path.

    Args:
        script_dir: Base directory of the deployment scripts
        multi_cluster: If True, return multi-cluster cert path

    Returns:
        Path to MongoDB certs (e.g., /path/to/project/certs/mongodb
        or /path/to/project/certs/mongodb-multi)
    """
    subdir = MONGODB_MULTI_CERTS_SUBDIR if multi_cluster else MONGODB_CERTS_SUBDIR
    return script_dir / CERTS_DIR / subdir


def get_kubeconfig_dir(script_dir: Path, multi_cluster: bool = False) -> Path:
    """Get the kubeconfig directory path.

    Args:
        script_dir: Base directory of the deployment scripts
        multi_cluster: If True, return multi-cluster kubeconfig path

    Returns:
        Path to kubeconfig directory (e.g., /path/to/project/.kube
        or /path/to/project/.kube-multi)
    """
    subdir = MULTI_CLUSTER_KUBE_DIR if multi_cluster else SINGLE_CLUSTER_KUBE_DIR
    return script_dir / subdir


def get_credentials_path(script_dir: Path) -> Path:
    """Get the credentials file path.

    Args:
        script_dir: Base directory of the deployment scripts

    Returns:
        Path to the Ops Manager API credentials JSON file
    """
    return script_dir / DEFAULT_CREDENTIALS_FILE
