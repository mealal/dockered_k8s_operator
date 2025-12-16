"""
X509 Certificate Manager for MongoDB Kubernetes deployments.

Provides client certificate generation and X509 user management
for MongoDB authentication. Extracted from deployment scripts to
reduce code duplication and improve maintainability.
"""

import logging
from pathlib import Path
from typing import Optional

from .utils import run_openssl

logger = logging.getLogger(__name__)


class X509CertificateManager:
    """Manages X509 client certificates for MongoDB authentication.

    Generates client certificates signed by the deployment CA and
    creates the corresponding MongoDBUser resources via the operator.

    Attributes:
        ca_cert_path: Path to CA certificate
        ca_key_path: Path to CA private key
        output_dir: Directory for generated client certificates
    """

    # Default certificate subject components
    DEFAULT_ORG = "MongoDB"
    DEFAULT_OU = "clients"
    DEFAULT_CN = "x509-client"
    DEFAULT_VALIDITY_DAYS = 365

    def __init__(
        self,
        ca_cert_path: Path,
        ca_key_path: Path,
        output_dir: Path
    ):
        """Initialize the X509 certificate manager.

        Args:
            ca_cert_path: Path to CA certificate file
            ca_key_path: Path to CA private key file
            output_dir: Directory where client certificates will be generated
        """
        self.ca_cert_path = Path(ca_cert_path)
        self.ca_key_path = Path(ca_key_path)
        self.output_dir = Path(output_dir)

    def validate(self) -> list[str]:
        """Validate that required CA files exist.

        Returns:
            List of validation error messages (empty if valid)
        """
        errors = []
        if not self.ca_cert_path.exists():
            errors.append(f"CA certificate not found: {self.ca_cert_path}")
        if not self.ca_key_path.exists():
            errors.append(f"CA private key not found: {self.ca_key_path}")
        return errors

    def generate_client_certificate(
        self,
        cn: str = DEFAULT_CN,
        org: str = DEFAULT_ORG,
        ou: str = DEFAULT_OU,
        validity_days: int = DEFAULT_VALIDITY_DAYS
    ) -> Optional[str]:
        """Generate a client certificate for X509 authentication.

        Creates a private key, CSR, and certificate signed by the CA.
        Also generates a combined PEM file for use with mongosh.

        Args:
            cn: Common Name for the client certificate
            org: Organization name
            ou: Organizational Unit name
            validity_days: Certificate validity period in days

        Returns:
            The certificate subject DN in RFC2253 format
            (e.g., CN=x509-client,OU=clients,O=MongoDB),
            or None if generation failed.
        """
        # Validate CA files exist
        validation_errors = self.validate()
        if validation_errors:
            for error in validation_errors:
                logger.error(error)
            logger.error("Run deploy_ops_manager.py first to generate CA certificates.")
            return None

        # Ensure output directory exists
        self.output_dir.mkdir(parents=True, exist_ok=True)

        logger.info(f"Generating X509 client certificate for '{cn}'...")

        # MongoDB extracts the DN from the certificate in reverse order (most specific first)
        # RFC2253 format: CN=...,OU=...,O=...
        x509_subject_dn = f"CN={cn},OU={ou},O={org}"

        # Define output paths
        client_key = self.output_dir / "client.key"
        client_csr = self.output_dir / "client.csr"
        client_cert = self.output_dir / "client.crt"
        client_pem = self.output_dir / "client.pem"  # Combined cert+key for mongosh
        ext_file = self.output_dir / "client-ext.cnf"

        # Create extension config file for client certificate
        ext_content = f"""[req]
distinguished_name = req_distinguished_name
req_extensions = v3_req
prompt = no

[req_distinguished_name]
O = {org}
OU = {ou}
CN = {cn}

[v3_req]
basicConstraints = CA:FALSE
keyUsage = critical, digitalSignature, keyEncipherment
extendedKeyUsage = clientAuth
"""
        try:
            ext_file.write_text(ext_content)

            # Generate private key
            if not run_openssl(
                ["genrsa", "-out", str(client_key), "2048"],
                "client private key generation"
            ):
                return None

            # Generate CSR with the specified subject
            if not run_openssl(
                ["req", "-new", "-key", str(client_key),
                 "-out", str(client_csr), "-config", str(ext_file)],
                "client CSR generation"
            ):
                return None

            # Sign certificate with CA
            if not run_openssl(
                ["x509", "-req", "-in", str(client_csr),
                 "-CA", str(self.ca_cert_path), "-CAkey", str(self.ca_key_path),
                 "-CAcreateserial", "-out", str(client_cert),
                 "-days", str(validity_days), "-extensions", "v3_req",
                 "-extfile", str(ext_file)],
                "client certificate signing"
            ):
                return None

            # Create combined PEM file for mongosh (cert + key)
            cert_content = client_cert.read_text()
            key_content = client_key.read_text()
            client_pem.write_text(cert_content + key_content)

            logger.info(f"Generated X509 client certificate: {client_cert}")
            logger.info(f"Generated combined PEM file: {client_pem}")
            logger.info(f"Certificate subject DN (RFC2253): {x509_subject_dn}")

            return x509_subject_dn

        finally:
            # Clean up temporary files (CSR and extension config)
            for temp_file in [client_csr, ext_file]:
                if temp_file.exists():
                    try:
                        temp_file.unlink()
                    except OSError:
                        pass  # Ignore cleanup errors

    @property
    def client_cert_path(self) -> Path:
        """Path to the generated client certificate."""
        return self.output_dir / "client.crt"

    @property
    def client_key_path(self) -> Path:
        """Path to the generated client private key."""
        return self.output_dir / "client.key"

    @property
    def client_pem_path(self) -> Path:
        """Path to the combined PEM file (cert + key)."""
        return self.output_dir / "client.pem"

    def cleanup(self) -> None:
        """Remove all generated client certificate files."""
        for file_path in [
            self.client_cert_path,
            self.client_key_path,
            self.client_pem_path
        ]:
            if file_path.exists():
                try:
                    file_path.unlink()
                    logger.debug(f"Removed: {file_path}")
                except OSError as e:
                    logger.warning(f"Failed to remove {file_path}: {e}")
