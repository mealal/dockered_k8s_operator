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

## Prerequisites

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
python deploy_mongodb_k8s.py --wait
```

This will:
- Create a kind cluster with port mappings for external access
- Deploy MongoDB Enterprise Kubernetes Operator
- Configure connection to Ops Manager
- Deploy a 3-member MongoDB replica set with TLS and authentication enabled
- Create both SCRAM and X509 users

### 3. (Alternative) Deploy Multi-Cluster MongoDB

For a 5-node replica set spanning two Kubernetes clusters:

```bash
python deploy_mongodb_k8s_multi.py --wait
```

This will:
- Create two kind clusters (central + member) with cross-cluster networking
- Deploy the operator on the central cluster with multi-cluster configuration
- Configure CoreDNS for cross-cluster DNS resolution
- Deploy a 5-member MongoDB replica set (3 on central, 2 on member)
- Set up NodePort services for external and cross-cluster access

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
│   ├── preflight.py                # Pre-flight validation checks
│   ├── decorators.py               # Retry decorators
│   ├── yaml_manager_base.py        # YAML template processing
│   ├── kind_manager_base.py        # Kind cluster management
│   ├── k8s_manager_base.py         # Kubernetes operations
│   ├── operator_deployer_base.py   # Operator deployment logic
│   └── ops_manager_cleanup.py      # Ops Manager project cleanup
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
- **Split-horizon DNS** - `replicaSetHorizons` configuration for proper replica set connectivity
- **Default ports**: 30000, 30001, 30002 (configured in `k8s/mongodb-replicaset.yaml`)

## Command Reference

### deploy_ops_manager.py

```bash
# Full deployment
python deploy_ops_manager.py

# Generate certificates only
python deploy_ops_manager.py --certs-only

# Clean up containers and volumes
python deploy_ops_manager.py --cleanup
```

### deploy_mongodb_k8s.py

```bash
# Full deployment with all security features
python deploy_mongodb_k8s.py --wait

# Skip TLS certificate validation for Ops Manager (testing only)
python deploy_mongodb_k8s.py --ssl-skip-verify --wait

# Skip pre-flight checks (useful with --ssl-skip-verify)
python deploy_mongodb_k8s.py --ssl-skip-verify --skip-preflight --wait

# Custom cluster and namespace configuration
python deploy_mongodb_k8s.py \
  --cluster-name my-cluster \
  --operator-namespace mongodb \
  --rs-namespace mongodb-rs \
  --worker-nodes 2 \
  --wait

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
| `--operator-namespace` | Operator namespace (default: mongodb) |
| `--rs-namespace` | Replica set namespace (default: mongodb-rs) |
| `--worker-nodes` | Number of worker nodes (default: 1) |
| `--ssl-skip-verify` | Skip TLS validation for Ops Manager |
| `--skip-preflight` | Skip pre-flight validation checks |
| `--wait` | Wait for MongoDB to reach Running state |
| `--wait-timeout` | Timeout for --wait in seconds (default: 600) |
| `--dry-run` | Show what would be done without changes |
| `--cluster-only` | Only create the kind cluster |
| `--instructions-only` | Only print kubectl instructions |
| `--skip-operator` | Skip operator deployment |
| `--skip-replica-set` | Skip replica set deployment |
| `--cleanup` | Delete the kind cluster |
| `-v, --verbose` | Verbose output |

### deploy_mongodb_k8s_multi.py

```bash
# Full multi-cluster deployment
python deploy_mongodb_k8s_multi.py --wait

# Skip TLS certificate validation for Ops Manager (testing only)
python deploy_mongodb_k8s_multi.py --ssl-skip-verify --wait

# Custom cluster names
python deploy_mongodb_k8s_multi.py \
  --central-cluster-name my-central \
  --member-cluster-name my-member \
  --wait

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
| `--wait` | Wait for MongoDB to reach Running state |
| `--wait-timeout` | Timeout for --wait in seconds (default: 900) |
| `--cleanup` | Delete both kind clusters |
| `-v, --verbose` | Verbose output |

## Troubleshooting

### Operator Logs

```bash
kubectl --kubeconfig .kube/config logs -n mongodb \
  -l app.kubernetes.io/name=mongodb-enterprise-operator
```

### MongoDB Pod Logs

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
