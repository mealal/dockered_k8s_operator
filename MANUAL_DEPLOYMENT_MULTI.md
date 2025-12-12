# MongoDB Enterprise Kubernetes Operator - Multi-Cluster Manual Deployment Guide

This guide provides step-by-step instructions for manually deploying MongoDB across multiple Kubernetes clusters using the MongoDB Enterprise Kubernetes Operator.

## Architecture Overview

```
┌───────────────────────────────────────────┐     ┌───────────────────────────────────────┐
│           CENTRAL CLUSTER                 │     │           MEMBER CLUSTER              │
│           (mongodb-central)               │     │           (mongodb-member-1)          │
│                                           │     │                                       │
│  ┌─────────────────────────────────┐      │     │                                       │
│  │  mongodb namespace              │      │     │                                       │
│  │  ┌─────────────────────────┐    │      │     │                                       │
│  │  │ MongoDB Operator        │────┼──────┼─────┼────────────────────────────┐          │
│  │  │ (watches both clusters) │    │      │     │                            │          │
│  │  └─────────────────────────┘    │      │     │                            │          │
│  └─────────────────────────────────┘      │     │                            │          │
│                                           │     │                            │          │
│  ┌─────────────────────────────────┐      │     │  ┌─────────────────────────▼──────┐   │
│  │  mongodb-rs namespace           │      │     │  │  mongodb-rs namespace          │   │
│  │  ┌─────────┐┌─────────┐┌───────┐│      │     │  │  ┌─────────┐ ┌─────────┐       │   │
│  │  │MongoDB-0││MongoDB-1││Mongo-2││      │     │  │  │MongoDB-3│ │MongoDB-4│       │   │
│  │  │(Primary)││(Second.)││(Sec.) ││      │     │  │  │(Second.)│ │(Second.)│       │   │
│  │  └────┬────┘└────┬────┘└───┬───┘│      │     │  │  └────┬────┘ └────┬────┘       │   │
│  └───────┼──────────┼─────────┼────┘      │     │  └───────┼──────────┼─────────────┘   │
│          │          │         │           │     │          │          │                 │
│       :30100     :30101    :30102         │     │       :30200     :30201               │
└──────────┴──────────┴─────────┴───────────┘     └──────────┴──────────┴─────────────────┘
           │          │         │                            │          │
           └──────────┴─────────┴────────────────────────────┴──────────┘
                          Cross-cluster replication (5-node replica set)
                          via external domains
```

## Prerequisites

1. **Docker** running on your machine
2. **Ops Manager** deployed and accessible (see `deploy_ops_manager.py`)
3. **API credentials** from Ops Manager (`ops-manager-api-key.json`)
4. **CA certificate** for TLS (`./certs/ca.crt`)
5. **kind** (Kubernetes IN Docker) - downloaded automatically or install manually
6. **kubectl** - downloaded automatically via Docker or install locally

### Verify Prerequisites

Run these commands to verify your environment is ready:

```bash
# Check Docker is running
docker version

# Check kubectl (optional - can use Docker-based kubectl)
kubectl version --client

# Check OpenSSL
openssl version

# Verify Ops Manager is accessible
curl -k https://localhost:8443/user/login

# Verify API credentials file exists
cat ops-manager-api-key.json

# Verify CA certificate exists
ls -la ./certs/ca.crt
```

Expected output shows version numbers for each tool and confirms credential files exist.

## Quick Start (Automated)

Use the automated script for the fastest deployment:

```bash
# Full deployment with wait
python deploy_mongodb_k8s_multi.py --wait

# Custom cluster names
python deploy_mongodb_k8s_multi.py --central-cluster-name my-central --member-cluster-name my-member

# With SSL verification disabled (testing only)
python deploy_mongodb_k8s_multi.py --ssl-skip-verify --wait

# Cleanup all clusters
python deploy_mongodb_k8s_multi.py --cleanup
```

## Cross-Cluster DNS with CoreDNS

A critical challenge in multi-cluster MongoDB deployments is DNS resolution. Each MongoDB pod needs to resolve hostnames for pods in both local and remote clusters for replica set communication.

### The DNS Problem

MongoDB replica set members communicate using hostnames defined in `replicaSetHorizons`. In a multi-cluster setup:
- Pods in the **central cluster** need to resolve hostnames for pods in the **member cluster**
- Pods in the **member cluster** need to resolve hostnames for pods in the **central cluster**
- Standard Kubernetes DNS only resolves local cluster services

### The Solution: CoreDNS Rewrite Rules + Static Hosts

The deployment script configures CoreDNS in each cluster with two strategies:

#### 1. Local Pods: DNS Rewrite Rules

For pods in the **same cluster**, use CoreDNS `rewrite` rules to redirect external domain queries to internal Kubernetes DNS:

```
# In central cluster CoreDNS:
rewrite name mongodb-multi-rs-0-0.central.mongodb.local mongodb-multi-rs-0-0.mongodb-multi-rs-svc.mongodb-rs.svc.cluster.local
rewrite name mongodb-multi-rs-0-1.central.mongodb.local mongodb-multi-rs-0-1.mongodb-multi-rs-svc.mongodb-rs.svc.cluster.local
```

This ensures that when pods restart and get new IPs, DNS automatically resolves to the correct address.

#### 2. Remote Pods: Static Hosts + NodePort Routing

For pods in **remote clusters**, use static `hosts` entries pointing to virtual IPs that are routed via iptables to NodePort services:

```
# In central cluster CoreDNS (for member cluster pods):
hosts {
    172.19.0.100 mongodb-multi-rs-1-0.member1.mongodb.local
    172.19.0.101 mongodb-multi-rs-1-1.member1.mongodb.local
    fallthrough
}
```

The virtual IPs (172.19.0.x) are routed through iptables DNAT rules to the member cluster's NodePort:

```bash
# On central cluster node:
iptables -t nat -A OUTPUT -d 172.19.0.100 -p tcp --dport 27017 -j DNAT --to-destination <member-node-ip>:30200
```

### DNS Architecture Diagram

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                           CENTRAL CLUSTER                                    │
│                                                                              │
│  ┌─────────────────────────────────────────────────────────────────────┐    │
│  │                         CoreDNS                                      │    │
│  │  ┌─────────────────────────────────────────────────────────────┐    │    │
│  │  │ REWRITE (local pods):                                        │    │    │
│  │  │   *.central.mongodb.local → *.mongodb-rs.svc.cluster.local  │    │    │
│  │  └─────────────────────────────────────────────────────────────┘    │    │
│  │  ┌─────────────────────────────────────────────────────────────┐    │    │
│  │  │ HOSTS (remote pods):                                         │    │    │
│  │  │   172.19.0.100 → mongodb-multi-rs-1-0.member1.mongodb.local │    │    │
│  │  │   172.19.0.101 → mongodb-multi-rs-1-1.member1.mongodb.local │    │    │
│  │  └─────────────────────────────────────────────────────────────┘    │    │
│  └─────────────────────────────────────────────────────────────────────┘    │
│                                    │                                         │
│                          iptables DNAT                                       │
│                    172.19.0.100:27017 → member-ip:30200                     │
│                    172.19.0.101:27017 → member-ip:30201                     │
│                                    │                                         │
└────────────────────────────────────┼────────────────────────────────────────┘
                                     │
                              Docker Network
                                     │
┌────────────────────────────────────┼────────────────────────────────────────┐
│                           MEMBER CLUSTER                                     │
│                                    │                                         │
│                          NodePort Services                                   │
│                         :30200 → mongodb-0                                   │
│                         :30201 → mongodb-1                                   │
└─────────────────────────────────────────────────────────────────────────────┘
```

### Troubleshooting DNS

```bash
# Test DNS resolution from a pod in central cluster
kubectl exec -it mongodb-multi-rs-0-0 -n mongodb-rs -c mongodb-enterprise-database -- \
  getent hosts mongodb-multi-rs-1-0.member1.mongodb.local

# Check CoreDNS configuration
kubectl get configmap coredns -n kube-system -o yaml

# Check iptables rules on the kind node
docker exec mongodb-central-control-plane iptables -t nat -L OUTPUT -n -v

# Test connectivity to remote cluster pod
kubectl exec -it mongodb-multi-rs-0-0 -n mongodb-rs -c mongodb-enterprise-database -- \
  curl -v telnet://mongodb-multi-rs-1-0.member1.mongodb.local:27017
```

## Manual Deployment Steps

### Step 1: Create Kind Clusters

Create two kind clusters - one central and one member:

```bash
# Create central cluster configuration (3 MongoDB nodes)
cat > central-kind-config.yaml << EOF
kind: Cluster
apiVersion: kind.x-k8s.io/v1alpha4
name: mongodb-central
nodes:
  - role: control-plane
    extraPortMappings:
      - containerPort: 30100
        hostPort: 30100
        protocol: TCP
      - containerPort: 30101
        hostPort: 30101
        protocol: TCP
      - containerPort: 30102
        hostPort: 30102
        protocol: TCP
EOF

# Create member cluster configuration (2 MongoDB nodes)
cat > member-kind-config.yaml << EOF
kind: Cluster
apiVersion: kind.x-k8s.io/v1alpha4
name: mongodb-member-1
nodes:
  - role: control-plane
    extraPortMappings:
      - containerPort: 30200
        hostPort: 30200
        protocol: TCP
      - containerPort: 30201
        hostPort: 30201
        protocol: TCP
EOF

# Create clusters
kind create cluster --config central-kind-config.yaml --wait 120s
kind create cluster --config member-kind-config.yaml --wait 120s

# Export kubeconfigs
kind get kubeconfig --name mongodb-central > central-config
kind get kubeconfig --name mongodb-member-1 > member-config
```

### Step 2: Deploy Operator to Central Cluster

```bash
# Set kubeconfig for central cluster
export KUBECONFIG=./central-config

# Create namespaces
kubectl create namespace mongodb
kubectl create namespace mongodb-rs

# Deploy CRDs
kubectl apply -f https://raw.githubusercontent.com/mongodb/mongodb-enterprise-kubernetes/master/crds.yaml

# Deploy operator
kubectl apply -f https://raw.githubusercontent.com/mongodb/mongodb-enterprise-kubernetes/master/mongodb-enterprise.yaml

# Configure operator to watch mongodb-rs namespace
kubectl set env deployment/mongodb-enterprise-operator -n mongodb WATCH_NAMESPACE=mongodb-rs

# Wait for operator to be ready
kubectl wait --for=condition=available deployment/mongodb-enterprise-operator -n mongodb --timeout=180s
```

### Step 3: Deploy RBAC on Central Cluster

```bash
export KUBECONFIG=./central-config

# Apply operator RBAC for mongodb-rs namespace
kubectl apply -f k8s-multi/generated/operator-rbac.yaml

# Apply database roles
kubectl apply -f k8s-multi/generated/database-roles.yaml
```

### Step 4: Prepare Member Cluster

```bash
# Switch to member cluster
export KUBECONFIG=./member-config

# Create namespace
kubectl create namespace mongodb-rs

# Apply member cluster RBAC
kubectl apply -f k8s-multi/generated/member-cluster-rbac.yaml

# Apply database roles
kubectl apply -f k8s-multi/generated/database-roles.yaml
```

### Step 5: Create Multi-Cluster Kubeconfig Secret

The operator needs a kubeconfig with access to both clusters:

```bash
# Switch back to central cluster
export KUBECONFIG=./central-config

# Create combined kubeconfig (the script does this automatically)
# For manual setup, merge both kubeconfigs and create secret:
kubectl create secret generic mongodb-enterprise-operator-multi-cluster-kubeconfig \
  -n mongodb \
  --from-file=kubeconfig=./combined-kubeconfig.yaml
```

### Step 6: Create Member List ConfigMap

```bash
export KUBECONFIG=./central-config

kubectl apply -f - << EOF
apiVersion: v1
kind: ConfigMap
metadata:
  name: mongodb-enterprise-operator-member-list
  namespace: mongodb
data:
  member-clusters: "mongodb-central,mongodb-member-1"
EOF
```

### Step 7: Create Ops Manager Credentials

```bash
export KUBECONFIG=./central-config

# Apply generated secret (contains your API keys)
kubectl apply -f k8s-multi/generated/ops-manager-secret.yaml

# Apply ConfigMap with connection details
kubectl apply -f k8s-multi/generated/ops-manager-configmap.yaml

# Apply CA certificate ConfigMap (if using self-signed certs)
kubectl apply -f k8s-multi/generated/ops-manager-ca-configmap.yaml
```

### Step 8: Create TLS Certificates

Generate and deploy TLS certificates to both clusters:

```bash
# Generate certificates (done by script, or manually with openssl)
# The certificates need SANs for all MongoDB pod hostnames and external domains

# Apply CA ConfigMap to central cluster
export KUBECONFIG=./central-config
kubectl apply -f k8s-multi/generated/mongodb-ca-configmap.yaml

# Create TLS secrets on central cluster
kubectl create secret tls mongodb-mongodb-multi-rs-cert \
  --cert=./certs/mongodb-multi/mongodb.crt \
  --key=./certs/mongodb-multi/mongodb.key \
  -n mongodb-rs

kubectl create secret tls mongodb-mongodb-multi-rs-agent-certs \
  --cert=./certs/mongodb-multi/mongodb.crt \
  --key=./certs/mongodb-multi/mongodb.key \
  -n mongodb-rs

# Apply to member cluster
export KUBECONFIG=./member-config
kubectl apply -f k8s-multi/generated/mongodb-ca-configmap.yaml

kubectl create secret tls mongodb-mongodb-multi-rs-cert \
  --cert=./certs/mongodb-multi/mongodb.crt \
  --key=./certs/mongodb-multi/mongodb.key \
  -n mongodb-rs

kubectl create secret tls mongodb-mongodb-multi-rs-agent-certs \
  --cert=./certs/mongodb-multi/mongodb.crt \
  --key=./certs/mongodb-multi/mongodb.key \
  -n mongodb-rs
```

### Step 9: Deploy MongoDBMultiCluster Resource

```bash
export KUBECONFIG=./central-config

# Apply the MongoDBMultiCluster resource
kubectl apply -f k8s-multi/generated/mongodb-multicluster.yaml

# Watch the deployment progress
kubectl get mongodbmulticluster -n mongodb-rs -w
```

### Step 10: Create MongoDB User (Optional)

```bash
export KUBECONFIG=./central-config

# Create password secret
kubectl apply -f k8s-multi/generated/mongodb-user-secret.yaml

# Create MongoDB user
kubectl apply -f k8s-multi/generated/mongodb-user.yaml
```

## Monitoring Deployment

### Check MongoDBMultiCluster Status

```bash
# Central cluster
export KUBECONFIG=./central-config

kubectl get mongodbmulticluster -n mongodb-rs
kubectl describe mongodbmulticluster mongodb-multi-rs -n mongodb-rs
```

### Check Pods on Both Clusters

```bash
# Central cluster pods
export KUBECONFIG=./central-config
kubectl get pods -n mongodb-rs

# Member cluster pods
export KUBECONFIG=./member-config
kubectl get pods -n mongodb-rs
```

### Check Operator Logs

```bash
export KUBECONFIG=./central-config
kubectl logs -n mongodb -l app.kubernetes.io/name=mongodb-enterprise-operator --tail=100 -f
```

## Connection Instructions

Once the deployment is running, connect to MongoDB:

### Using mongosh with SCRAM Authentication

```bash
# Connect to 5-node replica set (3 on central + 2 on member cluster)
mongosh "mongodb://localhost:30100,localhost:30101,localhost:30102,localhost:30200,localhost:30201/?replicaSet=mongodb-multi-rs&tls=true&tlsCAFile=./certs/ca.crt" \
  --username admin \
  --authenticationDatabase admin
```

### Using mongosh with TLS only

```bash
mongosh "mongodb://localhost:30100,localhost:30101,localhost:30102,localhost:30200,localhost:30201/?replicaSet=mongodb-multi-rs&tls=true&tlsCAFile=./certs/ca.crt&tlsAllowInvalidHostnames=true"
```

## Troubleshooting

### Pods not starting on member cluster

1. Check if RBAC is properly configured:
   ```bash
   export KUBECONFIG=./member-config
   kubectl get serviceaccount -n mongodb-rs
   kubectl get role,rolebinding -n mongodb-rs
   ```

2. Check if TLS secrets exist:
   ```bash
   kubectl get secrets -n mongodb-rs | grep mongodb
   ```

### Operator cannot connect to member cluster

1. Verify kubeconfig secret:
   ```bash
   export KUBECONFIG=./central-config
   kubectl get secret mongodb-enterprise-operator-multi-cluster-kubeconfig -n mongodb
   ```

2. Check operator logs for connection errors:
   ```bash
   kubectl logs -n mongodb -l app.kubernetes.io/name=mongodb-enterprise-operator | grep -i error
   ```

### MongoDB pods in CrashLoopBackOff

1. Check pod logs:
   ```bash
   kubectl logs <pod-name> -n mongodb-rs -c mongodb-enterprise-database
   ```

2. Check agent logs:
   ```bash
   kubectl logs <pod-name> -n mongodb-rs -c mongodb-agent
   ```

3. Verify Ops Manager connectivity:
   ```bash
   kubectl exec -it <pod-name> -n mongodb-rs -c mongodb-enterprise-database -- \
     curl -k https://host.docker.internal:8443/user/login
   ```

## Cleanup

```bash
# Delete both clusters
kind delete cluster --name mongodb-central
kind delete cluster --name mongodb-member-1

# Or use the script
python deploy_mongodb_k8s_multi.py --cleanup
```

### Verify Cleanup

After running cleanup, verify everything was removed:

```bash
# Verify kind clusters are deleted
kind get clusters
# Should not show "mongodb-central" or "mongodb-member-1"

# Verify Docker containers are removed
docker ps -a | grep mongodb-
# Should return empty (for kind containers)

# Verify generated files (optional - remove if you want a fresh start)
ls -la .kube-multi/
ls -la k8s-multi/generated/
ls -la certs/

# Clean generated files for fresh start
rm -rf .kube-multi/ k8s-multi/generated/ certs/
```

## File Reference

### Template Files (in `k8s-multi/`)

These are source templates with placeholders like `{{VARIABLE}}` that get processed by the deployment script:

| Template File | Description | Apply To |
|---------------|-------------|----------|
| `namespace.yaml` | Operator namespace | Central |
| `mongodb-rs-namespace.yaml` | ReplicaSet namespace | Both |
| `mongodb-multicluster.yaml` | MongoDBMultiCluster resource template | Central |
| `ops-manager-secret.yaml` | API credentials template | Central |
| `ops-manager-configmap.yaml` | Connection config template | Central |
| `ops-manager-ca-configmap.yaml` | CA certificate template | Central |
| `operator-rbac.yaml` | Operator RBAC template | Central |
| `database-roles.yaml` | Database pod roles template | Both |
| `member-cluster-rbac.yaml` | RBAC for member clusters template | Member |
| `kubeconfig-template.yaml` | Multi-cluster kubeconfig template | Central |
| `member-list-configmap.yaml` | Cluster list template | Central |
| `mongodb-ca-configmap.yaml` | MongoDB CA certificate template | Both |
| `mongodb-user-secret.yaml` | User password template | Central |
| `mongodb-user.yaml` | MongoDB user template | Central |
| `coredns-configmap.yaml` | CoreDNS configuration template | Both |

### Generated Files (in `k8s-multi/generated/`)

These files are created at runtime by the deployment script with placeholders replaced:

| Generated File | Source Template |
|----------------|-----------------|
| `ops-manager-secret.yaml` | Filled with API keys from `ops-manager-api-key.json` |
| `ops-manager-configmap.yaml` | Filled with Ops Manager URL, org ID, project name |
| `mongodb-ca-configmap.yaml` | Filled with CA certificate content |
| `kubeconfig-secret.yaml` | Filled with merged kubeconfig for both clusters |
| `member-list-configmap.yaml` | Filled with cluster names |
| `mongodb-multicluster.yaml` | Filled with replica set name, TLS settings |

> **Note**: The `k8s-multi/generated/` directory is created by the script and excluded
> from version control. If running manually, you must process the templates yourself
> or use the generated files after running the script once.

## References

- [MongoDB Multi-Cluster Overview](https://www.mongodb.com/docs/kubernetes-operator/v1.33/multi-cluster-overview/)
- [Deploy Multi-Cluster Without Service Mesh](https://www.mongodb.com/docs/kubernetes-operator/v1.33/multi-cluster-no-service-mesh-deploy-rs/)
- [Multi-Cluster Prerequisites](https://www.mongodb.com/docs/kubernetes-operator/v1.33/multi-cluster-prerequisites/)
- [MongoDBMultiCluster CRD Specification](https://www.mongodb.com/docs/kubernetes-operator/v1.33/reference/k8s-operator-multi-cluster-specification/)
