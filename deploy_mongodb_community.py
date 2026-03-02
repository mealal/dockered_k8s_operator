#!/usr/bin/env python3
"""
MongoDB Community 8.2 Docker Deployment — 3-Node Replica Set (Ops Manager Managed)

This script:
1. Reads Ops Manager API credentials from ops-manager-api-key.json
2. Creates a project in Ops Manager and obtains an agent API key
3. Builds a custom Docker image containing the MongoDB Automation Agent
4. Deploys 3 agent containers on a shared Docker network
5. Pushes an automation config to Ops Manager to create a 3-node replica set
6. Waits for the agents to converge and the RS to be healthy

The MongoDB Agent handles the full mongod lifecycle: it downloads the
correct MongoDB binary from Ops Manager, starts mongod, initializes the
replica set, and maintains the desired state.

Prerequisites:
    - Ops Manager must be running (deploy_ops_manager.py)
    - ops-manager-api-key.json must exist with valid credentials

Usage:
    python deploy_mongodb_community.py              # Full deployment
    python deploy_mongodb_community.py --cleanup     # Remove everything
    python deploy_mongodb_community.py --dry-run     # Show what would be done
"""

from __future__ import annotations

import subprocess
import sys
import time
import argparse
import json
import logging
import ssl
import urllib.request
import urllib.error
from pathlib import Path
from dataclasses import dataclass
from typing import Optional, List, Dict, Any

from shared.ui_utils import mask_password

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger(__name__)

# =============================================================================
# Constants
# =============================================================================

DEFAULT_MONGODB_VERSION = "8.2.5"
DEFAULT_RS_NAME = "rs-community"
DEFAULT_NETWORK_NAME = "mongodb-community-network"
DEFAULT_BASE_PORT = 27017
DEFAULT_MEMBER_COUNT = 3
DEFAULT_PROJECT_NAME = "CommunitySearchPOC"
DEFAULT_AGENT_IMAGE_NAME = "mongodb-agent"
DEFAULT_CREDENTIALS_FILE = "./ops-manager-api-key.json"
DEFAULT_BUILD_DIR = "./docker-build-agent"
DEFAULT_DATA_DIR = "./data-community"

CONTAINER_PREFIX = "mongo-agent"
MONGOT_GRPC_PORT = 27027


# =============================================================================
# Utility Functions
# =============================================================================

def run_command(cmd: List[str], check: bool = True, capture: bool = True,
                timeout: Optional[int] = None) -> subprocess.CompletedProcess:
    """Run a shell command with proper error handling."""
    logger.debug(f"Running: {' '.join(cmd)}")
    try:
        result = subprocess.run(
            cmd,
            capture_output=capture,
            text=True,
            encoding='utf-8',
            errors='replace',
            timeout=timeout
        )
        if check and result.returncode != 0:
            logger.error(f"Command failed: {result.stderr}")
            raise subprocess.CalledProcessError(result.returncode, cmd, result.stdout, result.stderr)
        return result
    except subprocess.TimeoutExpired:
        logger.error(f"Command timed out after {timeout}s: {' '.join(cmd)}")
        raise


# =============================================================================
# Configuration
# =============================================================================

@dataclass
class CommunityRSConfig:
    """Configuration for Ops Manager-managed Community RS deployment."""
    rs_name: str = DEFAULT_RS_NAME
    member_count: int = DEFAULT_MEMBER_COUNT
    base_port: int = DEFAULT_BASE_PORT
    mongodb_version: str = DEFAULT_MONGODB_VERSION
    network_name: str = DEFAULT_NETWORK_NAME
    container_prefix: str = CONTAINER_PREFIX
    project_name: str = DEFAULT_PROJECT_NAME
    agent_image_name: str = DEFAULT_AGENT_IMAGE_NAME
    credentials_file: str = DEFAULT_CREDENTIALS_FILE
    build_dir: str = DEFAULT_BUILD_DIR
    data_dir: str = DEFAULT_DATA_DIR
    ops_manager_url: str = ""  # Loaded from credentials file

    @property
    def member_ports(self) -> List[int]:
        return [self.base_port + i for i in range(self.member_count)]

    def container_name(self, index: int) -> str:
        return f"{self.container_prefix}-{index}"

    def hostname(self, index: int) -> str:
        """Hostname the agent reports to Ops Manager."""
        return f"{self.container_prefix}-{index}"


# =============================================================================
# Ops Manager API Client
# =============================================================================

class OpsManagerAPI:
    """Client for Ops Manager REST API with digest authentication."""

    def __init__(self, base_url: str, public_key: str, private_key: str):
        self.base_url = base_url.rstrip('/')
        self.public_key = public_key
        self.private_key = private_key

        # SSL context for self-signed certs
        self.ssl_context = ssl.create_default_context()
        self.ssl_context.check_hostname = False
        self.ssl_context.verify_mode = ssl.CERT_NONE

        # Build authenticated opener
        password_mgr = urllib.request.HTTPPasswordMgrWithDefaultRealm()
        password_mgr.add_password(None, self.base_url, public_key, private_key)
        auth_handler = urllib.request.HTTPDigestAuthHandler(password_mgr)
        self._opener = urllib.request.build_opener(
            auth_handler,
            urllib.request.HTTPSHandler(context=self.ssl_context)
        )

    def _request(self, endpoint: str, method: str = "GET",
                 data: Optional[Dict] = None) -> Any:
        """Make an authenticated API request."""
        url = f"{self.base_url}/api/public/v1.0{endpoint}"
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json"
        }
        body = json.dumps(data).encode('utf-8') if data else None
        req = urllib.request.Request(url, data=body, headers=headers, method=method)

        try:
            with self._opener.open(req, timeout=30) as response:
                response_data = response.read().decode('utf-8')
                return json.loads(response_data) if response_data else {}
        except urllib.error.HTTPError as e:
            error_body = e.read().decode('utf-8') if e.fp else ""
            logger.error(f"API {method} {endpoint}: HTTP {e.code} - {error_body}")
            raise

    def create_project(self, name: str, org_id: str) -> Dict[str, Any]:
        """Create a new project. Returns project data including agentApiKey."""
        return self._request("/groups", method="POST", data={
            "name": name,
            "orgId": org_id
        })

    def get_project_by_name(self, name: str) -> Optional[Dict[str, Any]]:
        """Find a project by name. Returns None if not found."""
        try:
            return self._request(f"/groups/byName/{name}")
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            raise

    def create_agent_api_key(self, project_id: str) -> str:
        """Create a new agent API key for the project.

        Always creates a new key because the GET endpoint returns masked keys
        (e.g., '****...871f') which the agent rejects as non-alphanumeric.
        """
        result = self._request(
            f"/groups/{project_id}/agentapikeys",
            method="POST",
            data={"desc": "Docker agent key"}
        )
        return result["key"]

    def get_automation_config(self, project_id: str) -> Dict[str, Any]:
        """Get the current automation config."""
        return self._request(f"/groups/{project_id}/automationConfig")

    def update_automation_config(self, project_id: str,
                                 config: Dict[str, Any]) -> Dict[str, Any]:
        """Update the automation config (PUT)."""
        return self._request(
            f"/groups/{project_id}/automationConfig",
            method="PUT",
            data=config
        )

    def get_automation_status(self, project_id: str) -> Dict[str, Any]:
        """Get the automation status (goal vs current state)."""
        return self._request(f"/groups/{project_id}/automationStatus")


# =============================================================================
# Agent Image Builder
# =============================================================================

class AgentImageBuilder:
    """Builds the MongoDB Agent Docker image."""

    def __init__(self, build_dir: str, image_name: str, ops_manager_url: str):
        self.build_dir = Path(build_dir).resolve()
        self.image_name = image_name
        self.ops_manager_url = ops_manager_url

    def build_image(self, no_cache: bool = False) -> str:
        """Build the agent Docker image."""
        tag = f"{self.image_name}:latest"
        logger.info(f"Building agent Docker image: {tag}")

        # Verify build files exist
        dockerfile = self.build_dir / "Dockerfile"
        entrypoint = self.build_dir / "entrypoint.sh"
        if not dockerfile.exists() or not entrypoint.exists():
            raise FileNotFoundError(
                f"Build files not found in {self.build_dir}. "
                f"Expected Dockerfile and entrypoint.sh"
            )

        # During Docker build, 'localhost' refers to the build container.
        # Replace it with 'host.docker.internal' so the builder can reach Ops Manager.
        docker_url = self.ops_manager_url.replace("://localhost:", "://host.docker.internal:")

        build_cmd = [
            "docker", "build",
            "--build-arg", f"OPS_MANAGER_URL={docker_url}",
            "-t", tag,
        ]
        if no_cache:
            build_cmd.append("--no-cache")
        build_cmd.append(str(self.build_dir))

        run_command(build_cmd, timeout=600)
        logger.info(f"Successfully built image: {tag}")
        return tag


# =============================================================================
# Community RS Deployer
# =============================================================================

class CommunityRSDeployer:
    """Deploys an Ops Manager-managed MongoDB Community RS in Docker."""

    def __init__(self, config: CommunityRSConfig):
        self.config = config
        self.data_dir = Path(config.data_dir).resolve()
        self.api: Optional[OpsManagerAPI] = None
        self.project_id: Optional[str] = None
        self.agent_api_key: Optional[str] = None

    def _run_docker(self, args: List[str], check: bool = True,
                    timeout: Optional[int] = None) -> subprocess.CompletedProcess:
        return run_command(["docker"] + args, check=check, timeout=timeout)

    # -------------------------------------------------------------------------
    # Credentials & Ops Manager Setup
    # -------------------------------------------------------------------------

    def load_credentials(self) -> Dict[str, Any]:
        """Load Ops Manager API credentials from file."""
        creds_path = Path(self.config.credentials_file)
        if not creds_path.exists():
            raise FileNotFoundError(
                f"Credentials file not found: {creds_path}\n"
                f"Run deploy_ops_manager.py first."
            )
        with open(creds_path) as f:
            creds = json.load(f)

        self.config.ops_manager_url = creds["baseUrl"]
        self.api = OpsManagerAPI(
            base_url=creds["baseUrl"],
            public_key=creds["publicKey"],
            private_key=creds["privateKey"],
        )
        logger.info(f"Loaded credentials from: {creds_path}")
        logger.info(f"Ops Manager URL: {creds['baseUrl']}")
        return creds

    def create_or_get_project(self, org_id: str) -> None:
        """Create project in Ops Manager (or reuse existing)."""
        existing = self.api.get_project_by_name(self.config.project_name)
        if existing:
            self.project_id = existing["id"]
            logger.info(f"Using existing project: {self.config.project_name} (ID: {self.project_id})")
        else:
            result = self.api.create_project(self.config.project_name, org_id)
            self.project_id = result["id"]
            self.agent_api_key = result.get("agentApiKey")
            logger.info(f"Created project: {self.config.project_name} (ID: {self.project_id})")

        # Always create a fresh agent API key (GET returns masked keys)
        if not self.agent_api_key:
            self.agent_api_key = self.api.create_agent_api_key(self.project_id)
        logger.info(f"Agent API key obtained")

    # -------------------------------------------------------------------------
    # Network
    # -------------------------------------------------------------------------

    def create_network(self) -> None:
        """Create Docker network, connecting it to the Ops Manager network."""
        result = self._run_docker(
            ["network", "ls", "--format", "{{.Name}}"], check=False
        )
        networks = result.stdout.split('\n')

        if self.config.network_name not in networks:
            self._run_docker(["network", "create", self.config.network_name])
            logger.info(f"Created network: {self.config.network_name}")
        else:
            logger.info(f"Network already exists: {self.config.network_name}")

    # -------------------------------------------------------------------------
    # Agent Container Deployment
    # -------------------------------------------------------------------------

    def _remove_container(self, name: str) -> None:
        result = self._run_docker(
            ["ps", "-a", "--filter", f"name=^{name}$", "--format", "{{.Names}}"],
            check=False
        )
        if name in result.stdout:
            logger.info(f"Removing existing container: {name}")
            self._run_docker(["rm", "-f", name])

    def deploy_agent(self, index: int) -> None:
        """Deploy a single agent container."""
        name = self.config.container_name(index)
        port = self.config.member_ports[index]

        self._remove_container(name)

        # Inside containers, 'localhost' refers to the container itself.
        # Replace with 'host.docker.internal' for agent→Ops Manager communication.
        agent_base_url = self.config.ops_manager_url.replace(
            "://localhost:", "://host.docker.internal:"
        )

        run_cmd = [
            "run", "-d",
            "--name", name,
            "--hostname", name,
            "--network", self.config.network_name,
            "-p", f"{port}:27017",
            "-v", f"{name}-data:/data/db",
            "-e", f"MMS_GROUP_ID={self.project_id}",
            "-e", f"MMS_API_KEY={self.agent_api_key}",
            "-e", f"MMS_BASE_URL={agent_base_url}",
            f"{self.config.agent_image_name}:latest",
        ]

        self._run_docker(run_cmd)
        logger.info(f"Started agent {name} on port {port}")

    def connect_to_ops_manager_network(self) -> None:
        """Connect agent containers to the Ops Manager network for agent→OM communication."""
        om_network = "ops-manager-network"
        result = self._run_docker(
            ["network", "ls", "--format", "{{.Name}}"], check=False
        )
        if om_network not in result.stdout.split('\n'):
            logger.warning(f"Ops Manager network '{om_network}' not found, skipping cross-connect")
            return

        for i in range(self.config.member_count):
            name = self.config.container_name(i)
            # Check if already connected
            result = self._run_docker(
                ["network", "inspect", om_network, "--format",
                 "{{range .Containers}}{{.Name}} {{end}}"],
                check=False
            )
            if name not in result.stdout:
                self._run_docker(
                    ["network", "connect", om_network, name],
                    check=False
                )
                logger.info(f"Connected {name} to {om_network}")

    def deploy_all_agents(self) -> None:
        """Deploy all agent containers."""
        for i in range(self.config.member_count):
            self.deploy_agent(i)

        # Connect to Ops Manager network
        self.connect_to_ops_manager_network()

        logger.info("Waiting for agents to register...")
        time.sleep(10)

    # -------------------------------------------------------------------------
    # Automation Config
    # -------------------------------------------------------------------------

    def build_automation_config(self, base_config: Dict[str, Any]) -> Dict[str, Any]:
        """Build the automation config for a 3-node replica set."""
        config = base_config.copy()

        # Build process list
        processes = []
        for i in range(self.config.member_count):
            hostname = self.config.hostname(i)
            process_name = f"{self.config.rs_name}_{i}"
            # featureCompatibilityVersion must match the major.minor of mongodb_version
            fcv = ".".join(self.config.mongodb_version.split(".")[:2])

            processes.append({
                "name": process_name,
                "processType": "mongod",
                "version": self.config.mongodb_version,
                "featureCompatibilityVersion": fcv,
                "hostname": hostname,
                "authSchemaVersion": 5,
                "logRotate": {
                    "sizeThresholdMB": 1000,
                    "timeThresholdHrs": 24
                },
                "args2_6": {
                    "net": {
                        "port": 27017
                    },
                    "replication": {
                        "replSetName": self.config.rs_name
                    },
                    "storage": {
                        "dbPath": "/data/db"
                    },
                    "systemLog": {
                        "destination": "file",
                        "path": "/data/db/mongodb.log"
                    }
                }
            })

        config["processes"] = processes

        # Build replica set config
        members = []
        for i in range(self.config.member_count):
            process_name = f"{self.config.rs_name}_{i}"
            members.append({
                "_id": i,
                "host": process_name,
                "priority": 1,
                "votes": 1,
                "arbiterOnly": False,
                "slaveDelay": 0,
                "buildIndexes": True
            })

        config["replicaSets"] = [{
            "_id": self.config.rs_name,
            "protocolVersion": 1,
            "members": members
        }]

        return config

    def push_automation_config(self) -> None:
        """Get the current config, add the RS definition, and push it back."""
        logger.info("Fetching current automation config...")
        current_config = self.api.get_automation_config(self.project_id)

        logger.info("Building replica set automation config...")
        new_config = self.build_automation_config(current_config)

        logger.info("Pushing automation config to Ops Manager...")
        self.api.update_automation_config(self.project_id, new_config)
        logger.info("Automation config updated - agents will converge to goal state")

    # -------------------------------------------------------------------------
    # Wait for Convergence
    # -------------------------------------------------------------------------

    def wait_for_agents(self, timeout: int = 120) -> bool:
        """Wait for all agents to register with Ops Manager."""
        logger.info("Waiting for agents to connect to Ops Manager...")
        start = time.time()

        while time.time() - start < timeout:
            all_running = True
            for i in range(self.config.member_count):
                name = self.config.container_name(i)
                result = self._run_docker(
                    ["ps", "--filter", f"name=^{name}$",
                     "--filter", "status=running",
                     "--format", "{{.Names}}"],
                    check=False
                )
                if name not in result.stdout:
                    all_running = False
                    # Check if crashed
                    result2 = self._run_docker(
                        ["ps", "-a", "--filter", f"name=^{name}$",
                         "--filter", "status=exited",
                         "--format", "{{.Names}}"],
                        check=False
                    )
                    if name in result2.stdout:
                        logs = self._run_docker(["logs", "--tail", "15", name], check=False)
                        logger.error(f"Container {name} exited:\n{logs.stdout}\n{logs.stderr}")
                        raise RuntimeError(f"Agent container {name} crashed")
                    break

            if all_running:
                logger.info("All agent containers are running")
                return True

            elapsed = int(time.time() - start)
            logger.info(f"Waiting for agents... ({elapsed}s)")
            time.sleep(5)

        raise TimeoutError(f"Agents not running within {timeout}s")

    def wait_for_goal_state(self, timeout: int = 600) -> bool:
        """Wait for all agents to reach the goal state (RS deployed)."""
        logger.info("Waiting for automation to reach goal state...")
        start = time.time()

        while time.time() - start < timeout:
            try:
                status = self.api.get_automation_status(self.project_id)
                goal_version = status.get("goalVersion", 0)
                processes = status.get("processes", [])

                if not processes:
                    elapsed = int(time.time() - start)
                    logger.info(f"No processes reported yet... ({elapsed}s)")
                    time.sleep(15)
                    continue

                all_at_goal = all(
                    p.get("lastGoalVersionAchieved", 0) == goal_version
                    for p in processes
                )

                if all_at_goal and goal_version > 0:
                    logger.info(f"All agents at goal version {goal_version}!")
                    return True

                at_goal = sum(
                    1 for p in processes
                    if p.get("lastGoalVersionAchieved", 0) == goal_version
                )
                elapsed = int(time.time() - start)
                logger.info(
                    f"Goal state progress: {at_goal}/{len(processes)} "
                    f"at version {goal_version} ({elapsed}s)"
                )
            except Exception as e:
                elapsed = int(time.time() - start)
                logger.warning(f"Status check failed: {e} ({elapsed}s)")

            time.sleep(15)

        raise TimeoutError(f"Automation did not reach goal state within {timeout}s")

    def _find_mongosh(self, container_name: str) -> Optional[str]:
        """Find the mongosh binary path inside an agent container."""
        result = self._run_docker([
            "exec", container_name,
            "bash", "-c",
            "ls -d /var/lib/mongodb-mms-automation/mongosh-*/bin/mongosh 2>/dev/null | head -1"
        ], check=False, timeout=10)
        path = result.stdout.strip()
        return path if path else None

    def verify_rs_health(self) -> bool:
        """Verify the replica set is healthy by checking rs.status()."""
        # Try each container to find one with mongosh available
        for i in range(self.config.member_count):
            container = self.config.container_name(i)
            mongosh = self._find_mongosh(container)
            if not mongosh:
                continue

            try:
                result = self._run_docker([
                    "exec", container,
                    "bash", "-c",
                    f'{mongosh} --port 27017 --quiet --eval '
                    '"rs.status().members.filter(m => m.stateStr === \'PRIMARY\').length"'
                ], check=False, timeout=15)

                if result.returncode == 0 and result.stdout.strip() == "1":
                    logger.info("Replica set has an elected primary!")
                    return True
            except subprocess.TimeoutExpired:
                continue

        logger.warning("Could not verify RS health via mongosh - agent may still be configuring")
        return False

    # -------------------------------------------------------------------------
    # Cleanup
    # -------------------------------------------------------------------------

    def cleanup(self) -> None:
        """Remove all containers, volumes, network, and data."""
        logger.info("Cleaning up MongoDB Community deployment...")

        for i in range(self.config.member_count):
            name = self.config.container_name(i)
            self._run_docker(["rm", "-f", name], check=False)
            self._run_docker(["volume", "rm", f"{name}-data"], check=False)

        # Remove mongot containers and volumes
        for i in range(self.config.member_count):
            mongot_name = f"mongot-{i}"
            self._run_docker(["rm", "-f", mongot_name], check=False)
            self._run_docker(["volume", "rm", f"{mongot_name}-data"], check=False)

        self._run_docker(["network", "rm", self.config.network_name], check=False)

        if self.data_dir.exists():
            import shutil
            shutil.rmtree(self.data_dir)
            logger.info(f"Removed data directory: {self.data_dir}")

        logger.info("Cleanup complete")

    # -------------------------------------------------------------------------
    # Full Deployment
    # -------------------------------------------------------------------------

    def deploy_all(self, no_cache: bool = False) -> Dict[str, Any]:
        """Run the complete deployment. Returns deployment info."""
        self.data_dir.mkdir(parents=True, exist_ok=True)

        # 1. Load credentials
        creds = self.load_credentials()

        # 2. Create project
        self.create_or_get_project(creds["orgId"])

        # 3. Build agent image
        builder = AgentImageBuilder(
            build_dir=self.config.build_dir,
            image_name=self.config.agent_image_name,
            ops_manager_url=self.config.ops_manager_url,
        )
        builder.build_image(no_cache=no_cache)

        # 4. Create network and deploy agents
        self.create_network()
        self.deploy_all_agents()

        # 5. Wait for agents to register
        self.wait_for_agents()

        # 6. Push automation config
        self.push_automation_config()

        # 7. Wait for goal state
        self.wait_for_goal_state()

        # 8. Verify RS health
        self.verify_rs_health()

        # Build info
        info = {
            "rs_name": self.config.rs_name,
            "members": self.config.member_count,
            "ports": self.config.member_ports,
            "mongodb_version": self.config.mongodb_version,
            "project_id": self.project_id,
            "project_name": self.config.project_name,
            "network": self.config.network_name,
            "ops_manager_url": self.config.ops_manager_url,
        }

        info_file = self.data_dir / "connection-info.json"
        with open(info_file, 'w') as f:
            json.dump(info, f, indent=2)
        logger.info(f"Connection info saved to: {info_file}")

        return info

    def print_summary(self, info: Dict[str, Any]) -> None:
        """Print deployment summary."""
        print(f"\n{'='*60}")
        print("MONGODB COMMUNITY REPLICA SET DEPLOYED")
        print(f"{'='*60}")
        print(f"Managed by:     Ops Manager ({info['ops_manager_url']})")
        print(f"Project:        {info['project_name']} (ID: {info['project_id']})")
        print(f"MongoDB:        {info['mongodb_version']}")
        print(f"Replica Set:    {info['rs_name']}")
        print(f"Members:        {info['members']}")
        print(f"Ports:          {', '.join(str(p) for p in info['ports'])}")
        print(f"Network:        {info['network']}")
        print()
        print("Containers:")
        for i in range(info['members']):
            name = self.config.container_name(i)
            port = info['ports'][i]
            print(f"  {name}  ->  localhost:{port}")
        print()
        print("Next step: Run deploy_mongot.py to add search capabilities")
        print(f"{'='*60}")


# =============================================================================
# Main
# =============================================================================

def main():
    parser = argparse.ArgumentParser(
        description="Deploy Ops Manager-managed MongoDB Community RS in Docker",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Full deployment (3-node RS managed by Ops Manager)
  python deploy_mongodb_community.py

  # Custom RS name and MongoDB version
  python deploy_mongodb_community.py --rs-name myrs --mongodb-version 8.0.0

  # Cleanup everything
  python deploy_mongodb_community.py --cleanup

  # Dry run
  python deploy_mongodb_community.py --dry-run
"""
    )

    parser.add_argument("--rs-name", default=DEFAULT_RS_NAME,
                        help=f"Replica set name (default: {DEFAULT_RS_NAME})")
    parser.add_argument("--member-count", type=int, default=DEFAULT_MEMBER_COUNT,
                        help=f"Number of RS members (default: {DEFAULT_MEMBER_COUNT})")
    parser.add_argument("--base-port", type=int, default=DEFAULT_BASE_PORT,
                        help=f"Base host port (default: {DEFAULT_BASE_PORT})")
    parser.add_argument("--mongodb-version", default=DEFAULT_MONGODB_VERSION,
                        help=f"MongoDB version (default: {DEFAULT_MONGODB_VERSION})")
    parser.add_argument("--project-name", default=DEFAULT_PROJECT_NAME,
                        help=f"Ops Manager project name (default: {DEFAULT_PROJECT_NAME})")
    parser.add_argument("--network-name", default=DEFAULT_NETWORK_NAME,
                        help=f"Docker network name (default: {DEFAULT_NETWORK_NAME})")
    parser.add_argument("--credentials-file", default=DEFAULT_CREDENTIALS_FILE,
                        help=f"Ops Manager credentials file (default: {DEFAULT_CREDENTIALS_FILE})")
    parser.add_argument("--build-dir", default=DEFAULT_BUILD_DIR,
                        help=f"Docker build directory (default: {DEFAULT_BUILD_DIR})")
    parser.add_argument("--data-dir", default=DEFAULT_DATA_DIR,
                        help=f"Data directory (default: {DEFAULT_DATA_DIR})")
    parser.add_argument("--no-cache", action="store_true",
                        help="Build without Docker cache")
    parser.add_argument("--cleanup", action="store_true",
                        help="Remove all containers, volumes, network, and data")
    parser.add_argument("--dry-run", action="store_true",
                        help="Show what would be done without making changes")
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="Verbose output")

    args = parser.parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    config = CommunityRSConfig(
        rs_name=args.rs_name,
        member_count=args.member_count,
        base_port=args.base_port,
        mongodb_version=args.mongodb_version,
        network_name=args.network_name,
        container_prefix=CONTAINER_PREFIX,
        project_name=args.project_name,
        credentials_file=args.credentials_file,
        build_dir=args.build_dir,
        data_dir=args.data_dir,
    )

    deployer = CommunityRSDeployer(config)

    if args.cleanup:
        deployer.cleanup()
        return

    if args.dry_run:
        logger.info("=== DRY-RUN MODE ===")
        logger.info("The following actions would be performed:")
        logger.info(f"  1. Load credentials from: {config.credentials_file}")
        logger.info(f"  2. Create project '{config.project_name}' in Ops Manager")
        logger.info(f"  3. Build agent Docker image: {config.agent_image_name}")
        logger.info(f"  4. Create Docker network: {config.network_name}")
        for i in range(config.member_count):
            name = config.container_name(i)
            port = config.member_ports[i]
            logger.info(f"  5.{i+1}. Deploy agent {name} on port {port}")
        logger.info(f"  6. Push automation config: {config.rs_name} ({config.mongodb_version})")
        logger.info(f"  7. Wait for agents to converge to goal state")
        logger.info("=== END DRY-RUN ===")
        return

    try:
        info = deployer.deploy_all(no_cache=args.no_cache)
        deployer.print_summary(info)
    except Exception as e:
        logger.error(f"Deployment failed: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
