# MongoDB Enterprise Kubernetes Manual Deployment Guide

This guide provides step-by-step instructions for manually deploying MongoDB on Kubernetes using the MongoDB Enterprise Kubernetes Operator with Ops Manager.

## Prerequisites

Before starting, ensure you have:

1. **Docker** installed and running
2. **kind** (Kubernetes IN Docker) - will be downloaded automatically if not present
3. **kubectl** - native installation or via Docker
4. **MongoDB Ops Manager** running with HTTPS enabled
5. **Ops Manager API credentials** (public key, private key, org ID, project ID)
6. **CA certificate** from Ops Manager (if using self-signed certificates)

## Directory Structure

```
koperator_poc/
├── k8s/                              # Kubernetes YAML templates
│   ├── namespace.yaml                # Namespace definition
│   ├── ops-manager-secret.yaml       # API credentials secret template
│   ├── ops-manager-configmap.yaml    # Ops Manager connection config
│   ├── ops-manager-ca-configmap.yaml # CA certificate for TLS
│   ├── mongodb-replicaset.yaml       # MongoDB ReplicaSet definition
│   └── generated/                    # Auto-generated YAML files (by script)
├── certs/                            # TLS certificates
│   ├── ca.crt                        # CA certificate
│   ├── ca.key                        # CA private key
│   └── server.pem                    # Server certificate + key
├── .kube/                            # Kubeconfig directory
│   └── config                        # Kubeconfig file for kind cluster
└── ops-manager-api-key.json          # Ops Manager credentials (generated)
```

## Step 1: Deploy Ops Manager (if not already running)

If Ops Manager is not already deployed, use the automated script:

```bash
python deploy_ops_manager.py
```

This will:
- Generate TLS certificates in `./certs/`
- Deploy Ops Manager with MongoDB appdb via Docker
- Create initial admin user and API keys
- Save credentials to `ops-manager-api-key.json`

## Step 2: Create Kubernetes Cluster with kind

### 2.1 Create kind configuration file

Create `.kube/kind-config.yaml`:

```yaml
kind: Cluster
apiVersion: kind.x-k8s.io/v1alpha4
name: mongodb-k8s
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
  - role: worker
```

### 2.2 Create the cluster

```bash
# Create cluster
kind create cluster --name mongodb-k8s --config .kube/kind-config.yaml --wait 120s

# Export kubeconfig
kind get kubeconfig --name mongodb-k8s > .kube/config

# Verify cluster
kubectl --kubeconfig .kube/config cluster-info
```

## Step 3: Deploy MongoDB Enterprise Kubernetes Operator

### 3.1 Create the namespace

```bash
kubectl --kubeconfig .kube/config apply -f k8s/namespace.yaml
```

Or manually:

```bash
kubectl --kubeconfig .kube/config create namespace mongodb
```

### 3.2 Deploy CRDs (Custom Resource Definitions)

```bash
kubectl --kubeconfig .kube/config apply -f \
  https://raw.githubusercontent.com/mongodb/mongodb-enterprise-kubernetes/master/crds.yaml
```

### 3.3 Deploy the Operator

```bash
kubectl --kubeconfig .kube/config apply -f \
  https://raw.githubusercontent.com/mongodb/mongodb-enterprise-kubernetes/master/mongodb-enterprise.yaml
```

### 3.4 Configure Operator to Watch mongodb-rs Namespace

Since the replica set will be deployed in a separate `mongodb-rs` namespace, configure the operator to watch that namespace:

```bash
# Set WATCH_NAMESPACE environment variable
kubectl --kubeconfig .kube/config set env deployment/mongodb-enterprise-operator \
  -n mongodb WATCH_NAMESPACE=mongodb-rs
```

### 3.5 Create RBAC for Operator in mongodb-rs Namespace

The operator needs permission to manage resources in the `mongodb-rs` namespace:

```bash
kubectl --kubeconfig .kube/config apply -f - <<EOF
apiVersion: rbac.authorization.k8s.io/v1
kind: Role
metadata:
  name: mongodb-enterprise-operator
  namespace: mongodb-rs
rules:
  - apiGroups: [""]
    resources: [services, secrets, configmaps, pods]
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
  namespace: mongodb-rs
roleRef:
  apiGroup: rbac.authorization.k8s.io
  kind: Role
  name: mongodb-enterprise-operator
subjects:
  - kind: ServiceAccount
    name: mongodb-enterprise-operator
    namespace: mongodb
EOF
```

### 3.6 Verify operator deployment

```bash
kubectl --kubeconfig .kube/config get pods -n mongodb

# Wait for operator to be ready
kubectl --kubeconfig .kube/config wait --for=condition=available \
  deployment/mongodb-enterprise-operator -n mongodb --timeout=180s
```

## Step 4: Configure Ops Manager Connection

### 4.1 Create Ops Manager credentials secret

Edit `k8s/ops-manager-secret.yaml` and replace placeholders:

```yaml
stringData:
  publicKey: "YOUR_PUBLIC_KEY"      # e.g., "szkhfxdt"
  privateKey: "YOUR_PRIVATE_KEY"    # e.g., "9fdfe594-fcc0-4517-93fd-e51f7dc3ea35"
```

Apply the secret:

```bash
kubectl --kubeconfig .kube/config apply -f k8s/ops-manager-secret.yaml
```

### 4.2 Create Ops Manager connection ConfigMap

Edit `k8s/ops-manager-configmap.yaml` and replace placeholders:

```yaml
data:
  baseUrl: "https://host.docker.internal:8443"  # Ops Manager URL from inside kind
  projectName: "Default"                         # Ops Manager Project Name (must match existing project)
  orgId: "YOUR_ORG_ID"                          # Ops Manager Organization ID
```

> **Important**: Use `projectName` with the exact project name as it appears in Ops Manager. If the project doesn't exist, a new one will be created.

Apply the ConfigMap:

```bash
kubectl --kubeconfig .kube/config apply -f k8s/ops-manager-configmap.yaml
```

### 4.3 Create CA certificate ConfigMap

The CA certificate ConfigMap requires the certificate content. You can either:

**Option A: Edit the template directly**

Edit `k8s/ops-manager-ca-configmap.yaml` and replace `{{CA_CERTIFICATE}}` with your CA certificate content (properly indented).

**Option B: Create from file (recommended)**

```bash
kubectl --kubeconfig .kube/config create configmap ops-manager-ca \
  --from-file=mms-ca.crt=./certs/ca.crt \
  -n mongodb
```

> **Important**: The key MUST be named `mms-ca.crt` - this is required by the operator.

## Step 5: Deploy MongoDB ReplicaSet

### 5.1 Configure the ReplicaSet

Edit `k8s/mongodb-replicaset.yaml` and update as needed:

```yaml
metadata:
  name: mongodb-rs           # Replica set name
spec:
  members: 3                 # Number of members (use odd numbers)
  version: "7.0.5-ent"       # MongoDB version
```

### 5.2 Apply the ReplicaSet configuration

```bash
kubectl --kubeconfig .kube/config apply -f k8s/mongodb-replicaset.yaml
```

### 5.3 Monitor deployment progress

```bash
# Watch MongoDB resource status
kubectl --kubeconfig .kube/config get mongodb -n mongodb -w

# Check pod status
kubectl --kubeconfig .kube/config get pods -n mongodb -w

# View detailed status
kubectl --kubeconfig .kube/config describe mongodb mongodb-rs -n mongodb
```

### 5.4 View logs for troubleshooting

```bash
# Operator logs
kubectl --kubeconfig .kube/config logs -n mongodb \
  -l app.kubernetes.io/name=mongodb-enterprise-operator

# MongoDB pod logs
kubectl --kubeconfig .kube/config logs -n mongodb \
  -l app=mongodb-rs-svc -c mongodb-enterprise-database

# Automation agent logs
kubectl --kubeconfig .kube/config logs -n mongodb \
  -l app=mongodb-rs-svc -c mongodb-agent
```

## Step 6: Verify Deployment

### 6.1 Check MongoDB status

```bash
kubectl --kubeconfig .kube/config get mongodb -n mongodb
```

Expected output when ready:
```
NAME         PHASE     VERSION     TYPE         AGE
mongodb-rs   Running   7.0.5-ent   ReplicaSet   10m
```

### 6.2 Check all pods are running

```bash
kubectl --kubeconfig .kube/config get pods -n mongodb
```

Expected output:
```
NAME                                           READY   STATUS    RESTARTS   AGE
mongodb-enterprise-operator-xxxxx              1/1     Running   0          15m
mongodb-rs-0                                   2/2     Running   0          10m
mongodb-rs-1                                   2/2     Running   0          8m
mongodb-rs-2                                   2/2     Running   0          6m
```

### 6.3 Verify in Ops Manager

1. Open Ops Manager: https://localhost:8443
2. Navigate to your project
3. You should see the replica set with all members healthy

## Troubleshooting

### Common Issues

#### 1. Pods stuck in Pending state

Check if there are resource constraints:
```bash
kubectl --kubeconfig .kube/config describe pod mongodb-rs-0 -n mongodb
```

#### 2. Agent cannot connect to Ops Manager

Verify the Ops Manager URL is accessible from inside the cluster:
```bash
kubectl --kubeconfig .kube/config run -it --rm debug \
  --image=curlimages/curl --restart=Never -- \
  curl -k https://host.docker.internal:8443/user/login
```

#### 3. TLS certificate errors

Ensure the CA certificate is correctly configured:
```bash
kubectl --kubeconfig .kube/config get configmap ops-manager-ca -n mongodb -o yaml
```

The key must be `mms-ca.crt`.

#### 4. Binary download failures

If agents can't download MongoDB binaries, ensure Ops Manager is configured for Hybrid Mode:
- `automation.versions.source=hybrid` in Ops Manager config
- Ops Manager must be able to download binaries from MongoDB

### Useful Commands

```bash
# Get events for debugging
kubectl --kubeconfig .kube/config get events -n mongodb --sort-by='.lastTimestamp'

# Exec into a pod for debugging
kubectl --kubeconfig .kube/config exec -it mongodb-rs-0 -n mongodb -c mongodb-enterprise-database -- bash

# Port-forward to MongoDB
kubectl --kubeconfig .kube/config port-forward -n mongodb svc/mongodb-rs-svc 27017:27017

# Delete and recreate (clean start)
kubectl --kubeconfig .kube/config delete mongodb mongodb-rs -n mongodb
kubectl --kubeconfig .kube/config apply -f k8s/mongodb-replicaset.yaml
```

## Cleanup

### Remove MongoDB deployment

```bash
kubectl --kubeconfig .kube/config delete -f k8s/mongodb-replicaset.yaml
```

### Remove all MongoDB resources

```bash
kubectl --kubeconfig .kube/config delete -f k8s/
```

### Delete the kind cluster

```bash
kind delete cluster --name mongodb-k8s
```

## Automated Deployment

For automated deployment, use the provided script:

```bash
# Full deployment
python deploy_mongodb_k8s.py

# With custom options
python deploy_mongodb_k8s.py \
  --cluster-name my-cluster \
  --namespace mongodb \
  --replica-set-members 3 \
  --wait

# Cleanup
python deploy_mongodb_k8s.py --cleanup
```

The script will:
1. Create the kind cluster
2. Generate YAML files from templates in `k8s/` to `k8s/generated/`
3. Apply all configurations
4. Deploy the MongoDB replica set
5. Optionally wait for deployment to complete

## References

- [MongoDB Enterprise Kubernetes Operator Documentation](https://www.mongodb.com/docs/kubernetes-operator/stable/)
- [kind Documentation](https://kind.sigs.k8s.io/)
- [MongoDB Ops Manager Documentation](https://www.mongodb.com/docs/ops-manager/current/)
