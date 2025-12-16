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

```bash
# Check Docker is running
docker version

# Check kubectl
kubectl version --client

# Check OpenSSL
openssl version

# Verify Ops Manager is accessible
curl -k https://localhost:8443/user/login
```

---

## Step 1: Create Kubernetes Cluster with kind

### 1.1 Create kind configuration file

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

### 1.2 Create the cluster

```bash
# Create cluster
kind create cluster --name mongodb-k8s --config .kube/kind-config.yaml --wait 120s

# Export kubeconfig
kind get kubeconfig --name mongodb-k8s > .kube/config

# Verify cluster
kubectl --kubeconfig .kube/config cluster-info
kubectl --kubeconfig .kube/config get nodes
```

---

## Step 2: Deploy MongoDB Enterprise Kubernetes Operator

### 2.1 Create Operator Namespace

Save as `namespace.yaml` and apply:

```yaml
apiVersion: v1
kind: Namespace
metadata:
  name: mongodb
  labels:
    app.kubernetes.io/name: mongodb-operator
    app.kubernetes.io/component: operator-namespace
```

```bash
kubectl --kubeconfig .kube/config apply -f namespace.yaml
```

### 2.2 Create Replica Set Namespace

Save as `mongodb-rs-namespace.yaml` and apply:

```yaml
apiVersion: v1
kind: Namespace
metadata:
  name: eksrsoppoc1d
  labels:
    app.kubernetes.io/name: mongodb-replicaset
    app.kubernetes.io/component: database-namespace
```

```bash
kubectl --kubeconfig .kube/config apply -f mongodb-rs-namespace.yaml
```

### 2.3 Deploy CRDs

```bash
kubectl --kubeconfig .kube/config apply -f \
  https://raw.githubusercontent.com/mongodb/mongodb-enterprise-kubernetes/master/crds.yaml
```

### 2.4 Deploy the Operator

```bash
kubectl --kubeconfig .kube/config apply -f \
  https://raw.githubusercontent.com/mongodb/mongodb-enterprise-kubernetes/master/mongodb-enterprise.yaml
```

### 2.5 Configure Operator to Watch eksrsoppoc1d Namespace

```bash
kubectl --kubeconfig .kube/config set env deployment/mongodb-enterprise-operator \
  -n mongodb WATCH_NAMESPACE=eksrsoppoc1d
```

### 2.6 Create RBAC for Operator

Save as `operator-rbac.yaml` and apply:

```yaml
apiVersion: rbac.authorization.k8s.io/v1
kind: Role
metadata:
  name: mongodb-enterprise-operator
  namespace: eksrsoppoc1d
rules:
  - apiGroups: [""]
    resources: [services]
    verbs: [get, list, create, update, delete, watch]
  - apiGroups: [""]
    resources: [secrets]
    verbs: [get, list, create, update, delete, watch]
  - apiGroups: [""]
    resources: [configmaps]
    verbs: [get, list, create, update, delete, watch]
  - apiGroups: [""]
    resources: [pods]
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
  namespace: eksrsoppoc1d
roleRef:
  apiGroup: rbac.authorization.k8s.io
  kind: Role
  name: mongodb-enterprise-operator
subjects:
  - kind: ServiceAccount
    name: mongodb-enterprise-operator
    namespace: mongodb
```

```bash
kubectl --kubeconfig .kube/config apply -f operator-rbac.yaml
```

### 2.7 Create Database Roles

Save as `database-roles.yaml` and apply:

```yaml
apiVersion: v1
kind: ServiceAccount
metadata:
  name: mongodb-enterprise-database-pods
  namespace: eksrsoppoc1d
---
apiVersion: v1
kind: ServiceAccount
metadata:
  name: mongodb-enterprise-appdb
  namespace: eksrsoppoc1d
---
apiVersion: rbac.authorization.k8s.io/v1
kind: Role
metadata:
  name: mongodb-enterprise-database-pods
  namespace: eksrsoppoc1d
rules:
  - apiGroups: [""]
    resources: [secrets]
    verbs: [get]
  - apiGroups: [""]
    resources: [pods]
    verbs: [patch, delete, get]
---
apiVersion: rbac.authorization.k8s.io/v1
kind: RoleBinding
metadata:
  name: mongodb-enterprise-database-pods
  namespace: eksrsoppoc1d
roleRef:
  apiGroup: rbac.authorization.k8s.io
  kind: Role
  name: mongodb-enterprise-database-pods
subjects:
  - kind: ServiceAccount
    name: mongodb-enterprise-database-pods
    namespace: eksrsoppoc1d
```

```bash
kubectl --kubeconfig .kube/config apply -f database-roles.yaml
```

### 2.8 Verify Operator Deployment

```bash
kubectl --kubeconfig .kube/config wait --for=condition=available \
  deployment/mongodb-enterprise-operator -n mongodb --timeout=180s

kubectl --kubeconfig .kube/config get pods -n mongodb
```

---

## Step 3: Configure Ops Manager Connection

### 3.1 Create Ops Manager Credentials Secret

Save as `ops-manager-secret.yaml` and apply (replace placeholders):

```yaml
apiVersion: v1
kind: Secret
metadata:
  name: ops-manager-admin-key
  namespace: eksrsoppoc1d
stringData:
  # Replace with your Ops Manager API public key
  publicKey: "YOUR_PUBLIC_KEY"
  # Replace with your Ops Manager API private key
  privateKey: "YOUR_PRIVATE_KEY"
```

```bash
kubectl --kubeconfig .kube/config apply -f ops-manager-secret.yaml
```

### 3.2 Create Ops Manager Connection ConfigMap

Save as `ops-manager-configmap.yaml` and apply (replace placeholders):

```yaml
apiVersion: v1
kind: ConfigMap
metadata:
  name: ops-manager-connection
  namespace: eksrsoppoc1d
data:
  # Ops Manager URL (use host.docker.internal from kind cluster)
  baseUrl: "https://host.docker.internal:8443"
  # Your Ops Manager project name
  projectName: "SingleCluster"
  # Your Ops Manager project ID
  projectId: "YOUR_PROJECT_ID"
  # Your Ops Manager organization ID
  orgId: "YOUR_ORG_ID"
  # ConfigMap containing CA certificate
  sslMMSCAConfigMap: "ops-manager-ca"
  # Set to false for self-signed certs (testing only)
  sslRequireValidMMSServerCertificates: "false"
```

```bash
kubectl --kubeconfig .kube/config apply -f ops-manager-configmap.yaml
```

### 3.3 Create CA Certificate ConfigMap

**Option A: Create from file (recommended)**

```bash
kubectl --kubeconfig .kube/config create configmap ops-manager-ca \
  --from-file=mms-ca.crt=./certs/ca.crt \
  -n eksrsoppoc1d
```

**Option B: Save as `ops-manager-ca-configmap.yaml`**

```yaml
apiVersion: v1
kind: ConfigMap
metadata:
  name: ops-manager-ca
  namespace: eksrsoppoc1d
data:
  # IMPORTANT: Key must be 'mms-ca.crt'
  # Paste your CA certificate content here (PEM format)
  mms-ca.crt: |
    -----BEGIN CERTIFICATE-----
    YOUR_CA_CERTIFICATE_CONTENT_HERE
    -----END CERTIFICATE-----
```

```bash
kubectl --kubeconfig .kube/config apply -f ops-manager-ca-configmap.yaml
```

---

## Step 4: Generate TLS Certificates for MongoDB

### 4.1 Generate Server Certificates

```bash
mkdir -p certs/mongodb

# Create OpenSSL config
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
DNS.1 = mongodb-rs-0.mongodb-rs-svc.eksrsoppoc1d.svc.cluster.local
DNS.2 = mongodb-rs-1.mongodb-rs-svc.eksrsoppoc1d.svc.cluster.local
DNS.3 = mongodb-rs-2.mongodb-rs-svc.eksrsoppoc1d.svc.cluster.local
DNS.4 = mongodb-rs-svc.eksrsoppoc1d.svc.cluster.local
DNS.5 = *.mongodb-rs-svc.eksrsoppoc1d.svc.cluster.local
DNS.6 = localhost
IP.1 = 127.0.0.1
EOF

# Generate server key and certificate
openssl genrsa -out certs/mongodb/mongodb.key 2048

openssl req -new -key certs/mongodb/mongodb.key \
  -out certs/mongodb/mongodb.csr \
  -config certs/mongodb/mongodb-ext.cnf

openssl x509 -req -in certs/mongodb/mongodb.csr \
  -CA certs/ca.crt -CAkey certs/ca.key \
  -CAcreateserial -out certs/mongodb/mongodb.crt \
  -days 365 -extensions v3_req \
  -extfile certs/mongodb/mongodb-ext.cnf
```

### 4.2 Generate Client Certificate (for X509 Authentication)

```bash
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

openssl genrsa -out certs/mongodb/client.key 2048

openssl req -new -key certs/mongodb/client.key \
  -out certs/mongodb/client.csr \
  -config certs/mongodb/client-ext.cnf

openssl x509 -req -in certs/mongodb/client.csr \
  -CA certs/ca.crt -CAkey certs/ca.key \
  -CAcreateserial -out certs/mongodb/client.crt \
  -days 365 -extensions v3_req \
  -extfile certs/mongodb/client-ext.cnf

# Create combined PEM for mongosh
cat certs/mongodb/client.crt certs/mongodb/client.key > certs/mongodb/client.pem
```

### 4.3 Create TLS Secrets in Kubernetes

```bash
# MongoDB CA ConfigMap (key must be 'ca-pem')
kubectl --kubeconfig .kube/config create configmap mongodb-ca \
  --from-file=ca-pem=./certs/ca.crt \
  -n eksrsoppoc1d

# Server TLS secret
kubectl --kubeconfig .kube/config create secret tls mongodb-mongodb-rs-cert \
  --cert=./certs/mongodb/mongodb.crt \
  --key=./certs/mongodb/mongodb.key \
  -n eksrsoppoc1d

# Agent TLS secret
kubectl --kubeconfig .kube/config create secret tls mongodb-mongodb-rs-agent-certs \
  --cert=./certs/mongodb/mongodb.crt \
  --key=./certs/mongodb/mongodb.key \
  -n eksrsoppoc1d
```

---

## Step 5: Pre-Create NodePort Services

Save as `nodeport-services.yaml` and apply:

```yaml
---
apiVersion: v1
kind: Service
metadata:
  name: mongodb-rs-0-svc-external
  namespace: eksrsoppoc1d
  labels:
    controller: mongodb-enterprise-operator
    statefulset.kubernetes.io/pod-name: mongodb-rs-0
spec:
  type: NodePort
  ports:
  - name: mongodb
    port: 27017
    targetPort: 27017
    nodePort: 30000
  selector:
    controller: mongodb-enterprise-operator
    statefulset.kubernetes.io/pod-name: mongodb-rs-0
---
apiVersion: v1
kind: Service
metadata:
  name: mongodb-rs-1-svc-external
  namespace: eksrsoppoc1d
  labels:
    controller: mongodb-enterprise-operator
    statefulset.kubernetes.io/pod-name: mongodb-rs-1
spec:
  type: NodePort
  ports:
  - name: mongodb
    port: 27017
    targetPort: 27017
    nodePort: 30001
  selector:
    controller: mongodb-enterprise-operator
    statefulset.kubernetes.io/pod-name: mongodb-rs-1
---
apiVersion: v1
kind: Service
metadata:
  name: mongodb-rs-2-svc-external
  namespace: eksrsoppoc1d
  labels:
    controller: mongodb-enterprise-operator
    statefulset.kubernetes.io/pod-name: mongodb-rs-2
spec:
  type: NodePort
  ports:
  - name: mongodb
    port: 27017
    targetPort: 27017
    nodePort: 30002
  selector:
    controller: mongodb-enterprise-operator
    statefulset.kubernetes.io/pod-name: mongodb-rs-2
```

```bash
kubectl --kubeconfig .kube/config apply -f nodeport-services.yaml
```

---

## Step 6: Deploy MongoDB ReplicaSet

### 6.1 Deploy the ReplicaSet

Save as `mongodb-replicaset.yaml` and apply:

```yaml
apiVersion: mongodb.com/v1
kind: MongoDB
metadata:
  name: mongodb-rs
  namespace: eksrsoppoc1d
spec:
  members: 3
  version: "7.0.25-ent"
  type: ReplicaSet

  opsManager:
    configMapRef:
      name: ops-manager-connection

  credentials: ops-manager-admin-key

  agent:
    startupOptions:
      tlsRequireValidMMSServerCertificates: "false"

  externalAccess:
    externalService:
      spec:
        type: NodePort

  connectivity:
    replicaSetHorizons:
      - "external": "localhost:30000"
      - "external": "localhost:30001"
      - "external": "localhost:30002"

  persistent: true

  podSpec:
    podTemplate:
      spec:
        containers:
          - name: mongodb-enterprise-database
            env:
              - name: SSL_REQUIRE_VALID_MMS_CERTIFICATES
                value: "false"
            resources:
              requests:
                cpu: "500m"
                memory: "1Gi"
              limits:
                cpu: "1"
                memory: "2Gi"
    persistence:
      single:
        storage: "5Gi"

  security:
    certsSecretPrefix: mongodb
    tls:
      ca: mongodb-ca
    authentication:
      enabled: true
      modes: ["SCRAM", "X509"]
      agents:
        mode: SCRAM
      ignoreUnknownUsers: true
```

```bash
kubectl --kubeconfig .kube/config apply -f mongodb-replicaset.yaml
```

### 6.2 Create MongoDB User Password Secret

Save as `mongodb-user-secret.yaml` and apply (replace password):

```yaml
apiVersion: v1
kind: Secret
metadata:
  name: mongodb-admin-password
  namespace: eksrsoppoc1d
type: Opaque
stringData:
  # Replace with a secure password
  password: "YourSecurePassword123!"
```

```bash
kubectl --kubeconfig .kube/config apply -f mongodb-user-secret.yaml
```

### 6.3 Create SCRAM User

Save as `mongodb-user.yaml` and apply:

```yaml
apiVersion: mongodb.com/v1
kind: MongoDBUser
metadata:
  name: mongodb-admin-user
  namespace: eksrsoppoc1d
spec:
  username: "admin"
  db: "admin"
  passwordSecretKeyRef:
    name: mongodb-admin-password
    key: password
  mongodbResourceRef:
    name: "mongodb-rs"
  roles:
    - db: "admin"
      name: "clusterAdmin"
    - db: "admin"
      name: "userAdminAnyDatabase"
    - db: "admin"
      name: "readWriteAnyDatabase"
    - db: "admin"
      name: "dbAdminAnyDatabase"
```

```bash
kubectl --kubeconfig .kube/config apply -f mongodb-user.yaml
```

### 6.4 Create X509 User

Save as `mongodb-x509-user.yaml` and apply:

```yaml
apiVersion: mongodb.com/v1
kind: MongoDBUser
metadata:
  name: mongodb-x509-user
  namespace: eksrsoppoc1d
spec:
  # Must match certificate subject DN (most specific first)
  username: "CN=x509-client,OU=clients,O=MongoDB"
  db: "$external"
  mongodbResourceRef:
    name: "mongodb-rs"
  roles:
    - db: "admin"
      name: "clusterAdmin"
    - db: "admin"
      name: "userAdminAnyDatabase"
    - db: "admin"
      name: "readWriteAnyDatabase"
    - db: "admin"
      name: "dbAdminAnyDatabase"
```

```bash
kubectl --kubeconfig .kube/config apply -f mongodb-x509-user.yaml
```

### 6.5 Monitor Deployment

```bash
# Watch MongoDB resource status
kubectl --kubeconfig .kube/config get mongodb -n eksrsoppoc1d -w

# Check pod status
kubectl --kubeconfig .kube/config get pods -n eksrsoppoc1d -w

# View detailed status
kubectl --kubeconfig .kube/config describe mongodb mongodb-rs -n eksrsoppoc1d
```

---

## Step 7: Verify Deployment

### 7.1 Check MongoDB Status

```bash
kubectl --kubeconfig .kube/config get mongodb -n eksrsoppoc1d
```

Expected output:
```
NAME         PHASE     VERSION       TYPE         AGE
mongodb-rs   Running   7.0.25-ent   ReplicaSet   10m
```

### 7.2 Check Pods

```bash
kubectl --kubeconfig .kube/config get pods -n eksrsoppoc1d
```

Expected output:
```
NAME           READY   STATUS    RESTARTS   AGE
mongodb-rs-0   2/2     Running   0          10m
mongodb-rs-1   2/2     Running   0          8m
mongodb-rs-2   2/2     Running   0          6m
```

### 7.3 Check Services

```bash
kubectl --kubeconfig .kube/config get svc -n eksrsoppoc1d | grep external
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

---

## Troubleshooting

### Common Issues

#### 1. Pods stuck in Pending state
```bash
kubectl --kubeconfig .kube/config describe pod mongodb-rs-0 -n eksrsoppoc1d
```

#### 2. Agent cannot connect to Ops Manager
```bash
kubectl --kubeconfig .kube/config run -it --rm debug \
  --image=curlimages/curl --restart=Never -- \
  curl -k https://host.docker.internal:8443/user/login
```

#### 3. View Operator Logs
```bash
kubectl --kubeconfig .kube/config logs -n mongodb \
  -l app.kubernetes.io/name=mongodb-enterprise-operator
```

#### 4. View MongoDB Pod Logs
```bash
kubectl --kubeconfig .kube/config logs -n eksrsoppoc1d \
  -l app=mongodb-rs-svc -c mongodb-enterprise-database
```

---

## Cleanup

```bash
# Delete MongoDB resources
kubectl --kubeconfig .kube/config delete -f mongodb-replicaset.yaml
kubectl --kubeconfig .kube/config wait --for=delete pod -l app=mongodb-rs-svc -n eksrsoppoc1d --timeout=180s

# Delete kind cluster
kind delete cluster --name mongodb-k8s

# Clean local files
rm -rf .kube/ certs/
```

---

## References

- [MongoDB Enterprise Kubernetes Operator Documentation](https://www.mongodb.com/docs/kubernetes-operator/stable/)
- [External Connectivity Guide](https://www.mongodb.com/docs/kubernetes-operator/v1.33/tutorial/connect-from-outside-k8s/)
- [kind Documentation](https://kind.sigs.k8s.io/)
