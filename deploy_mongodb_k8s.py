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
import sys
import time
import argparse
import json
import logging
import os
import re
import shutil
import ssl
import urllib.request
import urllib.error
from pathlib import Path
from dataclasses import dataclass
from typing import Optional, List, Callable, Dict
from functools import wraps

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger(__name__)

# Constants - Docker images for kind and kubectl
KIND_NODE_IMAGE = "kindest/node:v1.28.0"
KIND_IMAGE = "kindest/node:v1.28.0"  # We'll use alpine/k8s for kind CLI
KUBECTL_IMAGE = "bitnami/kubectl:1.28"
OPERATOR_VERSION = "1.33.0"
MONGODB_VERSION = "7.0.25-ent"

# Official MongoDB Enterprise Kubernetes Operator installation URLs
OPERATOR_CRDS_URL = "https://raw.githubusercontent.com/mongodb/mongodb-enterprise-kubernetes/master/crds.yaml"
OPERATOR_INSTALL_URL = "https://raw.githubusercontent.com/mongodb/mongodb-enterprise-kubernetes/master/mongodb-enterprise.yaml"

# YAML template directory
K8S_YAML_DIR = Path("./k8s")


# =============================================================================
# YAML Template Manager
# =============================================================================

class YAMLTemplateManager:
    """Manages YAML template files for Kubernetes resources."""

    def __init__(self, template_dir: Path = K8S_YAML_DIR):
        self.template_dir = template_dir
        self.generated_dir = template_dir / "generated"
        self._ensure_dirs()

    def _ensure_dirs(self) -> None:
        """Ensure template and generated directories exist."""
        self.template_dir.mkdir(parents=True, exist_ok=True)
        self.generated_dir.mkdir(parents=True, exist_ok=True)

    def render_template(self, template_name: str, variables: Dict[str, str],
                        output_name: Optional[str] = None) -> Path:
        """
        Render a YAML template with variable substitution.

        Args:
            template_name: Name of the template file (e.g., 'ops-manager-secret.yaml')
            variables: Dictionary of variables to substitute ({{KEY}} -> value)
            output_name: Optional output filename (defaults to template_name)

        Returns:
            Path to the generated YAML file
        """
        template_path = self.template_dir / template_name
        if not template_path.exists():
            raise FileNotFoundError(f"Template not found: {template_path}")

        # Read template
        template_content = template_path.read_text(encoding='utf-8')

        # Substitute variables
        rendered_content = template_content
        for key, value in variables.items():
            placeholder = f"{{{{{key}}}}}"  # {{KEY}}
            rendered_content = rendered_content.replace(placeholder, str(value))

        # Check for unsubstituted placeholders
        remaining = re.findall(r'\{\{[A-Z_]+\}\}', rendered_content)
        if remaining:
            logger.warning(f"Unsubstituted placeholders in {template_name}: {remaining}")

        # Write generated file
        output_path = self.generated_dir / (output_name or template_name)
        output_path.write_text(rendered_content, encoding='utf-8')
        logger.debug(f"Generated: {output_path}")

        return output_path

    def render_namespace(self, namespace: str, template_name: str = "namespace.yaml",
                         output_name: str = "namespace.yaml") -> Path:
        """Render namespace.yaml with the specified namespace name."""
        template_path = self.template_dir / template_name
        content = template_path.read_text(encoding='utf-8')

        # Replace namespace name using regex to handle any default name
        content = re.sub(r'(name: )(mongodb(?:-rs)?)\n', f'\\g<1>{namespace}\n', content)

        output_path = self.generated_dir / output_name
        output_path.write_text(content, encoding='utf-8')
        return output_path

    def render_operator_namespace(self, namespace: str) -> Path:
        """Render operator namespace.yaml."""
        return self.render_namespace(namespace, "namespace.yaml", "namespace.yaml")

    def render_rs_namespace(self, namespace: str) -> Path:
        """Render replica set namespace.yaml."""
        return self.render_namespace(namespace, "mongodb-rs-namespace.yaml", "mongodb-rs-namespace.yaml")

    def render_secret(self, namespace: str, public_key: str, private_key: str) -> Path:
        """Render ops-manager-secret.yaml with credentials."""
        variables = {
            "PUBLIC_KEY": public_key,
            "PRIVATE_KEY": private_key,
        }

        # Read template and also update namespace
        template_path = self.template_dir / "ops-manager-secret.yaml"
        content = template_path.read_text(encoding='utf-8')

        # Substitute variables
        for key, value in variables.items():
            content = content.replace(f"{{{{{key}}}}}", value)

        # Update namespace (template default is mongodb-rs)
        content = re.sub(r'namespace: mongodb-rs', f'namespace: {namespace}', content)

        output_path = self.generated_dir / "ops-manager-secret.yaml"
        output_path.write_text(content, encoding='utf-8')
        return output_path

    def render_configmap(self, namespace: str, base_url: str, project_name: str,
                         org_id: str) -> Path:
        """Render ops-manager-configmap.yaml with connection details."""
        variables = {
            "BASE_URL": base_url,
            "PROJECT_NAME": project_name,
            "ORG_ID": org_id,
        }

        template_path = self.template_dir / "ops-manager-configmap.yaml"
        content = template_path.read_text(encoding='utf-8')

        for key, value in variables.items():
            content = content.replace(f"{{{{{key}}}}}", value)

        # Update namespace (template default is mongodb-rs)
        content = re.sub(r'namespace: mongodb-rs', f'namespace: {namespace}', content)

        output_path = self.generated_dir / "ops-manager-configmap.yaml"
        output_path.write_text(content, encoding='utf-8')
        return output_path

    def render_ca_configmap(self, namespace: str, ca_cert_path: Path) -> Path:
        """Render ops-manager-ca-configmap.yaml with CA certificate."""
        if not ca_cert_path.exists():
            raise FileNotFoundError(f"CA certificate not found: {ca_cert_path}")

        ca_cert_content = ca_cert_path.read_text(encoding='utf-8').strip()

        # Indent the certificate for YAML embedding
        indented_cert = "\n".join("    " + line for line in ca_cert_content.split("\n"))

        template_path = self.template_dir / "ops-manager-ca-configmap.yaml"
        content = template_path.read_text(encoding='utf-8')

        # Replace placeholder with indented certificate
        content = content.replace("    {{CA_CERTIFICATE}}", indented_cert)
        # Update namespace (template default is mongodb-rs)
        content = re.sub(r'namespace: mongodb-rs', f'namespace: {namespace}', content)

        output_path = self.generated_dir / "ops-manager-ca-configmap.yaml"
        output_path.write_text(content, encoding='utf-8')
        return output_path

    def render_replicaset(self, namespace: str, rs_name: str, members: int,
                          version: str, cpu_request: str, memory_request: str,
                          storage_size: str) -> Path:
        """Render mongodb-replicaset.yaml with replica set configuration."""
        variables = {
            "REPLICA_SET_NAME": rs_name,
            "MEMBERS": str(members),
            "VERSION": version,
            "CPU_REQUEST": cpu_request,
            "MEMORY_REQUEST": memory_request,
            "STORAGE_SIZE": storage_size,
        }

        template_path = self.template_dir / "mongodb-replicaset.yaml"
        content = template_path.read_text(encoding='utf-8')

        for key, value in variables.items():
            content = content.replace(f"{{{{{key}}}}}", value)

        # Update namespace (template default is mongodb-rs)
        content = re.sub(r'namespace: mongodb-rs', f'namespace: {namespace}', content)

        output_path = self.generated_dir / "mongodb-replicaset.yaml"
        output_path.write_text(content, encoding='utf-8')
        return output_path

    def get_generated_files(self) -> List[Path]:
        """Get list of all generated YAML files."""
        if not self.generated_dir.exists():
            return []
        return sorted(self.generated_dir.glob("*.yaml"))

    def clean_generated(self) -> None:
        """Remove all generated YAML files."""
        if self.generated_dir.exists():
            shutil.rmtree(self.generated_dir)
            self.generated_dir.mkdir(parents=True, exist_ok=True)
            logger.info("Cleaned generated YAML files")


# =============================================================================
# Utility Functions
# =============================================================================

def retry_with_backoff(max_retries: int = 3, base_delay: float = 2.0,
                       max_delay: float = 30.0, exceptions: tuple = (Exception,)):
    """Decorator for retrying operations with exponential backoff."""
    def decorator(func: Callable):
        @wraps(func)
        def wrapper(*args, **kwargs):
            last_exception = None
            delay = base_delay
            for attempt in range(max_retries):
                try:
                    return func(*args, **kwargs)
                except exceptions as e:
                    last_exception = e
                    if attempt < max_retries - 1:
                        logger.warning(f"Attempt {attempt + 1}/{max_retries} failed: {e}. Retrying in {delay:.1f}s...")
                        time.sleep(delay)
                        delay = min(delay * 2, max_delay)
                    else:
                        logger.error(f"All {max_retries} attempts failed")
            raise last_exception
        return wrapper
    return decorator


class PreFlightChecker:
    """Pre-flight validation checks before deployment."""

    def __init__(self, ops_manager_url: str, credentials_file: str, ca_cert_path: str = "./certs/ca.crt"):
        self.ops_manager_url = ops_manager_url
        self.credentials_file = credentials_file
        self.ca_cert_path = ca_cert_path
        self.errors: List[str] = []
        self.warnings: List[str] = []

    def check_all(self) -> bool:
        """Run all pre-flight checks. Returns True if all critical checks pass."""
        logger.info("Running pre-flight checks...")

        checks = [
            ("Docker running", self._check_docker),
            ("Credentials file exists", self._check_credentials_file),
            ("CA certificate exists", self._check_ca_cert),
            ("Ops Manager reachable", self._check_ops_manager_connectivity),
        ]

        all_passed = True
        for check_name, check_func in checks:
            try:
                result = check_func()
                status = "✓" if result else "✗"
                level = "PASS" if result else "FAIL"
                logger.info(f"  [{status}] {check_name}: {level}")
                if not result:
                    all_passed = False
            except Exception as e:
                logger.error(f"  [✗] {check_name}: ERROR - {e}")
                self.errors.append(f"{check_name}: {e}")
                all_passed = False

        if self.warnings:
            logger.warning("Warnings:")
            for warning in self.warnings:
                logger.warning(f"  - {warning}")

        if self.errors:
            logger.error("Errors:")
            for error in self.errors:
                logger.error(f"  - {error}")

        return all_passed

    def _check_docker(self) -> bool:
        """Check if Docker is running."""
        try:
            result = subprocess.run(
                ["docker", "info"],
                capture_output=True,
                text=True,
                timeout=10
            )
            return result.returncode == 0
        except (FileNotFoundError, subprocess.TimeoutExpired):
            self.errors.append("Docker is not running or not installed")
            return False

    def _check_credentials_file(self) -> bool:
        """Check if credentials file exists and is valid."""
        path = Path(self.credentials_file)
        if not path.exists():
            self.errors.append(f"Credentials file not found: {self.credentials_file}")
            return False

        try:
            with open(path) as f:
                data = json.load(f)
            required_fields = ['publicKey', 'privateKey', 'orgId', 'projectId']
            missing = [f for f in required_fields if f not in data]
            if missing:
                self.errors.append(f"Missing fields in credentials: {missing}")
                return False
            return True
        except json.JSONDecodeError as e:
            self.errors.append(f"Invalid JSON in credentials file: {e}")
            return False

    def _check_ca_cert(self) -> bool:
        """Check if CA certificate exists."""
        path = Path(self.ca_cert_path)
        if not path.exists():
            self.errors.append(f"CA certificate not found: {self.ca_cert_path}")
            self.errors.append("Run deploy_ops_manager.py first to generate certificates")
            return False
        return True

    def _check_ops_manager_connectivity(self) -> bool:
        """Check if Ops Manager is reachable."""
        # Create SSL context that trusts our custom CA
        ssl_context = ssl.create_default_context()

        ca_path = Path(self.ca_cert_path)
        if ca_path.exists():
            ssl_context.load_verify_locations(str(ca_path))
        else:
            # Fall back to not verifying if CA doesn't exist
            ssl_context.check_hostname = False
            ssl_context.verify_mode = ssl.CERT_NONE

        # Try to connect to Ops Manager
        # Use localhost since we're checking from the host machine
        test_url = self.ops_manager_url.replace("host.docker.internal", "localhost")

        try:
            req = urllib.request.Request(f"{test_url}/user/login")
            with urllib.request.urlopen(req, context=ssl_context, timeout=10) as response:
                return response.status in [200, 302, 303]
        except urllib.error.HTTPError as e:
            # 401/403 still means the server is reachable
            if e.code in [401, 403]:
                return True
            self.warnings.append(f"Ops Manager returned HTTP {e.code}")
            return True  # Server is reachable but returned an error
        except Exception as e:
            self.errors.append(f"Cannot reach Ops Manager at {test_url}: {e}")
            self.errors.append("Make sure Ops Manager is running (python deploy_ops_manager.py)")
            return False


@dataclass
class OpsManagerCredentials:
    """Credentials for connecting to Ops Manager."""
    public_key: str
    private_key: str
    base_url: str
    org_id: str
    project_id: str
    project_name: str = "Default"

    @classmethod
    def from_file(cls, filepath: str) -> 'OpsManagerCredentials':
        """Load credentials from ops-manager-api-key.json."""
        with open(filepath, 'r') as f:
            data = json.load(f)
        return cls(
            public_key=data['publicKey'],
            private_key=data['privateKey'],
            base_url=data['baseUrl'],
            org_id=data['orgId'],
            project_id=data['projectId'],
            project_name=data.get('projectName', 'Default')
        )


@dataclass
class ClusterConfig:
    """Configuration for the Kubernetes cluster."""
    name: str = "mongodb-k8s"
    operator_namespace: str = "mongodb"      # Namespace for the operator
    rs_namespace: str = "mongodb-rs"         # Namespace for replica sets
    worker_nodes: int = 1
    ops_manager_url: str = "https://host.docker.internal:8443"
    kubeconfig_dir: str = "./.kube"

    @property
    def namespace(self) -> str:
        """Backward compatibility: returns operator namespace."""
        return self.operator_namespace


@dataclass
class ReplicaSetConfig:
    """Configuration for the MongoDB replica set."""
    name: str = "mongodb-rs"
    members: int = 3
    version: str = MONGODB_VERSION
    cpu_request: str = "500m"
    memory_request: str = "1Gi"
    storage_size: str = "5Gi"


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
        except Exception:
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


def run_command(cmd: List[str], check: bool = True, capture: bool = True,
                timeout: Optional[int] = None, input_data: Optional[str] = None) -> subprocess.CompletedProcess:
    """Run a shell command."""
    logger.debug(f"Running: {' '.join(cmd)}")
    try:
        result = subprocess.run(
            cmd,
            capture_output=capture,
            text=True,
            timeout=timeout,
            input=input_data
        )
        if check and result.returncode != 0:
            logger.error(f"Command failed: {result.stderr}")
            raise subprocess.CalledProcessError(result.returncode, cmd, result.stdout, result.stderr)
        return result
    except subprocess.TimeoutExpired:
        logger.error(f"Command timed out: {' '.join(cmd)}")
        raise
    except FileNotFoundError:
        # Command not found
        result = subprocess.CompletedProcess(cmd, 1, "", "Command not found")
        if check:
            raise
        return result


def convert_path_for_docker(path: Path) -> str:
    """Convert Windows path to Docker-compatible path."""
    path_str = str(path.resolve())
    if sys.platform == "win32":
        # Convert D:\path to /d/path for Docker
        if len(path_str) >= 2 and path_str[1] == ':':
            drive = path_str[0].lower()
            return f"/{drive}{path_str[2:].replace(os.sep, '/')}"
    return path_str


class KindManager:
    """Manages kind Kubernetes cluster - downloads kind binary if not found."""

    KIND_VERSION = "v0.20.0"
    KIND_DOWNLOAD_URLS = {
        "win32": f"https://kind.sigs.k8s.io/dl/v0.20.0/kind-windows-amd64",
        "linux": f"https://kind.sigs.k8s.io/dl/v0.20.0/kind-linux-amd64",
        "darwin": f"https://kind.sigs.k8s.io/dl/v0.20.0/kind-darwin-amd64",
    }

    def __init__(self, config: ClusterConfig):
        self.config = config
        self.kubeconfig_path = Path(config.kubeconfig_dir).resolve()
        self.kubeconfig_file = self.kubeconfig_path / "config"
        self.kind_config_file = self.kubeconfig_path / "kind-config.yaml"
        self.kubeconfig_path.mkdir(parents=True, exist_ok=True)

        # Local kind binary path
        kind_ext = ".exe" if sys.platform == "win32" else ""
        self.local_kind_binary = self.kubeconfig_path / f"kind{kind_ext}"

        # Check if kind is available (native or local)
        self.kind_binary = self._get_kind_binary()

    def _get_kind_binary(self) -> str:
        """Get path to kind binary, download if necessary."""
        # Check native kind first
        try:
            result = run_command(["kind", "version"], check=False)
            if result.returncode == 0:
                logger.info("Using system kind")
                return "kind"
        except FileNotFoundError:
            pass

        # Check local kind binary
        if self.local_kind_binary.exists():
            logger.info(f"Using local kind binary: {self.local_kind_binary}")
            return str(self.local_kind_binary)

        # Download kind
        logger.info("kind not found, downloading...")
        return self._download_kind()

    def _download_kind(self) -> str:
        """Download kind binary for current platform."""
        import urllib.request

        platform = sys.platform
        if platform not in self.KIND_DOWNLOAD_URLS:
            raise RuntimeError(f"Unsupported platform: {platform}")

        url = self.KIND_DOWNLOAD_URLS[platform]
        logger.info(f"Downloading kind from: {url}")

        try:
            urllib.request.urlretrieve(url, str(self.local_kind_binary))

            # Make executable on Unix
            if sys.platform != "win32":
                import stat
                self.local_kind_binary.chmod(
                    self.local_kind_binary.stat().st_mode | stat.S_IEXEC
                )

            logger.info(f"kind downloaded to: {self.local_kind_binary}")
            return str(self.local_kind_binary)
        except Exception as e:
            raise RuntimeError(f"Failed to download kind: {e}")

    def _run_kind(self, args: List[str], check: bool = True) -> subprocess.CompletedProcess:
        """Run kind command."""
        return run_command([self.kind_binary] + args, check=check, timeout=300)

    def _create_kind_config(self) -> None:
        """Create kind cluster configuration file."""
        config_content = f"""kind: Cluster
apiVersion: kind.x-k8s.io/v1alpha4
name: {self.config.name}
nodes:
  - role: control-plane
    extraPortMappings:
      - containerPort: 30000
        hostPort: 30000
        protocol: TCP
      - containerPort: 30001
        hostPort: 30001
        protocol: TCP
      - containerPort: 30002
        hostPort: 30002
        protocol: TCP
"""
        for _ in range(self.config.worker_nodes):
            config_content += "  - role: worker\n"

        self.kind_config_file.write_text(config_content)
        logger.info(f"Created kind config: {self.kind_config_file}")

    def cluster_exists(self) -> bool:
        """Check if cluster already exists."""
        try:
            result = self._run_kind(["get", "clusters"], check=False)
            return self.config.name in result.stdout.split('\n')
        except Exception:
            return False

    def create_cluster(self) -> bool:
        """Create the kind cluster."""
        logger.info(f"Creating kind cluster: {self.config.name}")

        if self.cluster_exists():
            logger.info(f"Cluster {self.config.name} already exists")
            self._export_kubeconfig()
            return True

        self._create_kind_config()

        try:
            self._run_kind([
                "create", "cluster",
                "--name", self.config.name,
                "--config", str(self.kind_config_file),
                "--wait", "120s"
            ])

            logger.info(f"Cluster {self.config.name} created successfully")
            self._export_kubeconfig()
            return True
        except Exception as e:
            logger.error(f"Failed to create cluster: {e}")
            return False

    def _export_kubeconfig(self) -> None:
        """Export kubeconfig for the cluster."""
        try:
            result = self._run_kind(["get", "kubeconfig", "--name", self.config.name])
            self.kubeconfig_file.write_text(result.stdout)
            logger.info(f"Kubeconfig exported to: {self.kubeconfig_file}")
        except Exception as e:
            logger.warning(f"Could not export kubeconfig: {e}")

    def delete_cluster(self) -> bool:
        """Delete the kind cluster."""
        logger.info(f"Deleting kind cluster: {self.config.name}")
        try:
            self._run_kind(["delete", "cluster", "--name", self.config.name])
            logger.info(f"Cluster {self.config.name} deleted")
            return True
        except Exception as e:
            logger.error(f"Failed to delete cluster: {e}")
            return False


class KubernetesManager:
    """Manages Kubernetes resources using kubectl via Docker."""

    def __init__(self, config: ClusterConfig):
        self.config = config
        self.kubeconfig_path = Path(config.kubeconfig_dir).resolve()
        self.kubeconfig_file = self.kubeconfig_path / "config"

        # Check if native kubectl is available
        try:
            result = run_command(["kubectl", "version", "--client"], check=False)
            self._use_native = result.returncode == 0
        except FileNotFoundError:
            self._use_native = False

    def run_kubectl(self, args: List[str], check: bool = True,
                    input_data: Optional[str] = None) -> subprocess.CompletedProcess:
        """Run kubectl command - native or via Docker."""
        if self._use_native:
            cmd = ["kubectl", "--kubeconfig", str(self.kubeconfig_file)] + args
            return run_command(cmd, check=check, input_data=input_data, timeout=120)
        else:
            return self._run_kubectl_docker(args, check=check, input_data=input_data)

    def _run_kubectl_docker(self, args: List[str], check: bool = True,
                            input_data: Optional[str] = None) -> subprocess.CompletedProcess:
        """Run kubectl via Docker container."""
        kubeconfig_mount = convert_path_for_docker(self.kubeconfig_path)

        cmd = [
            "docker", "run", "--rm", "-i",
            "-v", f"{kubeconfig_mount}:/root/.kube:ro",
            "--network", "host",
            KUBECTL_IMAGE
        ] + args

        return run_command(cmd, check=check, input_data=input_data, timeout=120)

    def create_namespace(self, namespace: Optional[str] = None) -> bool:
        """Create a Kubernetes namespace."""
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
        try:
            args = ["apply", "-f", "-"]
            if namespace:
                args.extend(["-n", namespace])
            self.run_kubectl(args, input_data=yaml_content)
            return True
        except Exception as e:
            logger.error(f"Failed to apply YAML: {e}")
            return False

    def wait_for_deployment(self, name: str, namespace: str, timeout: int = 300) -> bool:
        """Wait for a deployment to be ready."""
        logger.info(f"Waiting for deployment {name} to be ready...")
        try:
            self.run_kubectl([
                "wait", "--for=condition=available",
                f"deployment/{name}",
                "-n", namespace,
                f"--timeout={timeout}s"
            ])
            logger.info(f"Deployment {name} is ready")
            return True
        except Exception as e:
            logger.error(f"Deployment {name} not ready: {e}")
            return False

    def wait_for_pods(self, label: str, namespace: str, expected: int, timeout: int = 300) -> bool:
        """Wait for pods to be ready."""
        logger.info(f"Waiting for {expected} pods with label {label}...")
        start_time = time.time()

        while time.time() - start_time < timeout:
            result = self.run_kubectl([
                "get", "pods", "-n", namespace,
                "-l", label,
                "-o", "jsonpath={.items[*].status.phase}"
            ], check=False)

            if result.returncode == 0:
                phases = result.stdout.split()
                running = sum(1 for p in phases if p == "Running")
                if running >= expected:
                    logger.info(f"All {expected} pods are running")
                    return True
                logger.info(f"Pods running: {running}/{expected}")

            time.sleep(10)

        logger.error("Timeout waiting for pods")
        return False


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

        # Generate YAML from template - use rs_namespace for MongoDB pods
        yaml_path = self.yaml_manager.render_configmap(
            namespace=rs_namespace,
            base_url=self.cluster_config.ops_manager_url,
            project_name=self.credentials.project_name,
            org_id=self.credentials.org_id
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
        ca_cert_path = Path("./certs/ca.crt")
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
        """Create RBAC for operator to access the replica set namespace."""
        rs_namespace = self.cluster_config.rs_namespace
        operator_namespace = self.cluster_config.operator_namespace
        logger.info(f"Creating RBAC for operator in {rs_namespace} namespace...")

        rbac_yaml = f"""apiVersion: rbac.authorization.k8s.io/v1
kind: Role
metadata:
  name: mongodb-enterprise-operator
  namespace: {rs_namespace}
rules:
  - apiGroups: [""]
    resources: [services]
    verbs: [get, list, create, update, delete, watch]
  - apiGroups: [""]
    resources: [secrets]
    verbs: [get, list, create, update, delete, watch]
  - apiGroups: [""]
    resources: [configmaps]
    verbs: [get, list, create, update, delete, watch]
  - apiGroups: [""]
    resources: [pods]
    verbs: [get, list, create, update, delete, watch]
  - apiGroups: [apps]
    resources: [statefulsets]
    verbs: [get, list, create, update, delete, watch]
  - apiGroups: [mongodb.com]
    resources: ["*"]
    verbs: ["*"]
---
apiVersion: rbac.authorization.k8s.io/v1
kind: RoleBinding
metadata:
  name: mongodb-enterprise-operator
  namespace: {rs_namespace}
roleRef:
  apiGroup: rbac.authorization.k8s.io
  kind: Role
  name: mongodb-enterprise-operator
subjects:
  - kind: ServiceAccount
    name: mongodb-enterprise-operator
    namespace: {operator_namespace}
"""
        return self.k8s.apply_yaml(rbac_yaml)

    def deploy_database_roles(self) -> bool:
        """Deploy roles for MongoDB database pods in rs_namespace."""
        rs_namespace = self.cluster_config.rs_namespace
        logger.info(f"Deploying MongoDB database roles in {rs_namespace}...")

        roles_yaml = f"""apiVersion: v1
kind: ServiceAccount
metadata:
  name: mongodb-enterprise-database-pods
  namespace: {rs_namespace}
---
apiVersion: v1
kind: ServiceAccount
metadata:
  name: mongodb-enterprise-appdb
  namespace: {rs_namespace}
---
apiVersion: rbac.authorization.k8s.io/v1
kind: Role
metadata:
  name: mongodb-enterprise-database-pods
  namespace: {rs_namespace}
rules:
  - apiGroups: [""]
    resources: [secrets]
    verbs: [get]
  - apiGroups: [""]
    resources: [pods]
    verbs: [patch, delete, get]
---
apiVersion: rbac.authorization.k8s.io/v1
kind: RoleBinding
metadata:
  name: mongodb-enterprise-database-pods
  namespace: {rs_namespace}
roleRef:
  apiGroup: rbac.authorization.k8s.io
  kind: Role
  name: mongodb-enterprise-database-pods
subjects:
  - kind: ServiceAccount
    name: mongodb-enterprise-database-pods
    namespace: {rs_namespace}
"""
        return self.k8s.apply_yaml(roles_yaml)

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
            ("Creating Ops Manager CA ConfigMap", self.create_ops_manager_ca_configmap),
            ("Creating Ops Manager ConfigMap", self.create_ops_manager_configmap),
        ]

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

    def deploy_replica_set(self) -> bool:
        """Deploy the MongoDB replica set.

        Uses YAML template from k8s/mongodb-replicaset.yaml
        Deploys to rs_namespace (mongodb-rs by default).
        """
        rs_namespace = self.cluster_config.rs_namespace
        logger.info(f"Deploying MongoDB replica set: {self.rs_config.name} in namespace {rs_namespace}")

        # Generate YAML from template - use rs_namespace for MongoDB pods
        yaml_path = self.yaml_manager.render_replicaset(
            namespace=rs_namespace,
            rs_name=self.rs_config.name,
            members=self.rs_config.members,
            version=self.rs_config.version,
            cpu_request=self.rs_config.cpu_request,
            memory_request=self.rs_config.memory_request,
            storage_size=self.rs_config.storage_size
        )

        logger.info(f"Generated replica set YAML: {yaml_path}")
        yaml_content = yaml_path.read_text(encoding='utf-8')

        if not self.k8s.apply_yaml(yaml_content):
            return False

        logger.info("Replica set CR created, waiting for pods...")
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


def check_docker() -> bool:
    """Check if Docker is running."""
    try:
        result = run_command(["docker", "info"], check=False)
        if result.returncode != 0:
            logger.error("Docker is not running. Please start Docker and try again.")
            return False
        return True
    except FileNotFoundError:
        logger.error("Docker is not installed.")
        return False


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

  # Deploy with 5-node replica set
  python deploy_mongodb_k8s.py --replica-set-members 5

  # Cleanup
  python deploy_mongodb_k8s.py --cleanup

  # Show kubectl instructions only
  python deploy_mongodb_k8s.py --instructions-only

All tools (kind, kubectl) run via Docker - no local installation required!
"""
    )

    # Cluster options
    cluster_group = parser.add_argument_group("Cluster Options")
    cluster_group.add_argument("--cluster-name", default="mongodb-k8s", help="Kind cluster name")
    cluster_group.add_argument("--operator-namespace", default="mongodb",
                               help="Namespace for MongoDB operator (default: mongodb)")
    cluster_group.add_argument("--rs-namespace", default="mongodb-rs",
                               help="Namespace for MongoDB replica set (default: mongodb-rs)")
    cluster_group.add_argument("--worker-nodes", type=int, default=1, help="Number of worker nodes")
    cluster_group.add_argument("--kubeconfig-dir", default="./.kube", help="Directory for kubeconfig")

    # Ops Manager options
    om_group = parser.add_argument_group("Ops Manager Options")
    om_group.add_argument("--api-key-file", default="./ops-manager-api-key.json",
                          help="Path to Ops Manager API key file")
    om_group.add_argument("--ops-manager-url", default="https://host.docker.internal:8443",
                          help="Ops Manager URL (from inside kind cluster)")

    # Replica set options
    rs_group = parser.add_argument_group("Replica Set Options")
    rs_group.add_argument("--replica-set-name", default="mongodb-rs", help="Replica set name")
    rs_group.add_argument("--replica-set-members", type=int, default=3, help="Number of replica set members")
    rs_group.add_argument("--mongodb-version", default=MONGODB_VERSION, help="MongoDB version")
    rs_group.add_argument("--storage-size", default="5Gi", help="Storage size per member")

    # Operation modes
    mode_group = parser.add_argument_group("Operation Modes")
    mode_group.add_argument("--cleanup", action="store_true", help="Delete the kind cluster")
    mode_group.add_argument("--skip-operator", action="store_true", help="Skip operator deployment")
    mode_group.add_argument("--skip-replica-set", action="store_true", help="Skip replica set deployment")
    mode_group.add_argument("--instructions-only", action="store_true", help="Only print kubectl instructions")
    mode_group.add_argument("--cluster-only", action="store_true", help="Only create the kind cluster")
    mode_group.add_argument("--skip-preflight", action="store_true", help="Skip pre-flight validation checks")
    mode_group.add_argument("--wait", action="store_true",
                            help="Wait for MongoDB to reach Running state")
    mode_group.add_argument("--wait-timeout", type=int, default=600,
                            help="Timeout for --wait in seconds (default: 600)")
    mode_group.add_argument("--dry-run", action="store_true",
                            help="Show what would be done without making changes")

    # Logging
    parser.add_argument("-v", "--verbose", action="store_true", help="Verbose output")

    args = parser.parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    # Build configuration
    cluster_config = ClusterConfig(
        name=args.cluster_name,
        operator_namespace=args.operator_namespace,
        rs_namespace=args.rs_namespace,
        worker_nodes=args.worker_nodes,
        ops_manager_url=args.ops_manager_url,
        kubeconfig_dir=args.kubeconfig_dir
    )

    rs_config = ReplicaSetConfig(
        name=args.replica_set_name,
        members=args.replica_set_members,
        version=args.mongodb_version,
        storage_size=args.storage_size
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
            logger.info(f"     - Members: {rs_config.members}")
            logger.info(f"     - Version: {rs_config.version}")
            logger.info(f"     - Storage per member: {rs_config.storage_size}")
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

    # Handle cleanup
    if args.cleanup:
        kind_manager.delete_cluster()
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
        credentials = OpsManagerCredentials.from_file(args.api_key_file)
        logger.info(f"Loaded Ops Manager credentials from: {args.api_key_file}")
    except FileNotFoundError:
        logger.error(f"API key file not found: {args.api_key_file}")
        logger.error("Run deploy_ops_manager.py first to generate credentials")
        sys.exit(1)
    except Exception as e:
        logger.error(f"Failed to load credentials: {e}")
        sys.exit(1)

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
        if not rs_deployer.deploy_replica_set():
            logger.error("Failed to deploy replica set")
            sys.exit(1)

        logger.info("Replica set deployment initiated")

        # Wait for MongoDB to be Running if requested
        if args.wait:
            monitor = MongoDBStatusMonitor(k8s_manager, cluster_config.rs_namespace, rs_config.name)
            if not monitor.wait_for_running(timeout=args.wait_timeout):
                logger.error("MongoDB did not reach Running state within timeout")
                sys.exit(1)
        else:
            logger.info("Note: It may take several minutes for all pods to be ready")
            logger.info("Use --wait to wait for MongoDB to reach Running state")

    # Print summary and instructions
    print(f"""
{'='*70}
DEPLOYMENT COMPLETE
{'='*70}

Cluster Name: {cluster_config.name}
Operator Namespace: {cluster_config.operator_namespace}
ReplicaSet Namespace: {cluster_config.rs_namespace}
Replica Set: {rs_config.name} ({rs_config.members} members)

Ops Manager URL: {credentials.base_url}
Project ID: {credentials.project_id}
Organization ID: {credentials.org_id}

Kubeconfig: {Path(cluster_config.kubeconfig_dir).resolve() / 'config'}

YAML Templates: {yaml_manager.template_dir.resolve()}
Generated YAML: {yaml_manager.generated_dir.resolve()}

{'='*70}
""")

    print_kubectl_instructions(cluster_config)


if __name__ == "__main__":
    main()
