# MongoDB Enterprise Kubernetes Manual Deployment Guide

This guide provides step-by-step instructions for manually deploying MongoDB on Kubernetes using the MongoDB Enterprise Kubernetes Operator with Ops Manager.

## Estimated Time

| Step | Duration |
|------|----------|
| Step 1: Create Kubernetes Cluster | 2-3 minutes |
| Step 2: Deploy Operator | 3-5 minutes |
| Step 3: Configure Ops Manager Connection | 2-3 minutes |
| Step 4: Generate TLS Certificates | 1-2 minutes |
| Step 5: Pre-Create NodePort Services | 1 minute |
| Step 6: Deploy MongoDB ReplicaSet | 5-10 minutes |
| Step 7: Verify Deployment | 1-2 minutes |
| **Total** | **15-25 minutes** |

## Prerequisites

Before starting, ensure you have:

1. **Docker** installed and running
2. **kind** (Kubernetes IN Docker) - install from https://kind.sigs.k8s.io/
3. **kubectl** - install from https://kubernetes.io/docs/tasks/tools/
4. **OpenSSL** - for TLS certificate generation
5. **MongoDB Ops Manager** running with HTTPS enabled (deployed separately)
6. **Ops Manager API credentials** (public key, private key, org ID, project ID)
7. **CA certificate** from Ops Manager (`./certs/ca.crt`)

### Verify Prerequisites

Run these commands to verify your environment is ready:

```bash
# Check Docker is running
docker version

# Check kubectl (optional - can use Docker-based kubectl)
kubectl version --client

# Check OpenSSL
openssl version

# Verify Ops Manager is accessible (adjust URL as needed)
curl -k https://localhost:8443/user/login
```

Expected output shows version numbers for each tool. If any command fails, install the missing component before proceeding.

## Directory Structure

```
koperator_poc/
│
├── k8s/                              # Kubernetes YAML templates
│   ├── namespace.yaml                # Operator namespace
│   ├── mongodb-rs-namespace.yaml     # Replica set namespace
│   ├── ops-manager-secret.yaml       # API credentials secret template
│   ├── ops-manager-configmap.yaml    # Ops Manager connection config template
│   ├── ops-manager-ca-configmap.yaml # Ops Manager CA certificate template
│   ├── operator-rbac.yaml            # Operator RBAC template
│   ├── database-roles.yaml           # Database pod service accounts template
│   ├── mongodb-replicaset.yaml       # MongoDB ReplicaSet definition
│   ├── mongodb-user.yaml             # SCRAM user template
│   ├── mongodb-user-secret.yaml      # User password secret template
│   ├── mongodb-x509-user.yaml        # X509 user template
│   └── mongodb-ca-configmap.yaml     # MongoDB CA certificate template
│
├── certs/                            # TLS certificates (you create these)
│   ├── ca.crt                        # CA certificate
│   ├── ca.key                        # CA private key
│   └── mongodb/                      # MongoDB-specific certificates
│       ├── mongodb.crt               # Server certificate
│       ├── mongodb.key               # Server private key
│       ├── client.crt                # Client certificate (X509)
│       ├── client.key                # Client private key
│       └── client.pem                # Combined client cert+key
├── .kube/                            # Kubeconfig directory
│   ├── kind-config.yaml              # Kind cluster configuration
│   └── config                        # Kubeconfig file for kind cluster
└── ops-manager-api-key.json          # Ops Manager credentials (you create this)
```

> **Note**: Files in `k8s/` are templates containing placeholders like `{{VARIABLE}}`.
> You must replace these placeholders with actual values before applying the YAML files.

## Step 1: Create Kubernetes Cluster with kind

### 1.1 Create kind configuration file

Create `.kube/kind-config.yaml` with port mappings for external access:

```yaml
kind: Cluster
apiVersion: kind.x-k8s.io/v1alpha4
name: mongodb-k8s
nodes:
  - role: control-plane
    extraPortMappings:
      # External access ports for MongoDB replica set members
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

### 1.2 Create the cluster

```bash
# Create cluster
kind create cluster --name mongodb-k8s --config .kube/kind-config.yaml --wait 120s

# Export kubeconfig
kind get kubeconfig --name mongodb-k8s > .kube/config

# Verify cluster
kubectl --kubeconfig .kube/config cluster-info
```

**Verification checkpoint:**
```bash
# Expected: Shows cluster running at https://127.0.0.1:xxxxx
kubectl --kubeconfig .kube/config get nodes
# Expected: Shows 2 nodes (control-plane and worker) in Ready state
```

**If this step fails:**
- Check Docker is running: `docker info`
- Check available memory: `docker stats --no-stream`
- Delete failed cluster and retry: `kind delete cluster --name mongodb-k8s`

## Step 2: Deploy MongoDB Enterprise Kubernetes Operator

### 2.1 Create namespaces

Create two namespaces: one for the operator, one for the replica set.

```bash
kubectl --kubeconfig .kube/config create namespace mongodb
kubectl --kubeconfig .kube/config create namespace mongodb-rs
```

Or use the provided templates:

```bash
kubectl --kubeconfig .kube/config apply -f k8s/namespace.yaml
kubectl --kubeconfig .kube/config apply -f k8s/mongodb-rs-namespace.yaml
```

### 2.2 Deploy CRDs (Custom Resource Definitions)

```bash
kubectl --kubeconfig .kube/config apply -f \
  https://raw.githubusercontent.com/mongodb/mongodb-enterprise-kubernetes/master/crds.yaml
```

### 2.3 Deploy the Operator

```bash
kubectl --kubeconfig .kube/config apply -f \
  https://raw.githubusercontent.com/mongodb/mongodb-enterprise-kubernetes/master/mongodb-enterprise.yaml
```

### 2.4 Configure Operator to Watch mongodb-rs Namespace

Since the replica set will be deployed in a separate `mongodb-rs` namespace, configure the operator to watch that namespace:

```bash
# Set WATCH_NAMESPACE environment variable
kubectl --kubeconfig .kube/config set env deployment/mongodb-enterprise-operator \
  -n mongodb WATCH_NAMESPACE=mongodb-rs
```

### 2.5 Create RBAC for Operator in mongodb-rs Namespace

The `k8s/operator-rbac.yaml` template has namespaces hardcoded (`mongodb` for operator, `mongodb-rs` for replica sets). Apply directly:

```bash
kubectl --kubeconfig .kube/config apply -f k8s/operator-rbac.yaml
```

### 2.6 Create Database Roles

The `k8s/database-roles.yaml` template has namespace hardcoded (`mongodb-rs`). Apply directly:

```bash
kubectl --kubeconfig .kube/config apply -f k8s/database-roles.yaml
```

### 2.7 Verify operator deployment

```bash
kubectl --kubeconfig .kube/config get pods -n mongodb

# Wait for operator to be ready
kubectl --kubeconfig .kube/config wait --for=condition=available \
  deployment/mongodb-enterprise-operator -n mongodb --timeout=180s
```

**Verification checkpoint:**
```bash
kubectl --kubeconfig .kube/config get pods -n mongodb
# Expected: mongodb-enterprise-operator-xxxxx in Running state (1/1 Ready)
```

**If operator is not starting:**
- Check operator logs: `kubectl --kubeconfig .kube/config logs -n mongodb -l app.kubernetes.io/name=mongodb-enterprise-operator`
- Verify RBAC is applied: `kubectl --kubeconfig .kube/config get role,rolebinding -n mongodb-rs`

## Step 3: Configure Ops Manager Connection

### 3.1 Create Ops Manager credentials secret

Edit `k8s/ops-manager-secret.yaml` and replace the following placeholders:

| Placeholder | Description |
|------------|-------------|
| `{{PUBLIC_KEY}}` | Your Ops Manager API public key (e.g., "szkhfxdt") |
| `{{PRIVATE_KEY}}` | Your Ops Manager API private key (e.g., "9fdfe594-fcc0-4517-93fd-e51f7dc3ea35") |

You can use sed to replace the placeholders:

```bash
cat k8s/ops-manager-secret.yaml | \
  sed "s/{{PUBLIC_KEY}}/YOUR_PUBLIC_KEY/g; \
       s/{{PRIVATE_KEY}}/YOUR_PRIVATE_KEY/g" | \
  kubectl --kubeconfig .kube/config apply -f -
```

Or edit the file directly and apply:

```bash
kubectl --kubeconfig .kube/config apply -f k8s/ops-manager-secret.yaml
```

### 3.2 Create Ops Manager connection ConfigMap

Edit `k8s/ops-manager-configmap.yaml` and replace the following placeholders:

| Placeholder | Example Value | Description |
|------------|---------------|-------------|
| `{{BASE_URL}}` | `https://host.docker.internal:8443` | Ops Manager URL (use host.docker.internal from kind) |
| `{{PROJECT_NAME}}` | `SingleCluster` | Ops Manager Project Name (must match existing project) |
| `{{PROJECT_ID}}` | `your-project-id` | Ops Manager Project ID (optional, for reference) |
| `{{ORG_ID}}` | `your-org-id` | Ops Manager Organization ID |
| `{{SSL_REQUIRE_VALID_CERTS}}` | `false` | Set to `false` for self-signed certs |

You can use sed to replace the placeholders:

```bash
cat k8s/ops-manager-configmap.yaml | \
  sed "s|{{BASE_URL}}|https://host.docker.internal:8443|g; \
       s/{{PROJECT_NAME}}/SingleCluster/g; \
       s/{{PROJECT_ID}}/YOUR_PROJECT_ID/g; \
       s/{{ORG_ID}}/YOUR_ORG_ID/g; \
       s/{{SSL_REQUIRE_VALID_CERTS}}/false/g" | \
  kubectl --kubeconfig .kube/config apply -f -
```

Or edit the file directly and apply:

```bash
kubectl --kubeconfig .kube/config apply -f k8s/ops-manager-configmap.yaml
```

### 3.3 Create CA certificate ConfigMap (for Ops Manager)

The CA certificate ConfigMap requires the certificate content. You can either:

**Option A: Edit the template directly**

Edit `k8s/ops-manager-ca-configmap.yaml` and replace `{{CA_CERTIFICATE}}` with your CA certificate content (properly indented).

**Option B: Create from file (recommended)**

```bash
kubectl --kubeconfig .kube/config create configmap ops-manager-ca \
  --from-file=mms-ca.crt=./certs/ca.crt \
  -n mongodb-rs
```

> **Important**: The key MUST be named `mms-ca.crt` - this is required by the operator.

## Step 4: Generate TLS Certificates for MongoDB

### 4.1 Generate Server Certificates

Create certificates for MongoDB server with proper SANs:

```bash
mkdir -p certs/mongodb

# Create OpenSSL config for MongoDB server
cat > certs/mongodb/mongodb-ext.cnf << 'EOF'
[req]
distinguished_name = req_distinguished_name
req_extensions = v3_req
prompt = no

[req_distinguished_name]
CN = mongodb-rs

[v3_req]
basicConstraints = CA:FALSE
keyUsage = critical, digitalSignature, keyEncipherment
extendedKeyUsage = serverAuth, clientAuth
subjectAltName = @alt_names

[alt_names]
DNS.1 = mongodb-rs-0.mongodb-rs-svc.mongodb-rs.svc.cluster.local
DNS.2 = mongodb-rs-1.mongodb-rs-svc.mongodb-rs.svc.cluster.local
DNS.3 = mongodb-rs-2.mongodb-rs-svc.mongodb-rs.svc.cluster.local
DNS.4 = mongodb-rs-svc.mongodb-rs.svc.cluster.local
DNS.5 = *.mongodb-rs-svc.mongodb-rs.svc.cluster.local
DNS.6 = localhost
IP.1 = 127.0.0.1
IP.2 = ::1
EOF

# Generate server key
openssl genrsa -out certs/mongodb/mongodb.key 2048

# Generate server CSR
openssl req -new -key certs/mongodb/mongodb.key \
  -out certs/mongodb/mongodb.csr \
  -config certs/mongodb/mongodb-ext.cnf

# Sign with CA
openssl x509 -req -in certs/mongodb/mongodb.csr \
  -CA certs/ca.crt -CAkey certs/ca.key \
  -CAcreateserial -out certs/mongodb/mongodb.crt \
  -days 365 -extensions v3_req \
  -extfile certs/mongodb/mongodb-ext.cnf
```

### 4.2 Generate Client Certificate (for X509 Authentication)

```bash
# Create OpenSSL config for client certificate
cat > certs/mongodb/client-ext.cnf << 'EOF'
[req]
distinguished_name = req_distinguished_name
req_extensions = v3_req
prompt = no

[req_distinguished_name]
O = MongoDB
OU = clients
CN = x509-client

[v3_req]
basicConstraints = CA:FALSE
keyUsage = critical, digitalSignature, keyEncipherment
extendedKeyUsage = clientAuth
EOF

# Generate client key
openssl genrsa -out certs/mongodb/client.key 2048

# Generate client CSR
openssl req -new -key certs/mongodb/client.key \
  -out certs/mongodb/client.csr \
  -config certs/mongodb/client-ext.cnf

# Sign with CA
openssl x509 -req -in certs/mongodb/client.csr \
  -CA certs/ca.crt -CAkey certs/ca.key \
  -CAcreateserial -out certs/mongodb/client.crt \
  -days 365 -extensions v3_req \
  -extfile certs/mongodb/client-ext.cnf

# Create combined PEM file for mongosh
cat certs/mongodb/client.crt certs/mongodb/client.key > certs/mongodb/client.pem
```

### 4.3 Create TLS Secrets in Kubernetes

```bash
# Create MongoDB CA ConfigMap (required key: ca-pem)
kubectl --kubeconfig .kube/config create configmap mongodb-ca \
  --from-file=ca-pem=./certs/ca.crt \
  -n mongodb-rs

# Create server TLS secret
kubectl --kubeconfig .kube/config create secret tls mongodb-mongodb-rs-cert \
  --cert=./certs/mongodb/mongodb.crt \
  --key=./certs/mongodb/mongodb.key \
  -n mongodb-rs

# Create agent TLS secret (same cert for simplicity)
kubectl --kubeconfig .kube/config create secret tls mongodb-mongodb-rs-agent-certs \
  --cert=./certs/mongodb/mongodb.crt \
  --key=./certs/mongodb/mongodb.key \
  -n mongodb-rs
```

## Step 5: Pre-Create NodePort Services with Fixed Ports

**Why pre-create services**: The MongoDB operator reuses existing services if they match the expected naming convention. By pre-creating services with fixed NodePorts, the operator preserves the port assignments instead of assigning random ports.

```bash
kubectl --kubeconfig .kube/config apply -f k8s/nodeport-services.yaml
```

This creates NodePort services with fixed ports (30000, 30001, 30002) matching the Kind cluster's port mappings.

## Step 6: Deploy MongoDB ReplicaSet

### 6.1 Configure the ReplicaSet

The `k8s/mongodb-replicaset.yaml` template has all values hardcoded for consistency. No placeholders need to be replaced.

> **Template Design**: All configuration values including replica set name (`mongodb-rs`),
> member count (`members: 3`), MongoDB version (`7.0.25-ent`), resource limits, storage size,
> ports, and authentication modes are hardcoded in the template. To customize these values,
> edit the template directly.

The following values are defined directly in the template:

| Setting | Default Value | Location in Template |
|---------|---------------|----------------------|
| Replica Set Name | `mongodb-rs` | `metadata.name` |
| Members | `3` | `spec.members` |
| Version | `7.0.25-ent` | `spec.version` |
| External Ports | `30000, 30001, 30002` | `spec.connectivity.replicaSetHorizons` |
| CPU Request | `500m` | `spec.podSpec.podTemplate.spec.containers[].resources` |
| Memory Request | `1Gi` | `spec.podSpec.podTemplate.spec.containers[].resources` |
| Storage | `5Gi` | `spec.podSpec.persistence.single.storage` |
| Auth Modes | `["SCRAM", "X509"]` | `spec.security.authentication.modes` |

### 6.2 Apply the ReplicaSet configuration

```bash
kubectl --kubeconfig .kube/config apply -f k8s/mongodb-replicaset.yaml
```

### 6.3 Create MongoDB Users

#### SCRAM User (Username/Password)

Edit `k8s/mongodb-user-secret.yaml` and replace:
- `{{MONGODB_USER_PASSWORD}}` -> Your desired password

The `k8s/mongodb-user.yaml` template has username (`admin`) and replica set name (`mongodb-rs`) hardcoded.

Apply:

```bash
kubectl --kubeconfig .kube/config apply -f k8s/mongodb-user-secret.yaml
kubectl --kubeconfig .kube/config apply -f k8s/mongodb-user.yaml
```

#### X509 User (Certificate-based)

Edit `k8s/mongodb-x509-user.yaml` and replace:
- `{{X509_USERNAME}}` -> `CN=x509-client,OU=clients,O=MongoDB`
- `{{REPLICA_SET_NAME}}` -> `mongodb-rs`

Apply:

```bash
kubectl --kubeconfig .kube/config apply -f k8s/mongodb-x509-user.yaml
```

### 6.4 Monitor deployment progress

```bash
# Watch MongoDB resource status
kubectl --kubeconfig .kube/config get mongodb -n mongodb-rs -w

# Check pod status
kubectl --kubeconfig .kube/config get pods -n mongodb-rs -w

# View detailed status
kubectl --kubeconfig .kube/config describe mongodb mongodb-rs -n mongodb-rs
```

### 6.5 View logs for troubleshooting

```bash
# Operator logs
kubectl --kubeconfig .kube/config logs -n mongodb \
  -l app.kubernetes.io/name=mongodb-enterprise-operator

# MongoDB pod logs
kubectl --kubeconfig .kube/config logs -n mongodb-rs \
  -l app=mongodb-rs-svc -c mongodb-enterprise-database

# Automation agent logs
kubectl --kubeconfig .kube/config logs -n mongodb-rs \
  -l app=mongodb-rs-svc -c mongodb-agent
```

## Step 7: Verify Deployment

### 7.1 Check MongoDB status

```bash
kubectl --kubeconfig .kube/config get mongodb -n mongodb-rs
```

Expected output when ready:
```
NAME         PHASE     VERSION       TYPE         AGE
mongodb-rs   Running   7.0.25-ent   ReplicaSet   10m
```

### 7.2 Check all pods are running

```bash
kubectl --kubeconfig .kube/config get pods -n mongodb-rs
```

Expected output:
```
NAME           READY   STATUS    RESTARTS   AGE
mongodb-rs-0   2/2     Running   0          10m
mongodb-rs-1   2/2     Running   0          8m
mongodb-rs-2   2/2     Running   0          6m
```

### 7.3 Check external services

```bash
kubectl --kubeconfig .kube/config get svc -n mongodb-rs | grep external
```

Expected output:
```
mongodb-rs-0-svc-external   NodePort   10.96.x.x   <none>   27017:3xxxx/TCP   10m
mongodb-rs-1-svc-external   NodePort   10.96.x.x   <none>   27017:3xxxx/TCP   8m
mongodb-rs-2-svc-external   NodePort   10.96.x.x   <none>   27017:3xxxx/TCP   6m
```

### 7.4 Connect to MongoDB

**Using SCRAM authentication:**

```bash
mongosh "mongodb://localhost:30000,localhost:30001,localhost:30002/?replicaSet=mongodb-rs&tls=true&tlsCAFile=./certs/ca.crt" \
  --username admin \
  --authenticationDatabase admin
```

**Using X509 authentication:**

```bash
mongosh "mongodb://localhost:30000,localhost:30001,localhost:30002/?replicaSet=mongodb-rs&tls=true&tlsCAFile=./certs/ca.crt&authMechanism=MONGODB-X509&authSource=\$external" \
  --tlsCertificateKeyFile ./certs/mongodb/client.pem
```

### 7.5 Verify in Ops Manager

1. Open Ops Manager: https://localhost:8443
2. Navigate to your project
3. You should see the replica set with all members healthy

## Troubleshooting

### Common Issues

#### 1. Pods stuck in Pending state

Check if there are resource constraints:
```bash
kubectl --kubeconfig .kube/config describe pod mongodb-rs-0 -n mongodb-rs
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
kubectl --kubeconfig .kube/config get configmap mongodb-ca -n mongodb-rs -o yaml
```

The key must be `ca-pem`.

#### 4. Binary download failures

If agents can't download MongoDB binaries, ensure Ops Manager is configured for Hybrid Mode:
- `automation.versions.source=hybrid` in Ops Manager config
- Ops Manager must be able to download binaries from MongoDB

#### 5. External connectivity not working

Check that kind port mappings are configured:
```bash
docker port mongodb-k8s-control-plane
```

Should show ports 30000, 30001, 30002 mapped.

### Useful Commands

```bash
# Get events for debugging
kubectl --kubeconfig .kube/config get events -n mongodb-rs --sort-by='.lastTimestamp'

# Exec into a pod for debugging
kubectl --kubeconfig .kube/config exec -it mongodb-rs-0 -n mongodb-rs -c mongodb-enterprise-database -- bash

# Port-forward to MongoDB (fallback if external access not working)
kubectl --kubeconfig .kube/config port-forward -n mongodb-rs mongodb-rs-0 27017:27017

# Delete and recreate (clean start)
kubectl --kubeconfig .kube/config delete mongodb mongodb-rs -n mongodb-rs
kubectl --kubeconfig .kube/config apply -f k8s/mongodb-replicaset.yaml
```

## Cleanup

> **Important**: Follow this order to ensure proper cleanup and avoid orphaned resources in Ops Manager.

### Step 1: Remove MongoDB deployment (wait for Ops Manager deregistration)

```bash
# Delete the MongoDB CRD first - this triggers agent deregistration
kubectl --kubeconfig .kube/config delete -f k8s/mongodb-replicaset.yaml

# Wait for pods to terminate (allows agents to deregister from Ops Manager)
kubectl --kubeconfig .kube/config wait --for=delete pod -l app=mongodb-rs-svc -n mongodb-rs --timeout=180s

# Wait additional time for Ops Manager to register the removal
echo "Waiting 30s for Ops Manager to register agent removal..."
sleep 30
```

### Step 2: Remove all MongoDB resources

```bash
kubectl --kubeconfig .kube/config delete -f k8s/
```

### Step 3: Delete the kind cluster

```bash
kind delete cluster --name mongodb-k8s
```

### Step 4: Clean up Ops Manager project (optional)

If the project shows stale servers in Ops Manager, delete it via the UI or API.

### Verify Cleanup

After running cleanup, verify everything was removed:

```bash
# Verify kind cluster is deleted
kind get clusters
# Should not show "mongodb-k8s"

# Verify Docker containers are removed
docker ps -a | grep mongodb-k8s
# Should return empty

# Verify generated files (optional - remove if you want a fresh start)
ls -la .kube/
ls -la k8s/generated/
ls -la certs/

# Clean generated files for fresh start
rm -rf .kube/ k8s/generated/ certs/
```

## References

- [MongoDB Enterprise Kubernetes Operator Documentation](https://www.mongodb.com/docs/kubernetes-operator/stable/)
- [External Connectivity Guide](https://www.mongodb.com/docs/kubernetes-operator/v1.33/tutorial/connect-from-outside-k8s/)
- [kind Documentation](https://kind.sigs.k8s.io/)
- [MongoDB Ops Manager Documentation](https://www.mongodb.com/docs/ops-manager/current/)
