#!/usr/bin/env python3
"""
MongoDB Community Search (mongot) Docker Deployment

This script:
1. Builds a custom mongot Docker image from scratch (multi-stage build)
2. Updates the Ops Manager automation config to point each mongod at its mongot
3. Deploys mongot container(s) alongside the Ops Manager-managed replica set
4. Waits for search to become operational

Prerequisites:
    - Ops Manager must be running (deploy_ops_manager.py)
    - MongoDB Community RS must be running (deploy_mongodb_community.py)
    - ops-manager-api-key.json and data-community/connection-info.json must exist

Usage:
    python deploy_mongot.py                          # Deploy mongot for all RS members
    python deploy_mongot.py --member-count 1         # Deploy only 1 mongot (primary)
    python deploy_mongot.py --cleanup                # Remove mongot containers
    python deploy_mongot.py --build-only             # Only build the Docker image
"""

from __future__ import annotations

import subprocess
import sys
import time
import argparse
import json
import logging
from pathlib import Path
from dataclasses import dataclass
from typing import Optional, List, Dict, Any

from deploy_mongodb_community import OpsManagerAPI

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

DEFAULT_MONGOT_IMAGE_NAME = "mongot-custom"
DEFAULT_MONGOT_VERSION = "0.60.1"
DEFAULT_NETWORK_NAME = "mongodb-community-network"
DEFAULT_MEMBER_COUNT = 3
MONGOT_GRPC_PORT = 27027
MONGOD_PORT = 27017
MONGOT_CONTAINER_PREFIX = "mongot"
AGENT_CONTAINER_PREFIX = "mongo-agent"

DEFAULT_CREDENTIALS_FILE = "./ops-manager-api-key.json"
DEFAULT_CONNECTION_INFO_FILE = "./data-community/connection-info.json"
DEFAULT_MONGOT_USERNAME = "mongotUser"
DEFAULT_MONGOT_PASSWORD = "mongotSearchPwd123"
DEFAULT_PWFILE_PATH = "./data-community/mongot-pwfile"


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
class MongotConfig:
    """Configuration for mongot deployment."""
    member_count: int = DEFAULT_MEMBER_COUNT
    image_name: str = DEFAULT_MONGOT_IMAGE_NAME
    version: str = DEFAULT_MONGOT_VERSION
    network_name: str = DEFAULT_NETWORK_NAME
    mongot_container_prefix: str = MONGOT_CONTAINER_PREFIX
    agent_container_prefix: str = AGENT_CONTAINER_PREFIX
    build_dir: str = "./docker-build-mongot"
    credentials_file: str = DEFAULT_CREDENTIALS_FILE
    connection_info_file: str = DEFAULT_CONNECTION_INFO_FILE

    @property
    def image_tag(self) -> str:
        return f"{self.image_name}:{self.version}"

    def mongot_container_name(self, index: int) -> str:
        return f"{self.mongot_container_prefix}-{index}"

    def agent_container_name(self, index: int) -> str:
        return f"{self.agent_container_prefix}-{index}"


# =============================================================================
# Image Builder
# =============================================================================

class MongotImageBuilder:
    """Builds the custom mongot Docker image."""

    def __init__(self, config: MongotConfig):
        self.config = config
        self.build_dir = Path(config.build_dir).resolve()

    def build_image(self, no_cache: bool = False) -> str:
        """Build the mongot Docker image."""
        logger.info(f"Building mongot Docker image: {self.config.image_tag}")

        dockerfile = self.build_dir / "Dockerfile"
        entrypoint = self.build_dir / "entrypoint.sh"
        if not dockerfile.exists() or not entrypoint.exists():
            raise FileNotFoundError(
                f"Build files not found in {self.build_dir}. "
                f"Expected Dockerfile and entrypoint.sh"
            )

        build_cmd = [
            "docker", "build",
            "-t", self.config.image_tag,
            "-t", f"{self.config.image_name}:latest",
        ]
        if no_cache:
            build_cmd.append("--no-cache")
        build_cmd.append(str(self.build_dir))

        run_command(build_cmd, timeout=600)
        logger.info(f"Successfully built image: {self.config.image_tag}")
        return self.config.image_tag


# =============================================================================
# Mongot Deployer
# =============================================================================

class MongotDeployer:
    """Deploys mongot containers alongside an Ops Manager-managed RS."""

    def __init__(self, config: MongotConfig):
        self.config = config
        self.api: Optional[OpsManagerAPI] = None
        self.project_id: Optional[str] = None
        self.rs_name: Optional[str] = None

    def _run_docker(self, args: List[str], check: bool = True,
                    timeout: Optional[int] = None) -> subprocess.CompletedProcess:
        return run_command(["docker"] + args, check=check, timeout=timeout)

    # -------------------------------------------------------------------------
    # Setup
    # -------------------------------------------------------------------------

    def load_config(self) -> None:
        """Load Ops Manager credentials and RS connection info."""
        # Load Ops Manager credentials
        creds_path = Path(self.config.credentials_file)
        if not creds_path.exists():
            raise FileNotFoundError(
                f"Credentials file not found: {creds_path}\n"
                f"Run deploy_ops_manager.py first."
            )
        with open(creds_path) as f:
            creds = json.load(f)

        self.api = OpsManagerAPI(
            base_url=creds["baseUrl"],
            public_key=creds["publicKey"],
            private_key=creds["privateKey"],
        )

        # Load connection info from RS deployment
        info_path = Path(self.config.connection_info_file)
        if not info_path.exists():
            raise FileNotFoundError(
                f"Connection info not found: {info_path}\n"
                f"Run deploy_mongodb_community.py first."
            )
        with open(info_path) as f:
            info = json.load(f)

        self.project_id = info["project_id"]
        self.rs_name = info["rs_name"]
        logger.info(f"Project: {info['project_name']} (ID: {self.project_id})")
        logger.info(f"Replica Set: {self.rs_name}")

    # -------------------------------------------------------------------------
    # Automation Config Update
    # -------------------------------------------------------------------------

    def update_automation_config(self) -> None:
        """Add mongotHost and search parameters to each mongod process."""
        logger.info("Fetching current automation config...")
        config = self.api.get_automation_config(self.project_id)

        processes = config.get("processes", [])
        if not processes:
            raise RuntimeError("No processes found in automation config")

        updated = False
        for i, proc in enumerate(processes):
            mongot_host = f"{self.config.mongot_container_name(i)}:{MONGOT_GRPC_PORT}"

            args = proc.get("args2_6", {})
            set_param = args.get("setParameter", {})

            # Check if already configured
            if set_param.get("mongotHost") == mongot_host:
                logger.info(f"Process {proc['name']} already has mongotHost={mongot_host}")
                continue

            set_param["mongotHost"] = mongot_host
            set_param["searchIndexManagementHostAndPort"] = mongot_host
            set_param["skipAuthenticationToSearchIndexManagementServer"] = True
            set_param["useGrpcForSearch"] = True

            args["setParameter"] = set_param
            proc["args2_6"] = args
            updated = True
            logger.info(f"Configured {proc['name']} -> mongotHost={mongot_host}")

        if updated:
            logger.info("Pushing updated automation config...")
            self.api.update_automation_config(self.project_id, config)
            logger.info("Automation config updated with mongot parameters")
        else:
            logger.info("Automation config already up to date")

    def wait_for_goal_state(self, timeout: int = 300) -> None:
        """Wait for all agents to reach the goal state after config update."""
        logger.info("Waiting for agents to converge after config update...")
        start = time.time()

        while time.time() - start < timeout:
            try:
                status = self.api.get_automation_status(self.project_id)
                goal_version = status.get("goalVersion", 0)
                processes = status.get("processes", [])

                if not processes:
                    time.sleep(10)
                    continue

                all_at_goal = all(
                    p.get("lastGoalVersionAchieved", 0) == goal_version
                    for p in processes
                )

                if all_at_goal and goal_version > 0:
                    logger.info(f"All agents at goal version {goal_version}")
                    return

                at_goal = sum(
                    1 for p in processes
                    if p.get("lastGoalVersionAchieved", 0) == goal_version
                )
                elapsed = int(time.time() - start)
                logger.info(
                    f"Goal state: {at_goal}/{len(processes)} at v{goal_version} ({elapsed}s)"
                )
            except Exception as e:
                elapsed = int(time.time() - start)
                logger.warning(f"Status check failed: {e} ({elapsed}s)")

            time.sleep(10)

        raise TimeoutError(f"Agents did not reach goal state within {timeout}s")

    # -------------------------------------------------------------------------
    # Container Deployment
    # -------------------------------------------------------------------------

    def verify_prerequisites(self) -> None:
        """Verify that the agent containers and network are running."""
        for i in range(self.config.member_count):
            name = self.config.agent_container_name(i)
            result = self._run_docker(
                ["ps", "--filter", f"name=^{name}$",
                 "--filter", "status=running",
                 "--format", "{{.Names}}"],
                check=False
            )
            if name not in result.stdout:
                raise RuntimeError(
                    f"Agent container '{name}' is not running. "
                    f"Run deploy_mongodb_community.py first."
                )

        result = self._run_docker(
            ["network", "ls", "--format", "{{.Name}}"], check=False
        )
        if self.config.network_name not in result.stdout.split('\n'):
            raise RuntimeError(
                f"Docker network '{self.config.network_name}' not found. "
                f"Run deploy_mongodb_community.py first."
            )

        logger.info("Prerequisites verified: agent containers running, network exists")

    def _remove_container(self, name: str) -> None:
        result = self._run_docker(
            ["ps", "-a", "--filter", f"name=^{name}$", "--format", "{{.Names}}"],
            check=False
        )
        if name in result.stdout:
            logger.info(f"Removing existing container: {name}")
            self._run_docker(["rm", "-f", name])

    def deploy_mongot(self, index: int) -> None:
        """Deploy a single mongot container for a given RS member."""
        mongot_name = self.config.mongot_container_name(index)
        agent_name = self.config.agent_container_name(index)
        mongod_target = f"{agent_name}:{MONGOD_PORT}"

        self._remove_container(mongot_name)

        # Password file mount (host path -> container path)
        pwfile_host = str(Path(DEFAULT_PWFILE_PATH).resolve()).replace('\\', '/')
        pwfile_container = "/etc/mongot/pwfile"

        run_cmd = [
            "run", "-d",
            "--name", mongot_name,
            "--network", self.config.network_name,
            "-v", f"{mongot_name}-data:/data/mongot",
            "-v", f"{pwfile_host}:{pwfile_container}:ro",
            "-e", f"MONGOD_HOST_AND_PORT={mongod_target}",
            "-e", f"MONGOT_USERNAME={DEFAULT_MONGOT_USERNAME}",
            "-e", f"MONGOT_PWFILE={pwfile_container}",
            "-e", "MONGOT_AUTH_SOURCE=admin",
            "-e", "DATA_DIR=/data/mongot",
            f"{self.config.image_name}:latest",
        ]

        self._run_docker(run_cmd)
        logger.info(f"Started {mongot_name} -> {mongod_target}")

    def create_mongot_user(self) -> None:
        """Create the mongotUser on the RS with searchCoordinator role."""
        # Find the primary
        for i in range(self.config.member_count):
            agent_name = self.config.agent_container_name(i)
            mongosh_path = self._find_mongosh(agent_name)
            if not mongosh_path:
                continue

            result = self._run_docker([
                "exec", agent_name,
                "bash", "-c",
                f'{mongosh_path} --port 27017 --quiet --eval "'
                f'const s = rs.status().members.find(m => m.stateStr === \\"PRIMARY\\"); '
                f'print(s ? s.name : \\"none\\")"'
            ], check=False, timeout=10)

            primary_host = result.stdout.strip()
            if primary_host == "none" or not primary_host:
                continue

            # Found the primary — extract the container index
            primary_idx = primary_host.split(":")[0].split("-")[-1]
            primary_container = self.config.agent_container_name(int(primary_idx))
            mongosh_on_primary = self._find_mongosh(primary_container)

            logger.info(f"Creating mongotUser on primary: {primary_container}")
            result = self._run_docker([
                "exec", primary_container,
                "bash", "-c",
                f'{mongosh_on_primary} --port 27017 --quiet --eval "'
                'db = db.getSiblingDB(\\"admin\\"); '
                'try { '
                f'  db.createUser({{ user: \\"{DEFAULT_MONGOT_USERNAME}\\", '
                f'    pwd: \\"{DEFAULT_MONGOT_PASSWORD}\\", '
                '    roles: [{ role: \\"searchCoordinator\\", db: \\"admin\\" }] '
                '  }); '
                '  print(\\"mongotUser created\\"); '
                '} catch(e) { '
                '  if (e.codeName === \\"DuplicateKey\\") print(\\"mongotUser already exists\\"); '
                '  else print(\\"Error: \\" + e.message); '
                '}"'
            ], check=False, timeout=15)
            logger.info(f"User creation result: {result.stdout.strip()}")
            return

        logger.warning("Could not find primary to create mongotUser")

    def ensure_pwfile(self) -> None:
        """Ensure the password file exists."""
        pwfile = Path(DEFAULT_PWFILE_PATH).resolve()
        if not pwfile.exists():
            pwfile.parent.mkdir(parents=True, exist_ok=True)
            with open(pwfile, 'w', newline='\n') as f:
                f.write(DEFAULT_MONGOT_PASSWORD)
            logger.info(f"Created password file: {pwfile}")
        else:
            logger.info(f"Password file exists: {pwfile}")

    def deploy_all(self) -> None:
        """Deploy mongot containers for all RS members."""
        for i in range(self.config.member_count):
            self.deploy_mongot(i)

        logger.info("Waiting for mongot containers to initialize...")
        time.sleep(5)

    def wait_for_mongot(self, timeout: int = 120) -> bool:
        """Wait for all mongot containers to be running."""
        logger.info("Checking mongot container health...")
        start = time.time()

        while time.time() - start < timeout:
            all_running = True
            for i in range(self.config.member_count):
                name = self.config.mongot_container_name(i)
                result = self._run_docker(
                    ["ps", "--filter", f"name=^{name}$",
                     "--filter", "status=running",
                     "--format", "{{.Names}}"],
                    check=False
                )
                if name not in result.stdout:
                    all_running = False
                    # Check for crash
                    result2 = self._run_docker(
                        ["ps", "-a", "--filter", f"name=^{name}$",
                         "--filter", "status=exited",
                         "--format", "{{.Names}}"],
                        check=False
                    )
                    if name in result2.stdout:
                        logs = self._run_docker(
                            ["logs", "--tail", "20", name], check=False
                        )
                        logger.error(
                            f"Container {name} exited:\n{logs.stdout}\n{logs.stderr}"
                        )
                        raise RuntimeError(f"mongot container {name} crashed")
                    break

            if all_running:
                logger.info("All mongot containers are running!")
                return True

            elapsed = int(time.time() - start)
            logger.info(f"Waiting for mongot containers... ({elapsed}s)")
            time.sleep(5)

        raise TimeoutError(f"mongot containers not healthy within {timeout}s")

    # -------------------------------------------------------------------------
    # Search Validation
    # -------------------------------------------------------------------------

    def verify_search_ready(self, timeout: int = 90) -> bool:
        """Verify that search is functional by running listSearchIndexes on primary."""
        # Find all containers with mongosh and deploy the test script
        containers = []  # list of (agent_name, mongosh_path)
        for i in range(self.config.member_count):
            candidate = self.config.agent_container_name(i)
            path = self._find_mongosh(candidate)
            if path:
                containers.append((candidate, path))

        if not containers:
            logger.warning("Could not find mongosh in any agent container")
            return False

        # Write the test script to each container
        for agent_name, _ in containers:
            self._run_docker([
                "exec", agent_name, "bash", "-c",
                'cat > /tmp/verify_search.js << \'JSEOF\'\n'
                'try {\n'
                '  var result = db.getSiblingDB("admin").runCommand({ listSearchIndexes: "system.users" });\n'
                '  if (result.ok === 1) print("SEARCH_OK");\n'
                '  else print("SEARCH_FAIL: " + tojson(result));\n'
                '} catch(e) {\n'
                '  print("SEARCH_ERR: " + e.message);\n'
                '}\n'
                'JSEOF'
            ], check=False, timeout=10)

        start = time.time()
        while time.time() - start < timeout:
            # Try each container (one of them is the primary)
            for agent_name, mongosh_path in containers:
                try:
                    result = self._run_docker([
                        "exec", agent_name,
                        "bash", "-c",
                        f"{mongosh_path} --port 27017 --quiet /tmp/verify_search.js"
                    ], check=False, timeout=15)

                    if "SEARCH_OK" in result.stdout:
                        logger.info(f"Search API is responding on {agent_name}!")
                        return True
                except subprocess.TimeoutExpired:
                    pass

            elapsed = int(time.time() - start)
            logger.info(f"Waiting for search API... ({elapsed}s)")
            time.sleep(10)

        logger.warning("Search API did not respond - mongot may need more time")
        return False

    def _find_mongosh(self, container_name: str) -> Optional[str]:
        """Find the mongosh binary path inside an agent container."""
        result = self._run_docker([
            "exec", container_name,
            "bash", "-c",
            "ls -d /var/lib/mongodb-mms-automation/mongosh-*/bin/mongosh 2>/dev/null | head -1"
        ], check=False, timeout=10)
        path = result.stdout.strip()
        return path if path else None

    # -------------------------------------------------------------------------
    # Cleanup
    # -------------------------------------------------------------------------

    def cleanup(self) -> None:
        """Remove all mongot containers and volumes."""
        logger.info("Cleaning up mongot deployment...")
        for i in range(self.config.member_count):
            name = self.config.mongot_container_name(i)
            self._run_docker(["rm", "-f", name], check=False)
            self._run_docker(["volume", "rm", f"{name}-data"], check=False)
        logger.info("mongot cleanup complete")

    # -------------------------------------------------------------------------
    # Full Deployment
    # -------------------------------------------------------------------------

    def deploy_all_steps(self, no_cache: bool = False,
                         skip_build: bool = False) -> None:
        """Run the complete mongot deployment."""
        # 1. Load config
        self.load_config()

        # 2. Build image
        if not skip_build:
            builder = MongotImageBuilder(self.config)
            builder.build_image(no_cache=no_cache)

        # 3. Verify prerequisites
        self.verify_prerequisites()

        # 4. Update automation config with mongotHost parameters
        self.update_automation_config()

        # 5. Wait for agents to converge (mongod restarts with new params)
        self.wait_for_goal_state()

        # 6. Create mongot user and password file
        self.create_mongot_user()
        self.ensure_pwfile()

        # 7. Deploy mongot containers
        self.deploy_all()
        self.wait_for_mongot()

        # 7. Verify search
        time.sleep(10)  # Give mongot time to connect to mongod
        self.verify_search_ready()

        self.print_summary()

    def print_summary(self) -> None:
        """Print deployment summary."""
        print(f"\n{'='*60}")
        print("MONGOT DEPLOYMENT COMPLETE")
        print(f"{'='*60}")
        print(f"Image:     {self.config.image_tag}")
        print(f"Instances: {self.config.member_count}")
        print()
        for i in range(self.config.member_count):
            mongot = self.config.mongot_container_name(i)
            agent = self.config.agent_container_name(i)
            print(f"  {mongot} -> {agent}:{MONGOD_PORT} (gRPC :{MONGOT_GRPC_PORT})")
        print()
        print("Search is ready! Connect with mongosh:")
        mongosh_hint = self.config.agent_container_name(0)
        print(f"  docker exec -it {mongosh_hint} bash")
        print(f"  # Then find mongosh: ls /var/lib/mongodb-mms-automation/mongosh-*/bin/")
        print()
        print("  // Insert test data")
        print('  use test')
        print('  db.movies.insertMany([')
        print('    { title: "The Matrix", year: 1999, genre: "sci-fi" },')
        print('    { title: "Inception", year: 2010, genre: "sci-fi" },')
        print('    { title: "Interstellar", year: 2014, genre: "sci-fi" }')
        print("  ])")
        print()
        print("  // Create a search index")
        print('  db.movies.createSearchIndex("default", {')
        print('    mappings: { dynamic: true }')
        print("  })")
        print()
        print("  // Run a search query (wait ~10s for index to build)")
        print("  db.movies.aggregate([")
        print('    { $search: { text: { query: "matrix", path: { wildcard: "*" } } } }')
        print("  ])")
        print(f"{'='*60}")


# =============================================================================
# Main
# =============================================================================

def main():
    parser = argparse.ArgumentParser(
        description="Deploy MongoDB Community Search (mongot) in Docker",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Build image and deploy mongot for all RS members
  python deploy_mongot.py

  # Deploy only 1 mongot instance (for primary only)
  python deploy_mongot.py --member-count 1

  # Build image only
  python deploy_mongot.py --build-only

  # Cleanup mongot containers
  python deploy_mongot.py --cleanup

  # Rebuild image without cache
  python deploy_mongot.py --no-cache
"""
    )

    parser.add_argument("--member-count", type=int, default=DEFAULT_MEMBER_COUNT,
                        help=f"Number of mongot instances (default: {DEFAULT_MEMBER_COUNT})")
    parser.add_argument("--image-name", default=DEFAULT_MONGOT_IMAGE_NAME,
                        help=f"Docker image name (default: {DEFAULT_MONGOT_IMAGE_NAME})")
    parser.add_argument("--network-name", default=DEFAULT_NETWORK_NAME,
                        help=f"Docker network (default: {DEFAULT_NETWORK_NAME})")
    parser.add_argument("--build-dir", default="./docker-build-mongot",
                        help="Docker build directory (default: ./docker-build-mongot)")
    parser.add_argument("--credentials-file", default=DEFAULT_CREDENTIALS_FILE,
                        help="Ops Manager credentials file")
    parser.add_argument("--connection-info", default=DEFAULT_CONNECTION_INFO_FILE,
                        help="RS connection info file")
    parser.add_argument("--build-only", action="store_true",
                        help="Only build the Docker image")
    parser.add_argument("--skip-build", action="store_true",
                        help="Skip Docker image build")
    parser.add_argument("--no-cache", action="store_true",
                        help="Build without Docker cache")
    parser.add_argument("--cleanup", action="store_true",
                        help="Remove mongot containers and volumes")
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="Verbose output")

    args = parser.parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    config = MongotConfig(
        member_count=args.member_count,
        image_name=args.image_name,
        network_name=args.network_name,
        build_dir=args.build_dir,
        credentials_file=args.credentials_file,
        connection_info_file=args.connection_info,
    )

    deployer = MongotDeployer(config)

    if args.cleanup:
        deployer.cleanup()
        return

    if args.build_only:
        builder = MongotImageBuilder(config)
        builder.build_image(no_cache=args.no_cache)
        logger.info("Image build complete (--build-only)")
        return

    try:
        deployer.deploy_all_steps(
            no_cache=args.no_cache,
            skip_build=args.skip_build,
        )
    except Exception as e:
        logger.error(f"Deployment failed: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
