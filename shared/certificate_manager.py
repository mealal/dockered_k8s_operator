"""
Certificate management for MongoDB Kubernetes deployments.

This module provides utilities for generating TLS certificates for MongoDB
deployments, including CA certificates, server certificates, and client certificates.
"""

import logging
from pathlib import Path
from typing import List, Optional

from shared.utils import run_openssl

logger = logging.getLogger(__name__)


class CertificateManager:
    """Manages TLS certificate generation for MongoDB deployments."""

    def __init__(self, base_certs_dir: Path):
        """Initialize the certificate manager.

        Args:
            base_certs_dir: Base directory for certificates (e.g., ./certs)
        """
        self.base_certs_dir = Path(base_certs_dir)
        self.ca_cert = self.base_certs_dir / "ca.crt"
        self.ca_key = self.base_certs_dir / "ca.key"

    def ca_exists(self) -> bool:
        """Check if CA certificate and key exist."""
        return self.ca_cert.exists() and self.ca_key.exists()

    def generate_mongodb_certificate(
        self,
        output_dir: Path,
        cn: str,
        dns_sans: List[str],
        ip_sans: Optional[List[str]] = None,
        validity_days: int = 365
    ) -> bool:
        """Generate a TLS certificate for MongoDB.

        Args:
            output_dir: Directory to write certificate files to
            cn: Common Name for the certificate
            dns_sans: List of DNS Subject Alternative Names
            ip_sans: Optional list of IP Subject Alternative Names
            validity_days: Certificate validity in days (default: 365)

        Returns:
            True if successful, False otherwise
        """
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)

        if not self.ca_exists():
            logger.error("CA certificate not found. Run deploy_ops_manager.py first.")
            return False

        # Build SAN list
        san_parts = [f"DNS:{san}" for san in dns_sans]
        if ip_sans:
            san_parts.extend([f"IP:{ip}" for ip in ip_sans])
        san_list = ",".join(san_parts)

        # Create extension config file
        ext_file = output_dir / "mongodb-ext.cnf"
        ext_content = f"""[req]
distinguished_name = req_distinguished_name
req_extensions = v3_req
prompt = no

[req_distinguished_name]
CN = {cn}

[v3_req]
basicConstraints = CA:FALSE
keyUsage = critical, digitalSignature, keyEncipherment
extendedKeyUsage = serverAuth, clientAuth
subjectAltName = {san_list}
"""
        ext_file.write_text(ext_content)

        mongodb_key = output_dir / "mongodb.key"
        mongodb_csr = output_dir / "mongodb.csr"
        mongodb_cert = output_dir / "mongodb.crt"

        try:
            # Generate private key
            if not run_openssl(
                ["genrsa", "-out", str(mongodb_key), "2048"],
                "private key generation"
            ):
                return False

            # Generate CSR
            if not run_openssl(
                ["req", "-new", "-key", str(mongodb_key),
                 "-out", str(mongodb_csr), "-config", str(ext_file)],
                "CSR generation"
            ):
                return False

            # Sign certificate with CA
            if not run_openssl(
                ["x509", "-req", "-in", str(mongodb_csr),
                 "-CA", str(self.ca_cert), "-CAkey", str(self.ca_key),
                 "-CAcreateserial", "-out", str(mongodb_cert),
                 "-days", str(validity_days), "-extensions", "v3_req",
                 "-extfile", str(ext_file)],
                "certificate signing"
            ):
                return False

            logger.info(f"Generated MongoDB certificate: {mongodb_cert}")
            return True

        finally:
            # Clean up temporary files
            for temp_file in [mongodb_csr, ext_file]:
                if temp_file.exists():
                    temp_file.unlink()

    def generate_client_certificate(
        self,
        output_dir: Path,
        cn: str = "mongodb-client",
        validity_days: int = 365
    ) -> Optional[Path]:
        """Generate a client certificate for MongoDB authentication.

        Args:
            output_dir: Directory to write certificate files to
            cn: Common Name for the certificate (default: mongodb-client)
            validity_days: Certificate validity in days (default: 365)

        Returns:
            Path to the combined PEM file, or None on failure
        """
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)

        if not self.ca_exists():
            logger.error("CA certificate not found.")
            return None

        client_key = output_dir / f"{cn}.key"
        client_csr = output_dir / f"{cn}.csr"
        client_cert = output_dir / f"{cn}.crt"
        client_pem = output_dir / f"{cn}.pem"

        try:
            # Generate private key
            if not run_openssl(
                ["genrsa", "-out", str(client_key), "2048"],
                "client key generation"
            ):
                return None

            # Generate CSR
            if not run_openssl(
                ["req", "-new", "-key", str(client_key),
                 "-out", str(client_csr), "-subj", f"/CN={cn}"],
                "client CSR generation"
            ):
                return None

            # Sign with CA
            if not run_openssl(
                ["x509", "-req", "-in", str(client_csr),
                 "-CA", str(self.ca_cert), "-CAkey", str(self.ca_key),
                 "-CAcreateserial", "-out", str(client_cert),
                 "-days", str(validity_days)],
                "client certificate signing"
            ):
                return None

            # Combine key and cert into PEM
            with open(client_pem, 'w') as pem:
                pem.write(client_key.read_text())
                pem.write(client_cert.read_text())

            logger.info(f"Generated client certificate: {client_pem}")
            return client_pem

        finally:
            # Clean up temporary files
            for temp_file in [client_csr]:
                if temp_file.exists():
                    temp_file.unlink()


def generate_single_cluster_sans(
    rs_name: str,
    rs_namespace: str,
    members_count: int,
    include_localhost: bool = True
) -> tuple:
    """Generate SANs for a single-cluster MongoDB deployment.

    Args:
        rs_name: ReplicaSet name
        rs_namespace: Kubernetes namespace
        members_count: Number of ReplicaSet members
        include_localhost: Whether to include localhost SANs

    Returns:
        Tuple of (dns_sans, ip_sans)
    """
    dns_sans = []

    # Pod hostnames
    for i in range(members_count):
        dns_sans.append(f"{rs_name}-{i}.{rs_name}-svc.{rs_namespace}.svc.cluster.local")

    # Service hostnames
    dns_sans.append(f"{rs_name}-svc.{rs_namespace}.svc.cluster.local")
    dns_sans.append(f"*.{rs_name}-svc.{rs_namespace}.svc.cluster.local")

    if include_localhost:
        dns_sans.append("localhost")

    ip_sans = ["127.0.0.1", "::1"] if include_localhost else []

    return dns_sans, ip_sans


def generate_multi_cluster_sans(
    rs_name: str,
    rs_namespace: str,
    central_members: int,
    member_members: int,
    central_external_domain: str,
    member_external_domain: str,
    include_localhost: bool = True
) -> tuple:
    """Generate SANs for a multi-cluster MongoDB deployment.

    Args:
        rs_name: ReplicaSet name
        rs_namespace: Kubernetes namespace
        central_members: Number of members in central cluster
        member_members: Number of members in member cluster
        central_external_domain: External domain for central cluster
        member_external_domain: External domain for member cluster
        include_localhost: Whether to include localhost SANs

    Returns:
        Tuple of (dns_sans, ip_sans)
    """
    dns_sans = []

    # Central cluster pods
    for i in range(central_members):
        dns_sans.append(f"{rs_name}-{i}.{rs_name}-svc.{rs_namespace}.svc.cluster.local")
        dns_sans.append(f"{rs_name}-0-{i}.{central_external_domain}")

    # Member cluster pods
    for i in range(member_members):
        dns_sans.append(f"{rs_name}-{i}.{rs_name}-svc.{rs_namespace}.svc.cluster.local")
        dns_sans.append(f"{rs_name}-1-{i}.{member_external_domain}")

    # Common SANs
    dns_sans.extend([
        f"{rs_name}-svc.{rs_namespace}.svc.cluster.local",
        f"*.{rs_name}-svc.{rs_namespace}.svc.cluster.local",
        central_external_domain,
        member_external_domain,
    ])

    if include_localhost:
        dns_sans.append("localhost")

    ip_sans = ["127.0.0.1", "::1"] if include_localhost else []

    return dns_sans, ip_sans
