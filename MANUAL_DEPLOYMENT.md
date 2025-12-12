# MongoDB Enterprise Kubernetes Manual Deployment Guide

This guide provides step-by-step instructions for manually deploying MongoDB on Kubernetes using the MongoDB Enterprise Kubernetes Operator with Ops Manager.

## Prerequisites

Before starting, ensure you have:

1. **Docker** installed and running
2. **kind** (Kubernetes IN Docker) - will be downloaded automatically if not present
3. **kubectl** - native installation or via Docker
4. **OpenSSL** - for TLS certificate generation
5. **MongoDB Ops Manager** running with HTTPS enabled
6. **Ops Manager API credentials** (public key, private key, org ID, project ID)
7. **CA certificate** from Ops Manager (if using self-signed certificates)

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
│   ### Source Files (in version control) ###
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
├── shared/                           # Python utilities module
│   └── ...                           # Shared code for deployment scripts
│
│   ### Runtime-Generated (created by scripts, not in version control) ###
│
├── k8s/generated/                    # Processed YAML files with placeholders filled
├── certs/                            # TLS certificates
│   ├── ca.crt                        # CA certificate
│   ├── ca.key                        # CA private key
│   └── mongodb/                      # MongoDB-specific certificates
│       ├── mongodb.crt               # Server certificate
│       ├── mongodb.key               # Server private key
│       ├── client.crt                # Client certificate (X509)
│       ├── client.key                # Client private key
│       └── client.pem                # Combined client cert+key
├── .kube/                            # Kubeconfig directory
│   ├── kind.exe                      # Kind binary (downloaded if missing)
│   └── config                        # Kubeconfig file for kind cluster
└── ops-manager-api-key.json          # Ops Manager credentials
```

> **Note**: Files in `k8s/` are templates containing placeholders like `{{VARIABLE}}`.
> The deployment script processes these templates and writes the results to `k8s/generated/`.
> Runtime-generated directories (`certs/`, `.kube/`, `k8s/generated/`) are excluded from
> version control via `.gitignore`.

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

### 3.1 Create namespaces

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

Edit `k8s/operator-rbac.yaml` and replace the placeholders:
- `{{RS_NAMESPACE}}` -> `mongodb-rs`
- `{{OPERATOR_NAMESPACE}}` -> `mongodb`

Then apply:

```bash
kubectl --kubeconfig .kube/config apply -f k8s/operator-rbac.yaml
```

### 3.6 Create Database Roles

Edit `k8s/database-roles.yaml` and replace:
- `{{RS_NAMESPACE}}` -> `mongodb-rs`

Then apply:

```bash
kubectl --kubeconfig .kube/config apply -f k8s/database-roles.yaml
```

### 3.7 Verify operator deployment

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
  sslRequireValidMMSServerCertificates: "false" # Set to "true" if using valid certs
```

Apply the ConfigMap:

```bash
kubectl --kubeconfig .kube/config apply -f k8s/ops-manager-configmap.yaml
```

### 4.3 Create CA certificate ConfigMap (for Ops Manager)

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

## Step 5: Generate TLS Certificates for MongoDB

### 5.1 Generate Server Certificates

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

### 5.2 Generate Client Certificate (for X509 Authentication)

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

### 5.3 Create TLS Secrets in Kubernetes

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

## Step 6: Deploy MongoDB ReplicaSet

### 6.1 Configure the ReplicaSet

Edit `k8s/mongodb-replicaset.yaml` and replace the placeholders:

| Placeholder | Example Value | Description |
|------------|---------------|-------------|
| `{{REPLICA_SET_NAME}}` | `mongodb-rs` | Replica set name |
| `{{TLS_REQUIRE_VALID_CERTS}}` | `false` | TLS cert validation for Ops Manager agent |
| `{{SSL_REQUIRE_VALID_MMS_CERTS}}` | `false` | SSL cert validation for agent download |

> **Template Design**: Static configuration values like member count (`members: 3`),
> MongoDB version (`7.0.25-ent`), resource limits, storage size, ports, and authentication
> modes are hardcoded in the template. These values are extracted at runtime by the
> deployment script where needed (e.g., for kind port mappings). To customize these values,
> edit the template directly rather than using placeholders.

The following values are defined directly in the template:

| Setting | Default Value | Location in Template |
|---------|---------------|----------------------|
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

Edit `k8s/mongodb-user.yaml` and replace:
- `{{MONGODB_USERNAME}}` -> `admin`
- `{{REPLICA_SET_NAME}}` -> `mongodb-rs`

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

## Automated Deployment

For automated deployment, use the provided script:

```bash
# Full deployment with all security features
python deploy_mongodb_k8s.py --wait

# Skip TLS certificate validation for Ops Manager (testing only)
python deploy_mongodb_k8s.py --ssl-skip-verify --wait

# Skip pre-flight checks (useful with --ssl-skip-verify)
python deploy_mongodb_k8s.py --ssl-skip-verify --skip-preflight --wait

# With custom cluster options
python deploy_mongodb_k8s.py \
  --cluster-name my-cluster \
  --operator-namespace mongodb \
  --rs-namespace mongodb-rs \
  --worker-nodes 2 \
  --wait

# Cleanup
python deploy_mongodb_k8s.py --cleanup
```

The script will:
1. Create the kind cluster with port mappings (extracted from template)
2. Process YAML templates from `k8s/` and write results to `k8s/generated/`
3. Generate TLS certificates in `certs/`
4. Apply all configurations to the cluster
5. Deploy the MongoDB replica set with authentication and TLS
6. Create SCRAM and X509 users
7. Optionally wait for deployment to complete

> **Template Processing**: The script reads templates from `k8s/` (e.g., `ops-manager-secret.yaml`),
> replaces placeholders like `{{PUBLIC_KEY}}` with actual values, and writes the processed
> files to `k8s/generated/`. When deploying manually, you must either process these templates
> yourself or use the generated files after running the script once.

> **Note**: To customize member count, MongoDB version, ports, or resources,
> edit `k8s/mongodb-replicaset.yaml` directly. The script extracts these values
> from the template at runtime.

## References

- [MongoDB Enterprise Kubernetes Operator Documentation](https://www.mongodb.com/docs/kubernetes-operator/stable/)
- [External Connectivity Guide](https://www.mongodb.com/docs/kubernetes-operator/v1.33/tutorial/connect-from-outside-k8s/)
- [kind Documentation](https://kind.sigs.k8s.io/)
- [MongoDB Ops Manager Documentation](https://www.mongodb.com/docs/ops-manager/current/)
