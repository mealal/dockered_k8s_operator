#!/usr/bin/env python3
"""
MongoDB Ops Manager Docker Deployment with HTTPS

This script:
1. Generates a custom CA and server certificates using OpenSSL
2. Builds a custom Docker image with MongoDB Ops Manager
3. Deploys MongoDB Ops Manager in a Docker container with HTTPS configured
4. Creates initial admin user, organization, and project

The script downloads the Ops Manager RPM from MongoDB's official repository
and builds a custom Docker image with HTTPS support.
"""

from __future__ import annotations

import subprocess
import sys
import time
import argparse
import json
import urllib.request
import urllib.error
import ssl
import logging
import shutil
import secrets
import string
import hashlib
from pathlib import Path
from dataclasses import dataclass, field, asdict
from typing import Optional, Tuple, Dict, Any, List
from functools import wraps

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger(__name__)

# MongoDB Ops Manager version and download URL
OPS_MANAGER_VERSION = "7.0.20"
OPS_MANAGER_RPM_URL = "https://downloads.mongodb.com/on-prem-mms/rpm/mongodb-mms-7.0.20.500.20251204T1317Z.x86_64.rpm"


# =============================================================================
# Configuration Dataclasses
# =============================================================================

@dataclass
class CertificateConfig:
    """Configuration for certificate generation."""
    output_dir: str = "./certs"
    hostname: str = "localhost"
    ca_validity_days: int = 3650  # 10 years
    server_validity_days: int = 365  # 1 year
    key_size: int = 4096
    country: str = "US"
    state: str = "State"
    city: str = "City"
    organization: str = "Organization"
    organizational_unit: str = "IT"
    ca_common_name: str = "Custom-CA"


@dataclass
class AppDbConfig:
    """Configuration for the Application Database."""
    username: str = "admin"
    password: str = "password"
    image: str = "mongo:6.0"
    container_name: str = "ops-manager-appdb"
    data_volume: Optional[str] = None  # For persistence


@dataclass
class OpsManagerConfig:
    """Configuration for MongoDB Ops Manager deployment."""
    hostname: str = "localhost"
    https_port: int = 8443
    container_name: str = "ops-manager"
    admin_username: str = "admin"
    admin_password: Optional[str] = None  # Auto-generated if not provided
    org_name: str = "Default"
    project_name: str = "Default"
    email_domain: str = "localhost.local"
    smtp_hostname: str = "localhost"
    smtp_port: int = 25
    memory_limit: Optional[str] = None  # e.g., "4g"
    cpu_limit: Optional[float] = None  # e.g., 2.0
    config_timeout: int = 300
    network_name: str = "ops-manager-network"
    image_name: str = "ops-manager-https"
    version: str = OPS_MANAGER_VERSION
    gen_key_file: str = "./data/gen.key"  # Persisted gen.key location
    data_dir: str = "./data"  # Directory for persistent data

    def ensure_password(self) -> str:
        """Generate secure password if not provided. Returns the password."""
        if self.admin_password is None:
            self.admin_password = generate_secure_password()
            logger.info("Generated secure admin password")
        return self.admin_password


@dataclass
class DeploymentResult:
    """Result of a deployment operation."""
    success: bool
    message: str
    api_public_key: Optional[str] = None
    api_private_key: Optional[str] = None
    org_id: Optional[str] = None
    project_id: Optional[str] = None
    base_url: Optional[str] = None


# =============================================================================
# Utility Functions
# =============================================================================

def mask_sensitive(value: str, visible_chars: int = 4) -> str:
    """Mask sensitive data, showing only last few characters."""
    if not value or len(value) <= visible_chars:
        return "****"
    return "*" * (len(value) - visible_chars) + value[-visible_chars:]


def generate_secure_password(length: int = 16) -> str:
    """Generate a secure random password meeting Ops Manager requirements.

    Password requirements:
    - At least one uppercase letter
    - At least one lowercase letter
    - At least one digit
    - At least one special character
    - Minimum 8 characters (we use 16 by default)
    """
    # Define character sets
    uppercase = string.ascii_uppercase
    lowercase = string.ascii_lowercase
    digits = string.digits
    special = "!@#$%^&*"

    # Ensure at least one of each required type
    password = [
        secrets.choice(uppercase),
        secrets.choice(lowercase),
        secrets.choice(digits),
        secrets.choice(special),
    ]

    # Fill the rest with random characters from all sets
    all_chars = uppercase + lowercase + digits + special
    password.extend(secrets.choice(all_chars) for _ in range(length - 4))

    # Shuffle to avoid predictable pattern
    password_list = list(password)
    secrets.SystemRandom().shuffle(password_list)

    return ''.join(password_list)


def generate_gen_key() -> str:
    """Generate a stable gen.key value based on a random seed.

    The gen.key is used by Ops Manager for encryption and must remain
    consistent across restarts.
    """
    return secrets.token_hex(24)  # 48 character hex string


def load_or_create_gen_key(key_file: Path) -> str:
    """Load existing gen.key or create a new one.

    This ensures the gen.key persists across container restarts.
    """
    if key_file.exists():
        gen_key = key_file.read_text().strip()
        if gen_key:
            logger.info(f"Loaded existing gen.key from: {key_file}")
            return gen_key

    # Generate new key and save it
    gen_key = generate_gen_key()
    key_file.parent.mkdir(parents=True, exist_ok=True)
    key_file.write_text(gen_key)
    logger.info(f"Generated new gen.key and saved to: {key_file}")
    return gen_key


def retry_with_backoff(max_retries: int = 3, base_delay: float = 1.0, max_delay: float = 30.0):
    """Decorator for retrying operations with exponential backoff."""
    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            last_exception = None
            delay = base_delay
            for attempt in range(max_retries):
                try:
                    return func(*args, **kwargs)
                except (urllib.error.URLError, ConnectionError, TimeoutError) as e:
                    last_exception = e
                    if attempt < max_retries - 1:
                        logger.warning(f"Attempt {attempt + 1} failed: {e}. Retrying in {delay:.1f}s...")
                        time.sleep(delay)
                        delay = min(delay * 2, max_delay)
            raise last_exception
        return wrapper
    return decorator


def run_command(cmd: List[str], check: bool = True, capture: bool = True,
                cwd: Optional[str] = None, timeout: Optional[int] = None) -> subprocess.CompletedProcess:
    """Run a shell command with proper error handling."""
    logger.debug(f"Running: {' '.join(cmd)}")
    try:
        result = subprocess.run(
            cmd,
            capture_output=capture,
            text=True,
            encoding='utf-8',
            errors='replace',
            cwd=cwd,
            timeout=timeout
        )
        if check and result.returncode != 0:
            logger.error(f"Command failed: {result.stderr}")
            raise subprocess.CalledProcessError(result.returncode, cmd, result.stdout, result.stderr)
        return result
    except subprocess.TimeoutExpired as e:
        logger.error(f"Command timed out after {timeout}s: {' '.join(cmd)}")
        raise


# =============================================================================
# Certificate Generator
# =============================================================================

class CertificateGenerator:
    """Generates CA and server certificates using OpenSSL."""

    def __init__(self, config: CertificateConfig):
        self.config = config
        self.output_dir = Path(config.output_dir)
        self.ca_key = self.output_dir / "ca.key"
        self.ca_cert = self.output_dir / "ca.crt"
        self.server_key = self.output_dir / "server.key"
        self.server_csr = self.output_dir / "server.csr"
        self.server_cert = self.output_dir / "server.crt"
        self.server_pem = self.output_dir / "server.pem"

    def _run_openssl(self, args: List[str]) -> subprocess.CompletedProcess:
        """Run an OpenSSL command."""
        return run_command(["openssl"] + args)

    def setup_output_directory(self) -> None:
        """Create the output directory if it doesn't exist."""
        self.output_dir.mkdir(parents=True, exist_ok=True)
        logger.info(f"Certificate output directory: {self.output_dir}")

    def generate_ca(self) -> None:
        """Generate a self-signed CA certificate with proper extensions."""
        logger.info("Generating CA Certificate...")

        # Generate CA private key
        self._run_openssl([
            "genrsa",
            "-out", str(self.ca_key),
            str(self.config.key_size)
        ])
        logger.info(f"Generated CA private key: {self.ca_key}")

        # Create CA extensions config file
        ca_ext_file = self.output_dir / "ca_ext.cnf"
        ca_ext_content = """[req]
distinguished_name = req_distinguished_name
x509_extensions = v3_ca
prompt = no

[req_distinguished_name]
CN = CA

[v3_ca]
basicConstraints = critical, CA:TRUE
keyUsage = critical, keyCertSign, cRLSign, digitalSignature
subjectKeyIdentifier = hash
"""
        ca_ext_file.write_text(ca_ext_content)

        # Generate CA certificate with extensions
        subject = (
            f"/C={self.config.country}/ST={self.config.state}/L={self.config.city}"
            f"/O={self.config.organization}/OU={self.config.organizational_unit}"
            f"/CN={self.config.ca_common_name}"
        )
        self._run_openssl([
            "req", "-new", "-x509",
            "-days", str(self.config.ca_validity_days),
            "-key", str(self.ca_key),
            "-out", str(self.ca_cert),
            "-subj", subject,
            "-config", str(ca_ext_file),
            "-extensions", "v3_ca"
        ])
        logger.info(f"Generated CA certificate: {self.ca_cert}")

    def generate_server_certificate(self) -> None:
        """Generate a server certificate signed by the CA."""
        logger.info("Generating Server Certificate...")

        # Generate server private key
        self._run_openssl([
            "genrsa",
            "-out", str(self.server_key),
            str(self.config.key_size)
        ])
        logger.info(f"Generated server private key: {self.server_key}")

        # Create OpenSSL config for SAN
        san_config = self.output_dir / "san.cnf"
        san_config_content = f"""[req]
default_bits = {self.config.key_size}
prompt = no
default_md = sha256
distinguished_name = dn
req_extensions = req_ext

[dn]
C = {self.config.country}
ST = {self.config.state}
L = {self.config.city}
O = {self.config.organization}
OU = {self.config.organizational_unit}
CN = {self.config.hostname}

[req_ext]
subjectAltName = @alt_names

[alt_names]
DNS.1 = {self.config.hostname}
DNS.2 = localhost
DNS.3 = ops-manager
DNS.4 = host.docker.internal
"""
        san_config.write_text(san_config_content)

        # Generate CSR
        self._run_openssl([
            "req", "-new",
            "-key", str(self.server_key),
            "-out", str(self.server_csr),
            "-config", str(san_config)
        ])
        logger.info(f"Generated server CSR: {self.server_csr}")

        # Create extension file
        ext_file = self.output_dir / "v3_ext.cnf"
        ext_content = f"""authorityKeyIdentifier=keyid,issuer
basicConstraints=CA:FALSE
keyUsage = digitalSignature, nonRepudiation, keyEncipherment, dataEncipherment
subjectAltName = @alt_names

[alt_names]
DNS.1 = {self.config.hostname}
DNS.2 = localhost
DNS.3 = ops-manager
DNS.4 = host.docker.internal
"""
        ext_file.write_text(ext_content)

        # Sign server certificate
        self._run_openssl([
            "x509", "-req",
            "-in", str(self.server_csr),
            "-CA", str(self.ca_cert),
            "-CAkey", str(self.ca_key),
            "-CAcreateserial",
            "-out", str(self.server_cert),
            "-days", str(self.config.server_validity_days),
            "-sha256",
            "-extfile", str(ext_file)
        ])
        logger.info(f"Generated server certificate: {self.server_cert}")

        # Create combined PEM file
        with open(self.server_pem, 'w') as pem_file:
            pem_file.write(self.server_key.read_text())
            pem_file.write(self.server_cert.read_text())
        logger.info(f"Generated combined PEM file: {self.server_pem}")

    def generate_all(self) -> None:
        """Generate all certificates."""
        self.setup_output_directory()
        self.generate_ca()
        self.generate_server_certificate()
        logger.info("Certificate generation complete")


# =============================================================================
# Docker Image Builder
# =============================================================================

class OpsManagerImageBuilder:
    """Builds a custom Docker image for MongoDB Ops Manager."""

    def __init__(self, build_dir: str, config: OpsManagerConfig,
                 rpm_path: Optional[str] = None, rpm_url: str = OPS_MANAGER_RPM_URL):
        self.build_dir = Path(build_dir)
        self.config = config
        self.image_tag = f"{config.image_name}:{config.version}"
        self.rpm_path = Path(rpm_path) if rpm_path else None
        self.rpm_url = rpm_url
        self._cached_rpm = self.build_dir / "mongodb-mms.rpm"

    def create_dockerfile(self) -> Path:
        """Create Dockerfile for Ops Manager with HTTPS support."""
        dockerfile_path = self.build_dir / "Dockerfile"

        if self.rpm_path and self.rpm_path.exists():
            rpm_install = """# Install MongoDB Ops Manager from local RPM
COPY mongodb-mms.rpm /tmp/mongodb-mms.rpm
RUN rpm -ivh /tmp/mongodb-mms.rpm && rm -f /tmp/mongodb-mms.rpm"""
        else:
            rpm_install = f"""# Download and install MongoDB Ops Manager
RUN curl -fSL "{self.rpm_url}" -o /tmp/mongodb-mms.rpm \\
    && rpm -ivh /tmp/mongodb-mms.rpm \\
    && rm -f /tmp/mongodb-mms.rpm"""

        dockerfile_content = f"""# MongoDB Ops Manager with HTTPS Support
FROM rockylinux:8

LABEL maintainer="ops-manager-deployer"
LABEL version="{self.config.version}"

# Install dependencies
RUN dnf install -y --allowerasing \\
    curl procps net-tools cyrus-sasl cyrus-sasl-gssapi \\
    cyrus-sasl-plain krb5-libs libcurl openldap openssl \\
    ncurses java-11-openjdk-headless \\
    && dnf clean all

{rpm_install}

# Create directories and set permissions
RUN mkdir -p /etc/mongodb-mms/certs /data/appdb /data/backup /opt/mongodb/mms/mongodb-releases \\
    && chown -R mongodb-mms:mongodb-mms /etc/mongodb-mms \\
    && chown -R mongodb-mms:mongodb-mms /data \\
    && chown -R mongodb-mms:mongodb-mms /opt/mongodb/mms/conf \\
    && chown -R mongodb-mms:mongodb-mms /opt/mongodb/mms/mongodb-releases

# Copy entrypoint script
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

# Expose ports
EXPOSE 8080 8443

# Environment variables
ENV MMS_HTTPS_PORT=8443
ENV MMS_HTTP_PORT=8080

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=120s --retries=5 \\
    CMD curl -k -f https://localhost:8443/user/login || exit 1

# Volumes for persistence
VOLUME ["/data/appdb", "/data/backup", "/opt/mongodb/mms/logs"]

# Run as mongodb-mms user
USER mongodb-mms

ENTRYPOINT ["/entrypoint.sh"]
"""
        dockerfile_path.write_text(dockerfile_content)
        logger.info(f"Created Dockerfile: {dockerfile_path}")
        return dockerfile_path

    def create_entrypoint(self) -> Path:
        """Create entrypoint script for the container."""
        entrypoint_path = self.build_dir / "entrypoint.sh"

        entrypoint_content = '''#!/bin/bash
set -e

CONFIG_FILE="/opt/mongodb/mms/conf/conf-mms.properties"
CERT_DIR="/etc/mongodb-mms/certs"

# Function to update or add a property in the config file
update_config() {
    local key="$1"
    local value="$2"
    if grep -q "^${key}=" "$CONFIG_FILE" 2>/dev/null; then
        sed -i "s|^${key}=.*|${key}=${value}|" "$CONFIG_FILE"
    else
        echo "${key}=${value}" >> "$CONFIG_FILE"
    fi
}

echo "Configuring MongoDB Ops Manager..."

# Skip initial UI setup wizard
update_config "mms.ignoreInitialUiSetup" "true"

# Email configuration (required)
FROM_EMAIL="${MMS_FROM_EMAIL:-ops-manager@${MMS_EMAIL_DOMAIN:-localhost.local}}"
REPLY_EMAIL="${MMS_REPLY_EMAIL:-ops-manager@${MMS_EMAIL_DOMAIN:-localhost.local}}"
ADMIN_EMAIL="${MMS_ADMIN_EMAIL:-admin@${MMS_EMAIL_DOMAIN:-localhost.local}}"

update_config "mms.fromEmailAddr" "$FROM_EMAIL"
update_config "mms.replyToEmailAddr" "$REPLY_EMAIL"
update_config "mms.adminEmailAddr" "$ADMIN_EMAIL"

# SMTP configuration
update_config "mms.mail.transport" "smtp"
update_config "mms.mail.hostname" "${MMS_SMTP_HOSTNAME:-localhost}"
update_config "mms.mail.port" "${MMS_SMTP_PORT:-25}"

# User registration settings
update_config "mms.user.bypassInviteForExistingUsers" "true"
update_config "mms.userSvcClass" "com.xgen.svc.mms.svc.user.UserSvcDb"
update_config "mms.user.invitationOnly" "false"

# Disable API access list requirement for Kubernetes operator connectivity
# This allows API calls from any IP address without whitelist restrictions
update_config "mms.publicApi.whitelistEnabled" "false"

# Configure Hybrid Mode for MongoDB binary downloads
# In Hybrid Mode, Ops Manager downloads binaries from the internet and serves them to agents
# This allows agents without internet access to get binaries from Ops Manager
update_config "automation.versions.source" "hybrid"

# Configure the directory where Ops Manager stores MongoDB binaries
update_config "automation.versions.directory" "/opt/mongodb/mms/mongodb-releases/"

echo "Email configuration completed"
echo "Hybrid Mode enabled: Agents will download MongoDB binaries from Ops Manager"

# HTTPS configuration
if [ -f "${CERT_DIR}/server.pem" ] && [ -f "${CERT_DIR}/ca.crt" ]; then
    echo "Configuring HTTPS..."
    update_config "mms.https.PEMKeyFile" "${CERT_DIR}/server.pem"
    update_config "mms.https.CAFile" "${CERT_DIR}/ca.crt"
    update_config "mms.https.ClientCertificateMode" "None"
    if [ -n "$MMS_HTTPS_PORT" ]; then
        update_config "BASE_PORT" "${MMS_HTTPS_PORT}"
    fi
fi

# Database configuration
if [ -n "$MONGO_URI" ]; then
    update_config "mongo.mongoUri" "$MONGO_URI"
    echo "MongoDB URI configured"
fi

# Central URL
if [ -n "$MMS_CENTRAL_URL" ]; then
    update_config "mms.centralUrl" "$MMS_CENTRAL_URL"
fi

# Encryption key
if [ -n "$MMS_ENCRYPTION_KEY" ]; then
    update_config "mongodb.encryption.key" "$MMS_ENCRYPTION_KEY"
else
    RANDOM_KEY=$(openssl rand -base64 24)
    update_config "mongodb.encryption.key" "$RANDOM_KEY"
fi

# Gen key
if [ -n "$MMS_GEN_KEY" ]; then
    update_config "mms.genKey" "$MMS_GEN_KEY"
fi

echo ""
echo "=========================================="
echo "Ops Manager Configuration Summary:"
echo "=========================================="
echo "Skip UI Setup Wizard: true"
echo "From Email: $FROM_EMAIL"
echo "SMTP Host: ${MMS_SMTP_HOSTNAME:-localhost}"
echo "Central URL: ${MMS_CENTRAL_URL:-not set}"
echo "=========================================="
echo ""

echo "Starting MongoDB Ops Manager..."
/opt/mongodb/mms/bin/mongodb-mms start

echo "Ops Manager started. Tailing logs..."
tail -f /opt/mongodb/mms/logs/mms0.log
'''
        # Write with Unix line endings
        with open(entrypoint_path, 'w', newline='\n') as f:
            f.write(entrypoint_content)
        logger.info(f"Created entrypoint script: {entrypoint_path}")
        return entrypoint_path

    def build_image(self, no_cache: bool = False) -> str:
        """Build the Docker image."""
        logger.info(f"Building Docker image: {self.image_tag}")

        self.build_dir.mkdir(parents=True, exist_ok=True)

        # Copy RPM if provided
        if self.rpm_path and self.rpm_path.exists():
            shutil.copy2(self.rpm_path, self._cached_rpm)
            logger.info(f"Using local RPM: {self.rpm_path}")
        else:
            logger.info(f"Will download RPM from: {self.rpm_url}")

        self.create_dockerfile()
        self.create_entrypoint()

        build_cmd = [
            "docker", "build",
            "-t", self.image_tag,
            "-t", f"{self.config.image_name}:latest"
        ]
        if no_cache:
            build_cmd.append("--no-cache")
        build_cmd.append(str(self.build_dir))

        run_command(build_cmd, timeout=1800)  # 30 min timeout
        logger.info(f"Successfully built image: {self.image_tag}")
        return self.image_tag


# =============================================================================
# Ops Manager Deployer
# =============================================================================

class OpsManagerDeployer:
    """Deploys MongoDB Ops Manager in Docker with HTTPS."""

    def __init__(self, cert_dir: str, config: OpsManagerConfig, appdb_config: AppDbConfig):
        self.cert_dir = Path(cert_dir).resolve()
        self.config = config
        self.appdb_config = appdb_config
        self.data_dir = Path(config.data_dir).resolve()
        self.gen_key_file = Path(config.gen_key_file).resolve()

        # Ensure data directory exists
        self.data_dir.mkdir(parents=True, exist_ok=True)

    def _run_docker(self, args: List[str], check: bool = True) -> subprocess.CompletedProcess:
        """Run a Docker command."""
        return run_command(["docker"] + args, check=check)

    def create_network(self) -> None:
        """Create Docker network if it doesn't exist."""
        logger.info("Creating Docker network...")
        result = self._run_docker(["network", "ls", "--format", "{{.Name}}"], check=False)
        if self.config.network_name not in result.stdout.split('\n'):
            self._run_docker(["network", "create", self.config.network_name])
            logger.info(f"Created network: {self.config.network_name}")
        else:
            logger.info(f"Network already exists: {self.config.network_name}")

    def wait_for_appdb(self, timeout: int = 60) -> bool:
        """Wait for MongoDB AppDB to be ready using health check."""
        logger.info("Waiting for MongoDB AppDB to be ready...")
        start = time.time()
        while time.time() - start < timeout:
            result = self._run_docker([
                "exec", self.appdb_config.container_name,
                "mongosh", "--quiet", "--eval", "db.adminCommand('ping')"
            ], check=False)
            if result.returncode == 0:
                logger.info("MongoDB AppDB is ready")
                return True
            time.sleep(2)
        raise TimeoutError(f"MongoDB AppDB not ready within {timeout}s")

    def deploy_appdb(self) -> None:
        """Deploy MongoDB instance for Ops Manager Application Database."""
        logger.info("Deploying Application Database (MongoDB)...")

        # Remove existing container
        result = self._run_docker(
            ["ps", "-a", "--filter", f"name=^{self.appdb_config.container_name}$", "--format", "{{.Names}}"],
            check=False
        )
        if self.appdb_config.container_name in result.stdout:
            logger.info(f"Removing existing container: {self.appdb_config.container_name}")
            self._run_docker(["rm", "-f", self.appdb_config.container_name])

        # Build run command
        run_cmd = [
            "run", "-d",
            "--name", self.appdb_config.container_name,
            "--network", self.config.network_name,
            "-e", f"MONGO_INITDB_ROOT_USERNAME={self.appdb_config.username}",
            "-e", f"MONGO_INITDB_ROOT_PASSWORD={self.appdb_config.password}",
        ]

        # Add volume for persistence if specified
        if self.appdb_config.data_volume:
            run_cmd.extend(["-v", f"{self.appdb_config.data_volume}:/data/db"])

        run_cmd.extend([self.appdb_config.image, "--bind_ip_all"])

        self._run_docker(run_cmd)
        logger.info(f"Started MongoDB container: {self.appdb_config.container_name}")

        # Wait for MongoDB to be ready
        self.wait_for_appdb()

    def deploy_ops_manager(self) -> None:
        """Deploy MongoDB Ops Manager container with HTTPS."""
        logger.info("Deploying MongoDB Ops Manager...")

        # Remove existing container
        result = self._run_docker(
            ["ps", "-a", "--filter", f"name=^{self.config.container_name}$", "--format", "{{.Names}}"],
            check=False
        )
        if self.config.container_name in result.stdout:
            logger.info(f"Removing existing container: {self.config.container_name}")
            self._run_docker(["rm", "-f", self.config.container_name])

        # Load or create gen.key for persistence across restarts
        gen_key = load_or_create_gen_key(self.gen_key_file)

        # Create mongodb-releases directory for binary caching
        releases_dir = self.data_dir / "mongodb-releases"
        releases_dir.mkdir(parents=True, exist_ok=True)

        # Prepare paths (convert to Docker-compatible format on Windows)
        cert_mount = str(self.cert_dir).replace('\\', '/')
        releases_mount = str(releases_dir).replace('\\', '/')

        mongo_uri = (
            f"mongodb://{self.appdb_config.username}:{self.appdb_config.password}"
            f"@{self.appdb_config.container_name}:27017/?authSource=admin"
        )
        # Use host.docker.internal for central URL so Kubernetes pods can reach Ops Manager
        # for binary downloads. Agents inside k8s pods will use this URL.
        central_url = f"https://host.docker.internal:{self.config.https_port}"

        # Build run command
        run_cmd = [
            "run", "-d",
            "--name", self.config.container_name,
            "--network", self.config.network_name,
            "-p", f"{self.config.https_port}:8443",
            "-v", f"{cert_mount}:/etc/mongodb-mms/certs:ro",
            "-v", f"{releases_mount}:/opt/mongodb/mms/mongodb-releases",  # Persist downloaded binaries
            "-e", f"MONGO_URI={mongo_uri}",
            "-e", f"MMS_CENTRAL_URL={central_url}",
            "-e", "MMS_HTTPS_PORT=8443",
            "-e", f"MMS_EMAIL_DOMAIN={self.config.email_domain}",
            "-e", f"MMS_SMTP_HOSTNAME={self.config.smtp_hostname}",
            "-e", f"MMS_SMTP_PORT={self.config.smtp_port}",
            "-e", f"MMS_GEN_KEY={gen_key}",  # Persist gen.key across restarts
        ]

        # Add resource limits
        if self.config.memory_limit:
            run_cmd.extend(["--memory", self.config.memory_limit])
        if self.config.cpu_limit:
            run_cmd.extend(["--cpus", str(self.config.cpu_limit)])

        run_cmd.append(f"{self.config.image_name}:latest")
        self._run_docker(run_cmd)
        logger.info(f"Started Ops Manager container: {self.config.container_name}")
        logger.info(f"MongoDB releases cached in: {releases_dir}")

    def cleanup(self) -> None:
        """Remove all deployed containers and network."""
        logger.info("Cleaning up...")
        self._run_docker(["rm", "-f", self.config.container_name], check=False)
        self._run_docker(["rm", "-f", self.appdb_config.container_name], check=False)
        self._run_docker(["network", "rm", self.config.network_name], check=False)
        logger.info("Cleanup complete")

    def deploy_all(self) -> None:
        """Deploy complete Ops Manager setup."""
        self.create_network()
        self.deploy_appdb()
        self.deploy_ops_manager()
        logger.info(f"Deployment complete. Ops Manager: https://{self.config.hostname}:{self.config.https_port}")

    def backup(self, backup_dir: str) -> bool:
        """Backup Ops Manager data and configuration."""
        backup_path = Path(backup_dir)
        backup_path.mkdir(parents=True, exist_ok=True)
        timestamp = time.strftime("%Y%m%d_%H%M%S")

        logger.info(f"Creating backup in {backup_path}...")

        try:
            # Backup AppDB
            appdb_backup = backup_path / f"appdb_{timestamp}.archive"
            self._run_docker([
                "exec", self.appdb_config.container_name,
                "mongodump", "--archive=/tmp/backup.archive",
                "-u", self.appdb_config.username,
                "-p", self.appdb_config.password,
                "--authenticationDatabase", "admin"
            ])
            self._run_docker([
                "cp", f"{self.appdb_config.container_name}:/tmp/backup.archive",
                str(appdb_backup)
            ])
            logger.info(f"AppDB backup saved: {appdb_backup}")

            # Backup Ops Manager config
            config_backup = backup_path / f"conf-mms_{timestamp}.properties"
            self._run_docker([
                "cp", f"{self.config.container_name}:/opt/mongodb/mms/conf/conf-mms.properties",
                str(config_backup)
            ])
            logger.info(f"Config backup saved: {config_backup}")

            return True
        except Exception as e:
            logger.error(f"Backup failed: {e}")
            return False

    def restore(self, backup_dir: str, timestamp: str) -> bool:
        """Restore Ops Manager data from backup."""
        backup_path = Path(backup_dir)

        logger.info(f"Restoring from backup {timestamp}...")

        try:
            # Restore AppDB
            appdb_backup = backup_path / f"appdb_{timestamp}.archive"
            if appdb_backup.exists():
                self._run_docker([
                    "cp", str(appdb_backup),
                    f"{self.appdb_config.container_name}:/tmp/backup.archive"
                ])
                self._run_docker([
                    "exec", self.appdb_config.container_name,
                    "mongorestore", "--archive=/tmp/backup.archive", "--drop",
                    "-u", self.appdb_config.username,
                    "-p", self.appdb_config.password,
                    "--authenticationDatabase", "admin"
                ])
                logger.info("AppDB restored")

            # Restore config
            config_backup = backup_path / f"conf-mms_{timestamp}.properties"
            if config_backup.exists():
                self._run_docker([
                    "cp", str(config_backup),
                    f"{self.config.container_name}:/opt/mongodb/mms/conf/conf-mms.properties"
                ])
                # Restart Ops Manager
                self._run_docker(["restart", self.config.container_name])
                logger.info("Config restored and Ops Manager restarted")

            return True
        except Exception as e:
            logger.error(f"Restore failed: {e}")
            return False


# =============================================================================
# Ops Manager Configurator
# =============================================================================

class OpsManagerConfigurator:
    """Configures MongoDB Ops Manager initial setup."""

    def __init__(self, config: OpsManagerConfig):
        self.config = config
        self.base_url = f"https://{config.hostname}:{config.https_port}"

        # SSL context for self-signed certs
        self.ssl_context = ssl.create_default_context()
        self.ssl_context.check_hostname = False
        self.ssl_context.verify_mode = ssl.CERT_NONE

        # API credentials
        self.api_public_key: Optional[str] = None
        self.api_private_key: Optional[str] = None
        self.user_id: Optional[str] = None
        self.org_id: Optional[str] = None
        self.project_id: Optional[str] = None
        # Multi-project support
        self.single_cluster_project_id: Optional[str] = None
        self.multi_cluster_project_id: Optional[str] = None

        # Cached opener for authenticated requests
        self._auth_opener: Optional[urllib.request.OpenerDirector] = None

    @retry_with_backoff(max_retries=3)
    def _make_request(self, endpoint: str, method: str = "GET",
                      data: Optional[Dict] = None) -> Dict[str, Any]:
        """Make an HTTP request to Ops Manager API."""
        url = f"{self.base_url}{endpoint}"
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json"
        }

        request_data = json.dumps(data).encode('utf-8') if data else None
        req = urllib.request.Request(url, data=request_data, headers=headers, method=method)

        try:
            with urllib.request.urlopen(req, context=self.ssl_context, timeout=30) as response:
                response_data = response.read().decode('utf-8')
                return json.loads(response_data) if response_data else {}
        except urllib.error.HTTPError as e:
            error_body = e.read().decode('utf-8') if e.fp else ""
            logger.error(f"HTTP {e.code}: {e.reason} - {error_body}")
            raise

    def _get_auth_opener(self) -> urllib.request.OpenerDirector:
        """Get or create cached authenticated opener."""
        if self._auth_opener is None and self.api_public_key and self.api_private_key:
            password_mgr = urllib.request.HTTPPasswordMgrWithDefaultRealm()
            password_mgr.add_password(None, self.base_url, self.api_public_key, self.api_private_key)
            auth_handler = urllib.request.HTTPDigestAuthHandler(password_mgr)
            self._auth_opener = urllib.request.build_opener(
                auth_handler,
                urllib.request.HTTPSHandler(context=self.ssl_context)
            )
        return self._auth_opener

    @retry_with_backoff(max_retries=3)
    def _make_authenticated_request(self, endpoint: str, method: str = "GET",
                                     data: Optional[Dict] = None) -> Dict[str, Any]:
        """Make an authenticated API request using digest auth."""
        if not self.api_public_key or not self.api_private_key:
            raise ValueError("API credentials not set")

        url = f"{self.base_url}{endpoint}"
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json"
        }

        opener = self._get_auth_opener()
        request_data = json.dumps(data).encode('utf-8') if data else None
        req = urllib.request.Request(url, data=request_data, headers=headers, method=method)

        try:
            with opener.open(req, timeout=30) as response:
                response_data = response.read().decode('utf-8')
                return json.loads(response_data) if response_data else {}
        except urllib.error.HTTPError as e:
            error_body = e.read().decode('utf-8') if e.fp else ""
            logger.error(f"HTTP {e.code}: {e.reason} - {error_body}")
            raise

    def wait_for_ops_manager(self, timeout: Optional[int] = None) -> bool:
        """Wait for Ops Manager to be ready."""
        timeout = timeout or self.config.config_timeout
        logger.info("Waiting for Ops Manager to be ready...")
        start_time = time.time()

        while time.time() - start_time < timeout:
            try:
                req = urllib.request.Request(f"{self.base_url}/user/login")
                with urllib.request.urlopen(req, context=self.ssl_context, timeout=10) as response:
                    if response.status in [200, 302, 303]:
                        logger.info("Ops Manager is ready!")
                        return True
            except (urllib.error.URLError, urllib.error.HTTPError, OSError):
                elapsed = int(time.time() - start_time)
                logger.info(f"Waiting for Ops Manager... ({elapsed}s)")
                time.sleep(10)

        raise TimeoutError(f"Ops Manager not ready within {timeout}s")

    def create_first_user(self) -> bool:
        """Create the first admin user and get API key."""
        # Ensure password is generated (lazy generation)
        password = self.config.ensure_password()

        logger.info(f"Creating admin user: {self.config.admin_username}")
        logger.debug(f"Password: {mask_sensitive(password)}")

        user_data = {
            "username": self.config.admin_username,
            "password": password,
            "firstName": "Admin",
            "lastName": "User",
            "emailAddress": f"{self.config.admin_username}@{self.config.email_domain}"
        }

        try:
            result = self._make_request(
                "/api/public/v1.0/unauth/users",
                method="POST",
                data=user_data
            )
            logger.info("Admin user created successfully!")

            # Extract API key
            if "programmaticApiKey" in result:
                api_key = result["programmaticApiKey"]
                self.api_public_key = api_key.get("publicKey")
                self.api_private_key = api_key.get("privateKey")
                logger.info(f"API Key obtained: {self.api_public_key}")
            elif "apiKey" in result:
                self.api_public_key = result["apiKey"].get("publicKey")
                self.api_private_key = result["apiKey"].get("privateKey")
                if self.api_public_key:
                    logger.info(f"API Key obtained: {self.api_public_key}")

            if "id" in result:
                self.user_id = result["id"]

            # Add IP access list entries for Kubernetes operator connectivity
            if self.api_public_key:
                self._configure_api_access_list()

            return True
        except urllib.error.HTTPError as e:
            if e.code == 409:
                logger.info("Admin user already exists")
                return True
            raise

    def _configure_api_access_list(self) -> None:
        """Configure API access list to allow connections from Docker/Kubernetes networks."""
        logger.info("Configuring API access list for Kubernetes operator...")

        # Add common Docker and Kubernetes network ranges
        # These ranges cover typical Docker bridge networks and kind cluster networks
        ip_ranges = [
            "0.0.0.0/0",  # Allow all IPs (for development/testing)
        ]

        for cidr in ip_ranges:
            try:
                self._make_authenticated_request(
                    f"/api/public/v1.0/users/{self.user_id}/accessList",
                    method="POST",
                    data=[{"cidrBlock": cidr}]
                )
                logger.info(f"Added {cidr} to API access list")
            except urllib.error.HTTPError as e:
                if e.code == 409:
                    logger.debug(f"Access list entry {cidr} already exists")
                else:
                    logger.warning(f"Failed to add {cidr} to access list: {e}")
            except Exception as e:
                logger.warning(f"Failed to add {cidr} to access list: {e}")

    def _create_single_project(self, project_name: str) -> Optional[str]:
        """Create a single project and return its ID."""
        logger.info(f"Creating project: {project_name}")
        try:
            result = self._make_authenticated_request(
                "/api/public/v1.0/groups",
                method="POST",
                data={"name": project_name}
            )
            project_id = result.get("id")
            org_id = result.get("orgId")
            logger.info(f"Project created: {project_name} (ID: {project_id})")
            if org_id and not self.org_id:
                self.org_id = org_id
                logger.info(f"Organization ID: {org_id}")
            return project_id
        except urllib.error.HTTPError as e:
            if e.code == 409:
                logger.info(f"Project '{project_name}' already exists")
                # Try to get existing project ID
                return self._get_project_id_by_name(project_name)
            logger.error(f"Project creation failed: {e}")
            return None

    def _get_project_id_by_name(self, project_name: str) -> Optional[str]:
        """Get project ID by name."""
        try:
            result = self._make_authenticated_request("/api/public/v1.0/groups")
            for group in result.get("results", []):
                if group.get("name") == project_name:
                    return group.get("id")
        except Exception as e:
            logger.warning(f"Could not fetch project ID for {project_name}: {e}")
        return None

    def create_projects(self) -> bool:
        """Create both SingleCluster and MultiCluster projects."""
        if not self.api_public_key or not self.api_private_key:
            logger.warning("No API key available - cannot create projects")
            return False

        # Create SingleCluster project
        self.single_cluster_project_id = self._create_single_project("SingleCluster")
        # Create MultiCluster project
        self.multi_cluster_project_id = self._create_single_project("MultiCluster")

        # For backwards compatibility, set project_id to SingleCluster
        self.project_id = self.single_cluster_project_id

        return bool(self.single_cluster_project_id and self.multi_cluster_project_id)

    def create_project(self) -> bool:
        """Create projects - creates both SingleCluster and MultiCluster by default."""
        return self.create_projects()

    def save_api_key(self, filepath: str = "./ops-manager-api-key.json") -> bool:
        """Save the API key and credentials to a file with both projects."""
        if not self.api_public_key or not self.api_private_key:
            logger.warning("No API key to save")
            return False

        api_key_data = {
            "publicKey": self.api_public_key,
            "privateKey": self.api_private_key,
            "baseUrl": self.base_url,
            "username": self.config.admin_username,
            "password": self.config.admin_password,
            "orgId": self.org_id,
            # Include both projects
            "projects": {
                "singleCluster": {
                    "projectId": self.single_cluster_project_id,
                    "projectName": "SingleCluster"
                },
                "multiCluster": {
                    "projectId": self.multi_cluster_project_id,
                    "projectName": "MultiCluster"
                }
            },
            # Default project for backwards compatibility
            "projectId": self.single_cluster_project_id,
            "projectName": "SingleCluster"
        }

        try:
            with open(filepath, 'w') as f:
                json.dump(api_key_data, f, indent=2)
            logger.info(f"API key saved to: {filepath}")
            return True
        except IOError as e:
            logger.error(f"Failed to save API key: {e}")
            return False

    def configure_all(self, api_key_file: str = "./ops-manager-api-key.json") -> DeploymentResult:
        """Run complete initial configuration."""
        try:
            api_key_path = Path(api_key_file)

            self.wait_for_ops_manager()

            user_created = self.create_first_user()
            project_created = self.create_project() if self.api_public_key else False

            if self.api_public_key:
                # Delete existing credentials file only AFTER configuration succeeds
                # This preserves old credentials if configuration fails
                if api_key_path.exists():
                    api_key_path.unlink()
                    logger.info(f"Removed existing credentials file: {api_key_file}")
                self.save_api_key(api_key_file)

            # Print summary
            self._print_summary(api_key_file, project_created)

            return DeploymentResult(
                success=user_created,
                message="Configuration complete",
                api_public_key=self.api_public_key,
                api_private_key=self.api_private_key,
                org_id=self.org_id,
                project_id=self.project_id,
                base_url=self.base_url
            )
        except Exception as e:
            logger.error(f"Configuration failed: {e}")
            return DeploymentResult(success=False, message=str(e))

    def _print_summary(self, api_key_file: str, project_created: bool) -> None:
        """Print configuration summary."""
        print(f"\n{'='*60}")
        print("INITIAL CONFIGURATION COMPLETE")
        print(f"{'='*60}")
        print(f"URL: {self.base_url}")
        print(f"Username: {self.config.admin_username}")
        print(f"Password: {self.config.admin_password}")
        print()
        if self.api_public_key:
            print("Programmatic API Key (GLOBAL_OWNER):")
            print(f"  Public Key:  {self.api_public_key}")
            print(f"  Private Key: {mask_sensitive(self.api_private_key, 8)}")
            print(f"  Saved to: {api_key_file}")
            print()
        if self.org_id:
            print(f"Organization ID: {self.org_id}")
        print()
        print("Projects Created:")
        if self.single_cluster_project_id:
            print(f"  SingleCluster: {self.single_cluster_project_id}")
        if self.multi_cluster_project_id:
            print(f"  MultiCluster:  {self.multi_cluster_project_id}")
        print()
        print("Configuration applied via conf-mms.properties:")
        print("  - mms.ignoreInitialUiSetup=true")
        print("  - Email configuration completed")
        print("  - HTTPS certificates configured")
        print("  - MongoDB connection configured")
        if not project_created:
            print()
            print("Note: Organization/project can be created via the UI")
        print(f"{'='*60}")


# =============================================================================
# Main Function
# =============================================================================

def main():
    parser = argparse.ArgumentParser(
        description="Deploy MongoDB Ops Manager with HTTPS using custom certificates",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Full deployment
  python deploy_ops_manager.py

  # Skip certificate generation
  python deploy_ops_manager.py --skip-certs

  # Build image only
  python deploy_ops_manager.py --build-only

  # Cleanup
  python deploy_ops_manager.py --cleanup

  # With custom credentials
  python deploy_ops_manager.py --admin-username myuser --admin-password MyPass@123

  # With resource limits
  python deploy_ops_manager.py --memory-limit 4g --cpu-limit 2.0

  # Backup
  python deploy_ops_manager.py --backup --backup-dir ./backups
"""
    )

    # Certificate options
    cert_group = parser.add_argument_group("Certificate Options")
    cert_group.add_argument("--cert-dir", default="./certs", help="Certificate directory")
    cert_group.add_argument("--hostname", default="localhost", help="Server hostname")
    cert_group.add_argument("--ca-validity-days", type=int, default=3650, help="CA validity (days)")
    cert_group.add_argument("--cert-validity-days", type=int, default=365, help="Server cert validity (days)")

    # Deployment options
    deploy_group = parser.add_argument_group("Deployment Options")
    deploy_group.add_argument("--https-port", type=int, default=8443, help="HTTPS port")
    deploy_group.add_argument("--container-name", default="ops-manager", help="Container name")
    deploy_group.add_argument("--network-name", default="ops-manager-network", help="Docker network name")
    deploy_group.add_argument("--build-dir", default="./docker-build", help="Docker build directory")
    deploy_group.add_argument("--ops-manager-version", default=OPS_MANAGER_VERSION, help="Ops Manager version")
    deploy_group.add_argument("--rpm-path", help="Path to Ops Manager RPM file")

    # AppDB options
    appdb_group = parser.add_argument_group("Application Database Options")
    appdb_group.add_argument("--appdb-username", default="admin", help="AppDB admin username")
    appdb_group.add_argument("--appdb-password", default="password", help="AppDB admin password")
    appdb_group.add_argument("--appdb-image", default="mongo:6.0", help="AppDB Docker image")
    appdb_group.add_argument("--appdb-volume", help="AppDB data volume for persistence")

    # Admin options
    admin_group = parser.add_argument_group("Admin User Options")
    admin_group.add_argument("--admin-username", default="admin", help="Admin username")
    admin_group.add_argument("--admin-password", default=None,
                              help="Admin password (auto-generated if not provided)")
    admin_group.add_argument("--org-name", default="Default", help="Organization name")
    admin_group.add_argument("--project-name", default="Default", help="Project name")
    admin_group.add_argument("--api-key-file", default="./ops-manager-api-key.json",
                              help="Path to save API key credentials (default: ./ops-manager-api-key.json)")
    admin_group.add_argument("--email-domain", default="localhost.local", help="Email domain")

    # Data persistence options
    persist_group = parser.add_argument_group("Data Persistence Options")
    persist_group.add_argument("--data-dir", default="./data",
                                help="Directory for persistent data (gen.key, mongodb-releases)")
    persist_group.add_argument("--gen-key-file", default=None,
                                help="Path to gen.key file (default: <data-dir>/gen.key)")

    # Resource limits
    resource_group = parser.add_argument_group("Resource Limits")
    resource_group.add_argument("--memory-limit", help="Memory limit (e.g., 4g)")
    resource_group.add_argument("--cpu-limit", type=float, help="CPU limit (e.g., 2.0)")

    # Operation modes
    mode_group = parser.add_argument_group("Operation Modes")
    mode_group.add_argument("--skip-certs", action="store_true", help="Skip certificate generation")
    mode_group.add_argument("--skip-build", action="store_true", help="Skip Docker image build")
    mode_group.add_argument("--skip-config", action="store_true", help="Skip initial configuration")
    mode_group.add_argument("--certs-only", action="store_true", help="Only generate certificates")
    mode_group.add_argument("--build-only", action="store_true", help="Only build Docker image")
    mode_group.add_argument("--cleanup", action="store_true", help="Remove all containers and exit")
    mode_group.add_argument("--no-cache", action="store_true", help="Build without Docker cache")
    mode_group.add_argument("--config-timeout", type=int, default=300, help="Config timeout (seconds)")
    mode_group.add_argument("--dry-run", action="store_true",
                            help="Show what would be done without making changes")

    # Backup/Restore
    backup_group = parser.add_argument_group("Backup/Restore")
    backup_group.add_argument("--backup", action="store_true", help="Create backup")
    backup_group.add_argument("--restore", help="Restore from backup (timestamp)")
    backup_group.add_argument("--backup-dir", default="./backups", help="Backup directory")

    # Logging
    parser.add_argument("-v", "--verbose", action="store_true", help="Verbose output")

    args = parser.parse_args()

    # Configure logging level
    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    # Build configuration objects
    cert_config = CertificateConfig(
        output_dir=args.cert_dir,
        hostname=args.hostname,
        ca_validity_days=args.ca_validity_days,
        server_validity_days=args.cert_validity_days
    )

    appdb_config = AppDbConfig(
        username=args.appdb_username,
        password=args.appdb_password,
        image=args.appdb_image,
        data_volume=args.appdb_volume
    )

    # Determine gen_key_file path
    gen_key_file = args.gen_key_file if args.gen_key_file else str(Path(args.data_dir) / "gen.key")

    om_config = OpsManagerConfig(
        hostname=args.hostname,
        https_port=args.https_port,
        container_name=args.container_name,
        admin_username=args.admin_username,
        admin_password=args.admin_password,  # None triggers auto-generation
        org_name=args.org_name,
        project_name=args.project_name,
        email_domain=args.email_domain,
        memory_limit=args.memory_limit,
        cpu_limit=args.cpu_limit,
        config_timeout=args.config_timeout,
        network_name=args.network_name,
        version=args.ops_manager_version,
        gen_key_file=gen_key_file,
        data_dir=args.data_dir
    )

    cert_dir = Path(args.cert_dir).resolve()
    build_dir = Path(args.build_dir).resolve()

    # Initialize deployer
    deployer = OpsManagerDeployer(
        cert_dir=str(cert_dir),
        config=om_config,
        appdb_config=appdb_config
    )

    # Handle cleanup
    if args.cleanup:
        deployer.cleanup()
        return

    # Dry-run mode
    if args.dry_run:
        logger.info("=== DRY-RUN MODE ===")
        logger.info("The following actions would be performed:")
        if not args.skip_certs and not args.build_only:
            logger.info(f"  1. Generate certificates in: {cert_dir}")
            logger.info(f"     - CA certificate (valid {args.ca_validity_days} days)")
            logger.info(f"     - Server certificate (valid {args.cert_validity_days} days)")
            logger.info(f"     - SANs: localhost, ops-manager, host.docker.internal")
        if not args.skip_build:
            logger.info(f"  2. Build Docker image: {om_config.image_name}:{om_config.version}")
            logger.info(f"     - Base: rockylinux:8")
            logger.info(f"     - Ops Manager version: {om_config.version}")
        if not args.certs_only and not args.build_only:
            logger.info(f"  3. Create Docker network: {om_config.network_name}")
            logger.info(f"  4. Deploy AppDB container: {appdb_config.container_name}")
            logger.info(f"  5. Deploy Ops Manager container: {om_config.container_name}")
            logger.info(f"     - HTTPS port: {om_config.https_port}")
            logger.info(f"     - Central URL: https://host.docker.internal:{om_config.https_port}")
            logger.info(f"     - Data directory: {args.data_dir}")
        if not args.skip_config:
            logger.info(f"  6. Configure Ops Manager:")
            logger.info(f"     - Create admin user: {om_config.admin_username}")
            logger.info(f"     - Create organization: {om_config.org_name}")
            logger.info(f"     - Create project: {om_config.project_name}")
            logger.info(f"     - Save API key to: {args.api_key_file}")
        logger.info("=== END DRY-RUN ===")
        return

    # Handle backup
    if args.backup:
        deployer.backup(args.backup_dir)
        return

    # Handle restore
    if args.restore:
        deployer.restore(args.backup_dir, args.restore)
        return

    # Generate certificates
    if not args.skip_certs and not args.build_only:
        cert_gen = CertificateGenerator(cert_config)
        cert_gen.generate_all()
    elif args.skip_certs:
        logger.info(f"Using existing certificates in: {cert_dir}")

    if args.certs_only:
        logger.info("Certificate generation complete (--certs-only)")
        return

    # Build Docker image
    if not args.skip_build:
        builder = OpsManagerImageBuilder(
            build_dir=str(build_dir),
            config=om_config,
            rpm_path=args.rpm_path
        )
        builder.build_image(no_cache=args.no_cache)

    if args.build_only:
        logger.info("Docker image build complete (--build-only)")
        return

    # Deploy Ops Manager
    deployer.deploy_all()

    # Run initial configuration
    if not args.skip_config:
        configurator = OpsManagerConfigurator(om_config)
        try:
            configurator.configure_all(api_key_file=args.api_key_file)
        except Exception as e:
            logger.error(f"Configuration failed: {e}")
            print(f"\nYou can complete setup manually at: https://{args.hostname}:{args.https_port}")
    else:
        logger.info("Skipping initial configuration (--skip-config)")


if __name__ == "__main__":
    main()
