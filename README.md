# MongoDB Enterprise Kubernetes Operator POC

A proof-of-concept for deploying MongoDB Enterprise Kubernetes Operator with Ops Manager running in Docker containers.

## Overview

This project provides automated deployment scripts for:

1. **MongoDB Ops Manager** - Running in Docker with HTTPS enabled
2. **MongoDB Enterprise Kubernetes Operator** - Deployed to a kind (Kubernetes IN Docker) cluster
3. **MongoDB Replica Set** - Managed by the operator and registered in Ops Manager

## Architecture

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

## Prerequisites

- **Docker** - For running Ops Manager and kind cluster
- **Python 3.8+** - For deployment scripts
- **kubectl** - Kubernetes CLI (optional, can use Docker-based kubectl)

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
- Create a kind cluster with port mappings
- Deploy MongoDB Enterprise Kubernetes Operator
- Configure connection to Ops Manager
- Deploy a 3-member MongoDB replica set

### 3. Access Ops Manager

Open https://localhost:8443 in your browser (accept the self-signed certificate).

Login credentials are saved in `ops-manager-api-key.json`.

## Project Structure

```
koperator_poc/
├── deploy_ops_manager.py       # Ops Manager deployment script
├── deploy_mongodb_k8s.py       # Kubernetes deployment script
├── docker-compose.ops-manager.yml  # Docker Compose for Ops Manager
├── docker-build/               # Custom Ops Manager Docker image
│   ├── Dockerfile
│   └── entrypoint.sh
├── k8s/                        # Kubernetes manifest templates
│   ├── namespace.yaml          # Operator namespace
│   ├── mongodb-rs-namespace.yaml   # Replica set namespace
│   ├── ops-manager-secret.yaml     # API credentials template
│   ├── ops-manager-configmap.yaml  # Connection config template
│   ├── ops-manager-ca-configmap.yaml   # CA certificate template
│   └── mongodb-replicaset.yaml     # MongoDB ReplicaSet CRD
├── MANUAL_DEPLOYMENT.md        # Manual deployment guide
└── README.md                   # This file
```

## Configuration

### Namespaces

The deployment uses separate namespaces for better isolation:
- `mongodb` - Kubernetes operator
- `mongodb-rs` - MongoDB replica set pods

### Ops Manager Connection

The operator connects to Ops Manager using:
- **projectName** - Deploys to an existing Ops Manager project by name
- **orgId** - Organization ID for the project
- **sslMMSCAConfigMap** - CA certificate for TLS verification

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
# Full deployment with wait
python deploy_mongodb_k8s.py --wait

# Skip preflight checks
python deploy_mongodb_k8s.py --skip-preflight --wait

# Custom cluster name
python deploy_mongodb_k8s.py --cluster-name my-cluster --wait

# Clean up kind cluster
python deploy_mongodb_k8s.py --cleanup
```

## Troubleshooting

### Operator Logs

```bash
kubectl --kubeconfig .kube/config logs -n mongodb \
  -l app.kubernetes.io/name=mongodb-enterprise-operator
```

### MongoDB Pod Logs

```bash
kubectl --kubeconfig .kube/config logs -n mongodb-rs \
  mongodb-rs-0 -c mongodb-agent
```

### Check Replica Set Status

```bash
kubectl --kubeconfig .kube/config get mongodb -n mongodb-rs
kubectl --kubeconfig .kube/config get pods -n mongodb-rs
```

## Documentation

- [Manual Deployment Guide](MANUAL_DEPLOYMENT.md) - Step-by-step manual deployment instructions
- [MongoDB Enterprise Kubernetes Operator Docs](https://www.mongodb.com/docs/kubernetes-operator/stable/)
- [MongoDB Ops Manager Docs](https://www.mongodb.com/docs/ops-manager/current/)

## License

This is a proof-of-concept project for educational and testing purposes.
