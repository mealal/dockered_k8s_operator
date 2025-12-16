# MongoDB Enterprise Kubernetes Operator POC

A proof-of-concept for deploying MongoDB Enterprise Kubernetes Operator with Ops Manager running in Docker containers.

## Overview

This project provides automated deployment scripts for:

1. **MongoDB Ops Manager** - Running in Docker with HTTPS enabled
2. **MongoDB Enterprise Kubernetes Operator** - Deployed to a kind (Kubernetes IN Docker) cluster
3. **MongoDB Replica Set** - Managed by the operator and registered in Ops Manager

## Features

- **Security enabled by default**: Authentication (SCRAM + X509), TLS, and external access are enabled by default
- **External connectivity**: Connect to MongoDB from outside Kubernetes via NodePort services
- **Template-based configuration**: All Kubernetes manifests use YAML templates for easy customization
- **Automated certificate management**: TLS certificates generated automatically for MongoDB and client connections
- **Dual authentication**: Both SCRAM (username/password) and X509 (client certificate) authentication supported
- **Password masking**: Passwords are masked in output by default for security
- **Health checks**: Automatic connectivity verification after deployment
- **Wait by default**: Scripts wait for MongoDB to reach Running state (use `--no-wait` to skip)

## Architecture

### Single-Cluster Mode

```
+------------------+     +------------------------+     +---------------------+
|  Ops Manager     |<--->|  MongoDB Enterprise    |<--->|  MongoDB Replica    |
|  (Docker)        |     |  K8s Operator (kind)   |     |  Set (kind)         |
+------------------+     +------------------------+     +---------------------+
     |                          |
     v                          v
+------------------+     +------------------------+
|  AppDB (MongoDB) |     |  Automation Agents     |
|  (Docker)        |     |  (in MongoDB pods)     |
+------------------+     +------------------------+
```

### Multi-Cluster Mode

```
+------------------+     +--------------------------------------------------+
|  Ops Manager     |     |              CENTRAL CLUSTER                     |
|  (Docker)        |<--->|  +------------------------------------------+    |
+------------------+     |  | MongoDB Operator (watches both clusters) |    |
                         |  +------------------------------------------+    |
                         |  +------------+ +------------+ +------------+    |
                         |  | MongoDB-0  | | MongoDB-1  | | MongoDB-2  |    |
                         |  | :30100     | | :30101     | | :30102     |    |
                         |  +------------+ +------------+ +------------+    |
                         +--------------------------------------------------+
                                              |
                         Cross-cluster replication via CoreDNS + NodePort
                                              |
                         +--------------------------------------------------+
                         |              MEMBER CLUSTER                      |
                         |  +------------+ +------------+                   |
                         |  | MongoDB-3  | | MongoDB-4  |                   |
                         |  | :30200     | | :30201     |                   |
                         |  +------------+ +------------+                   |
                         +--------------------------------------------------+
```

## Version Compatibility

This project has been tested with the following versions:

| Component | Tested Version | Notes |
|-----------|----------------|-------|
| MongoDB Ops Manager | 7.0.x, 8.0.x | Docker image with HTTPS |
| MongoDB Enterprise Server | 7.0.25-ent | Configured in templates |
| MongoDB Enterprise K8s Operator | 1.33.x | Latest stable |
| Kubernetes (kind) | 1.28.x | Kind v0.20+ recommended |
| Python | 3.8+ | 3.10+ recommended |
| Docker | 24.0+ | Docker Desktop or Engine |

> **Note**: For the latest operator compatibility matrix, see the
> [MongoDB Kubernetes Operator compatibility page](https://www.mongodb.com/docs/kubernetes-operator/stable/reference/compatibility/).

## System Requirements

### Resource Requirements

| Deployment Mode | Docker Memory | Docker CPUs | Disk Space |
|-----------------|---------------|-------------|------------|
| Single-cluster (3 nodes) | 6 GB minimum, 8 GB recommended | 4 cores | 10 GB |
| Multi-cluster (5 nodes) | 8 GB minimum, 12 GB recommended | 6 cores | 15 GB |

> **Note**: These are minimums for development/testing. Production deployments require significantly more resources.

### Software Prerequisites

- **Docker** - For running Ops Manager and kind cluster
- **Python 3.8+** - For deployment scripts
- **OpenSSL** - For TLS certificate generation
- **kubectl** - Kubernetes CLI (optional, can use Docker-based kubectl)

### Verify Prerequisites

```bash
# Check Docker is running
docker version

# Check Python version
python --version

# Check OpenSSL
openssl version

# Check kubectl (optional)
kubectl version --client
```

### Install Python Dependencies

```bash
# Install required dependencies
pip install -r requirements.txt

# For development (includes testing tools)
pip install -r requirements.txt -r requirements-dev.txt
```

## Quick Reference

| Task | Command |
|------|---------|
| Deploy Ops Manager | `python deploy_ops_manager.py` |
| Deploy single-cluster MongoDB | `python deploy_mongodb_k8s.py` |
| Deploy multi-cluster MongoDB | `python deploy_mongodb_k8s_multi.py` |
| Cleanup single-cluster | `python deploy_mongodb_k8s.py --cleanup` |
| Cleanup multi-cluster | `python deploy_mongodb_k8s_multi.py --cleanup` |
| Skip SSL verification (testing) | `--ssl-skip-verify --skip-preflight` |
| Don't wait for Running state | `--no-wait` |
| Show passwords in output | `--show-password` |
| View operator logs | `kubectl --kubeconfig .kube/config logs -n mongodb -l app.kubernetes.io/name=mongodb-enterprise-operator` |

## Deployment Workflow

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                           DEPLOYMENT WORKFLOW                                │
├─────────────────────────────────────────────────────────────────────────────┤
│                                                                              │
│   Step 1: Ops Manager              Step 2: Choose Deployment Mode           │
│   ┌─────────────────────┐                                                   │
│   │ deploy_ops_manager  │          ┌─────────────────────────────────────┐  │
│   │ .py                 │          │     Single-Cluster     Multi-Cluster│  │
│   │                     │          │     ┌───────────┐     ┌───────────┐ │  │
│   │ • Generate certs    │    OR    │     │ 1 kind    │     │ 2 kind    │ │  │
│   │ • Start containers  │─────────>│     │ cluster   │     │ clusters  │ │  │
│   │ • Create API keys   │          │     │ 3 MongoDB │     │ 5 MongoDB │ │  │
│   │ • Save credentials  │          │     │ nodes     │     │ nodes     │ │  │
│   └─────────────────────┘          │     └───────────┘     └───────────┘ │  │
│            │                       │           │                 │       │  │
│            v                       │           v                 v       │  │
│   ops-manager-api-key.json         │  deploy_mongodb   deploy_mongodb   │  │
│                                    │  _k8s.py          _k8s_multi.py    │  │
│                                    └─────────────────────────────────────┘  │
│                                                                              │
│   Step 3: Connect to MongoDB                                                 │
│   ┌─────────────────────────────────────────────────────────────────────┐   │
│   │ mongosh "mongodb://localhost:30000,..." --username admin            │   │
│   └─────────────────────────────────────────────────────────────────────┘   │
│                                                                              │
└─────────────────────────────────────────────────────────────────────────────┘
```

## Quick Start

### 1. Deploy Ops Manager

```bash
python deploy_ops_manager.py
```

This will:
- Generate TLS certificates in `./certs/`
- Start MongoDB (AppDB) and Ops Manager in Docker
- Create admin user and API keys
- Save credentials to `ops-manager-api-key.json`

### 2. Deploy MongoDB to Kubernetes

```bash
python deploy_mongodb_k8s.py
```

This will:
- Create a kind cluster with port mappings for external access
- Deploy MongoDB Enterprise Kubernetes Operator
- Configure connection to Ops Manager
- Deploy a 3-member MongoDB replica set with TLS and authentication enabled
- Create both SCRAM and X509 users
- Wait for MongoDB to reach Running state (use `--no-wait` to skip)
- Run connectivity health check

### 3. (Alternative) Deploy Multi-Cluster MongoDB

For a 5-node replica set spanning two Kubernetes clusters:

```bash
python deploy_mongodb_k8s_multi.py
```

This will:
- Create two kind clusters (central + member) with cross-cluster networking
- Deploy the operator on the central cluster with multi-cluster configuration
- Configure CoreDNS for cross-cluster DNS resolution
- Deploy a 5-member MongoDB replica set (3 on central, 2 on member)
- Set up NodePort services for external and cross-cluster access
- Wait for MongoDB to reach Running state (use `--no-wait` to skip)
- Run connectivity health check

### 4. Connect to MongoDB

#### Single-Cluster Mode

After deployment, connect using SCRAM authentication:

```bash
mongosh "mongodb://localhost:30000,localhost:30001,localhost:30002/?replicaSet=mongodb-rs&tls=true&tlsCAFile=./certs/ca.crt" \
  --username admin \
  --authenticationDatabase admin
```

Or using X509 authentication:

```bash
mongosh "mongodb://localhost:30000,localhost:30001,localhost:30002/?replicaSet=mongodb-rs&tls=true&tlsCAFile=./certs/ca.crt&authMechanism=MONGODB-X509&authSource=\$external" \
  --tlsCertificateKeyFile ./certs/mongodb/client.pem
```

#### Multi-Cluster Mode

Connect to the 5-node replica set (3 on central cluster + 2 on member cluster):

```bash
# SCRAM authentication
mongosh "mongodb://localhost:30100,localhost:30101,localhost:30102,localhost:30200,localhost:30201/?replicaSet=mongodb-multi-rs&tls=true&tlsCAFile=./certs/ca.crt" \
  --username admin \
  --authenticationDatabase admin
```

```bash
# X509 authentication
mongosh "mongodb://localhost:30100,localhost:30101,localhost:30102,localhost:30200,localhost:30201/?replicaSet=mongodb-multi-rs&tls=true&tlsCAFile=./certs/ca.crt&authMechanism=MONGODB-X509&authSource=\$external" \
  --tlsCertificateKeyFile ./certs/mongodb-multi/client.pem
```

> **Note**: Multi-cluster ports are 30100-30102 for central cluster and 30200-30201 for member cluster.

### 5. Access Ops Manager

Open https://localhost:8443 in your browser (accept the self-signed certificate).

Login credentials are saved in `ops-manager-api-key.json`.

## Project Structure

```
koperator_poc/
├── deploy_ops_manager.py           # Ops Manager deployment script
├── deploy_mongodb_k8s.py           # Single-cluster K8s deployment script
├── deploy_mongodb_k8s_multi.py     # Multi-cluster K8s deployment script
├── docker-compose.ops-manager.yml  # Docker Compose for Ops Manager
├── docker-build/                   # Custom Ops Manager Docker image
│   ├── Dockerfile
│   └── entrypoint.sh
├── shared/                         # Shared Python utilities module
│   ├── __init__.py                 # Module exports
│   ├── utils.py                    # Command execution, path conversion
│   ├── validators.py               # CLI argument validators
│   ├── models.py                   # Data models (OpsManagerCredentials)
│   ├── constants.py                # Centralized configuration values
│   ├── preflight.py                # Pre-flight validation checks
│   ├── decorators.py               # Retry/polling decorators, backoff strategies
│   ├── exceptions.py               # Custom exception hierarchy
│   ├── result.py                   # Result[T] type for error handling
│   ├── cleanup.py                  # Consolidated cleanup utilities
│   ├── user_manager.py             # SCRAM/X509 user management
│   ├── logging_config.py           # Standardized logging configuration
│   ├── yaml_manager_base.py        # YAML template processing with caching
│   ├── kind_manager_base.py        # Kind cluster management
│   ├── k8s_manager_base.py         # Kubernetes operations
│   ├── operator_deployer_base.py   # Operator deployment logic
│   ├── ops_manager_cleanup.py      # Ops Manager project cleanup
│   ├── certificate_manager.py      # TLS certificate generation
│   ├── x509_manager.py             # X509 client certificate management
│   ├── health_check.py             # MongoDB connectivity verification
│   └── ui_utils.py                 # Password masking, progress indicators
├── tests/                          # Unit tests
│   ├── test_result.py              # Tests for Result type
│   ├── test_health_check.py        # Tests for health checks
│   ├── test_cleanup.py             # Tests for cleanup utilities
│   └── test_ui_utils.py            # Tests for UI utilities
├── k8s/                            # Single-cluster YAML templates
│   ├── namespace.yaml              # Operator namespace
│   ├── mongodb-rs-namespace.yaml   # Replica set namespace
│   ├── ops-manager-secret.yaml     # API credentials template
│   ├── ops-manager-configmap.yaml  # Connection config template
│   ├── mongodb-replicaset.yaml     # MongoDB ReplicaSet CRD
│   └── ...                         # Other templates
├── k8s-multi/                      # Multi-cluster YAML templates
│   ├── mongodb-multicluster.yaml   # MongoDBMultiCluster CRD
│   ├── coredns-configmap.yaml      # CoreDNS configuration template
│   ├── kubeconfig-template.yaml    # Multi-cluster kubeconfig
│   └── ...                         # Other templates
│
│   ### Runtime-Generated Directories (created by scripts) ###
│
├── .kube/                          # Single-cluster kubeconfig (generated)
├── .kube-multi/                    # Multi-cluster kubeconfigs (generated)
├── certs/                          # TLS certificates (generated)
├── k8s/generated/                  # Processed YAML files (generated)
├── k8s-multi/generated/            # Processed YAML files (generated)
└── ops-manager-api-key.json        # API credentials (generated)
```

> **Note**: Directories marked as "generated" are created at runtime by the deployment
> scripts. They are excluded from version control via `.gitignore`.

## Configuration

### Namespaces

The deployment uses separate namespaces for better isolation:
- `mongodb` - Kubernetes operator
- `mongodb-rs` - MongoDB replica set pods

### Security Features (Always Enabled)

All security features are enabled by default and cannot be disabled:

| Feature | Description |
|---------|-------------|
| Authentication | SCRAM + X509 dual authentication |
| TLS | Full TLS encryption for all connections |
| External Access | NodePort services for external connectivity |

> **Note**: To customize static values like member count, MongoDB version, ports, or resources,
> edit the template directly at `k8s/mongodb-replicaset.yaml`.

### External Connectivity

External access is configured using:
- **NodePort services** - Each MongoDB pod gets a dedicated external service
- **Split-horizon DNS** - `replicaSetHorizons` maps internal DNS names to external `localhost:port` addresses
- **Default ports**:
  - Single-cluster: 30000, 30001, 30002
  - Multi-cluster: 30100-30102 (central), 30200-30201 (member)

**Why replicaSetHorizons is required**: MongoDB replica sets return member hostnames to clients. Without horizons, MongoDB returns internal Kubernetes DNS names (e.g., `mongodb-rs-0.mongodb-rs-svc.mongodb-rs.svc.cluster.local`) which cannot be resolved from outside the cluster. The horizons configuration tells MongoDB to advertise external addresses (`localhost:30000`) to clients connecting via the `external` horizon.

## Command Reference

### deploy_ops_manager.py

```bash
# Full deployment (password masked in output by default)
python deploy_ops_manager.py

# Show password in output (masked by default)
python deploy_ops_manager.py --show-password

# Custom admin credentials
python deploy_ops_manager.py --admin-username myuser --admin-password MySecureP@ss123

# Generate certificates only
python deploy_ops_manager.py --certs-only

# Build Docker image only
python deploy_ops_manager.py --build-only

# Clean up containers, volumes, and certificates
python deploy_ops_manager.py --cleanup

# Show what would be done (dry-run)
python deploy_ops_manager.py --dry-run
```

#### Ops Manager Flags

| Flag | Description |
|------|-------------|
| `--hostname` | Server hostname (default: localhost) |
| `--https-port` | HTTPS port (default: 8443) |
| `--admin-username` | Admin username (default: admin) |
| `--admin-password` | Admin password (auto-generated if not provided) |
| `--show-password` | Show passwords in output (default: masked) |
| `--certs-only` | Only generate certificates |
| `--build-only` | Only build Docker image |
| `--skip-certs` | Skip certificate generation |
| `--skip-build` | Skip Docker image build |
| `--skip-config` | Skip initial configuration |
| `--cleanup` | Remove containers, network, and certificates |
| `--dry-run` | Show what would be done without changes |
| `-v, --verbose` | Verbose output |

### deploy_mongodb_k8s.py

```bash
# Full deployment (waits for Running state by default)
python deploy_mongodb_k8s.py

# Deploy without waiting for Running state
python deploy_mongodb_k8s.py --no-wait

# Show passwords in output (masked by default)
python deploy_mongodb_k8s.py --show-password

# Skip connectivity health check after deployment
python deploy_mongodb_k8s.py --skip-health-check

# Skip TLS certificate validation for Ops Manager (testing only)
python deploy_mongodb_k8s.py --ssl-skip-verify

# Skip pre-flight checks (useful with --ssl-skip-verify)
python deploy_mongodb_k8s.py --ssl-skip-verify --skip-preflight

# Custom cluster configuration
python deploy_mongodb_k8s.py \
  --cluster-name my-cluster \
  --worker-nodes 2

# Show what would be done (dry-run)
python deploy_mongodb_k8s.py --dry-run

# Only create the kind cluster (skip operator and replica set)
python deploy_mongodb_k8s.py --cluster-only

# Print kubectl instructions without deploying
python deploy_mongodb_k8s.py --instructions-only

# Clean up kind cluster
python deploy_mongodb_k8s.py --cleanup

# Verify cleanup was successful
kind get clusters                    # Should not show mongodb-k8s
docker ps -a | grep mongodb-k8s      # Should return empty
```

#### Available Flags

| Flag | Description |
|------|-------------|
| `--cluster-name` | Kind cluster name (default: mongodb-k8s) |
| `--worker-nodes` | Number of worker nodes (default: 1) |
| `--kubeconfig-dir` | Directory for kubeconfig (default: ./.kube) |
| `--ssl-skip-verify` | Skip TLS validation for Ops Manager |
| `--skip-preflight` | Skip pre-flight validation checks |
| `--no-wait` | Don't wait for MongoDB Running state (default: wait) |
| `--wait-timeout` | Timeout for waiting in seconds (default: 600) |
| `--skip-health-check` | Skip MongoDB connectivity health check |
| `--show-password` | Show passwords in plaintext (default: masked) |
| `--dry-run` | Show what would be done without changes |
| `--cluster-only` | Only create the kind cluster |
| `--instructions-only` | Only print kubectl instructions |
| `--skip-operator` | Skip operator deployment |
| `--skip-replica-set` | Skip replica set deployment |
| `--cleanup` | Delete the kind cluster |
| `-v, --verbose` | Verbose output |

> **Note**: Namespace values are hardcoded to match YAML templates:
> - Operator namespace: `mongodb` (k8s/namespace.yaml)
> - Replica set namespace: `mongodb-rs` (k8s/mongodb-rs-namespace.yaml)

### deploy_mongodb_k8s_multi.py

```bash
# Full multi-cluster deployment (waits by default)
python deploy_mongodb_k8s_multi.py

# Deploy without waiting for Running state
python deploy_mongodb_k8s_multi.py --no-wait

# Show passwords in output (masked by default)
python deploy_mongodb_k8s_multi.py --show-password

# Skip connectivity health check after deployment
python deploy_mongodb_k8s_multi.py --skip-health-check

# Skip TLS certificate validation for Ops Manager (testing only)
python deploy_mongodb_k8s_multi.py --ssl-skip-verify

# Custom cluster names
python deploy_mongodb_k8s_multi.py \
  --central-cluster-name my-central \
  --member-cluster-name my-member

# Clean up both clusters
python deploy_mongodb_k8s_multi.py --cleanup

# Verify cleanup was successful
kind get clusters                    # Should not show mongodb-central or mongodb-member-1
docker ps -a | grep mongodb-         # Should return empty (for kind containers)
```

#### Multi-Cluster Flags

| Flag | Description |
|------|-------------|
| `--central-cluster-name` | Central cluster name (default: mongodb-central) |
| `--member-cluster-name` | Member cluster name (default: mongodb-member-1) |
| `--ssl-skip-verify` | Skip TLS validation for Ops Manager |
| `--skip-preflight` | Skip pre-flight validation checks |
| `--no-wait` | Don't wait for MongoDB Running state (default: wait) |
| `--wait-timeout` | Timeout for waiting in seconds (default: 600) |
| `--skip-health-check` | Skip MongoDB connectivity health check |
| `--show-password` | Show passwords in plaintext (default: masked) |
| `--cleanup` | Delete both kind clusters |
| `-v, --verbose` | Verbose output |

## Running Tests

The project includes unit tests for critical components:

```bash
# Run all tests
python -m pytest tests/ -v

# Run specific test file
python -m pytest tests/test_result.py -v

# Run with coverage
python -m pytest tests/ --cov=shared --cov-report=term-missing
```

## Known Limitations

### Windows-Specific Issues

- **Path length**: Windows has a 260-character path limit. Keep the project directory path short.
- **Encoding**: Set `PYTHONUTF8=1` environment variable to avoid encoding issues.
- **Line endings**: Git may convert line endings. Use `.gitattributes` to enforce LF.

### Docker Desktop Limitations

- **WSL2 networking**: On Windows with WSL2, `host.docker.internal` may not work in all scenarios.
- **Memory pressure**: Docker Desktop may become slow if memory is constrained. Monitor with `docker stats`.
- **VPN conflicts**: Some VPNs interfere with Docker networking. Disconnect VPN if experiencing connectivity issues.

### Deployment Constraints

- **Single Ops Manager**: Scripts assume one Ops Manager instance at `localhost:8443`.
- **Port conflicts**: Default ports (30000-30002, 30100-30102, 30200-30201) must be available.
- **No IPv6**: Scripts assume IPv4 networking only.

## Exit Codes

| Code | Meaning |
|------|---------|
| 0 | Success |
| 1 | General error |
| 2 | Pre-flight check failed |
| 3 | Cluster creation failed |
| 4 | Deployment failed |
| 5 | Cleanup failed |

## Troubleshooting

### Common Errors

#### Health Check Failures

If the connectivity health check fails after deployment:

```
Health check failed: Could not connect to MongoDB
```

**Possible causes and solutions:**
1. **MongoDB not yet ready** - Wait a few more minutes for all pods to reach Running state
2. **TLS certificate mismatch** - Ensure `./certs/ca.crt` matches the CA used by MongoDB
3. **Port mapping issues** - Verify kind port mappings with `docker port mongodb-k8s-control-plane`
4. **Firewall blocking** - Check if local firewall allows connections to ports 30000-30002

#### Template Placeholder Errors

```
Error: Template contains unresolved placeholder: {{VARIABLE}}
```

**Solution:** Ensure all required values are provided. Check `ops-manager-api-key.json` exists and contains valid credentials.

#### Unicode/Encoding Errors (Windows)

```
UnicodeDecodeError: 'charmap' codec can't decode byte
```

**Solution:** Set environment variable before running:
```cmd
set PYTHONUTF8=1
python deploy_mongodb_k8s.py
```

#### Agent Download Failures

```
Error while downloading the Mongodb agent
```

**Solution:** This usually indicates SSL certificate issues. Use `--ssl-skip-verify` flag:
```bash
python deploy_mongodb_k8s.py --ssl-skip-verify --skip-preflight
```

#### Pods Stuck in Pending State

**Cause:** Usually resource constraints or scheduling issues.

```bash
kubectl --kubeconfig .kube/config describe pod mongodb-rs-0 -n mongodb-rs
```

Check for events mentioning insufficient CPU, memory, or PVC issues.

### Viewing Logs

#### Operator Logs

```bash
kubectl --kubeconfig .kube/config logs -n mongodb \
  -l app.kubernetes.io/name=mongodb-enterprise-operator
```

#### MongoDB Pod Logs

```bash
# Database container logs
kubectl --kubeconfig .kube/config logs -n mongodb-rs \
  mongodb-rs-0 -c mongodb-enterprise-database

# Agent logs
kubectl --kubeconfig .kube/config logs -n mongodb-rs \
  mongodb-rs-0 -c mongodb-agent
```

### Check Replica Set Status

```bash
kubectl --kubeconfig .kube/config get mongodb -n mongodb-rs
kubectl --kubeconfig .kube/config get pods -n mongodb-rs
kubectl --kubeconfig .kube/config describe mongodb mongodb-rs -n mongodb-rs
```

### Check External Services

```bash
kubectl --kubeconfig .kube/config get svc -n mongodb-rs | grep external
```

### Connection Issues

If you can't connect via external ports, use port-forwarding as a fallback:

```bash
kubectl --kubeconfig .kube/config port-forward -n mongodb-rs mongodb-rs-0 27017:27017
```

## Documentation

- [Manual Deployment Guide](MANUAL_DEPLOYMENT.md) - Step-by-step single-cluster manual deployment
- [Multi-Cluster Deployment Guide](MANUAL_DEPLOYMENT_MULTI.md) - Multi-cluster manual deployment with CoreDNS
- [SSL Certificate Bypass](SSL_CERTIFICATE_BYPASS.md) - Documentation on SSL certificate bypass for testing
- [MongoDB Enterprise Kubernetes Operator Docs](https://www.mongodb.com/docs/kubernetes-operator/stable/)
- [MongoDB Ops Manager Docs](https://www.mongodb.com/docs/ops-manager/current/)
- [External Connectivity Guide](https://www.mongodb.com/docs/kubernetes-operator/v1.33/tutorial/connect-from-outside-k8s/)
- [Multi-Cluster Overview](https://www.mongodb.com/docs/kubernetes-operator/v1.33/multi-cluster-overview/)

## License

This is a proof-of-concept project for educational and testing purposes.
