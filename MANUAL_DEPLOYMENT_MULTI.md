# MongoDB Enterprise Kubernetes Operator - Multi-Cluster Manual Deployment Guide

This guide provides step-by-step instructions for manually deploying MongoDB across multiple Kubernetes clusters using the MongoDB Enterprise Kubernetes Operator. These steps replicate what the `deploy_mongodb_k8s_multi.py` script does automatically.

## Prerequisites

1. **Docker** running on your machine
2. **Ops Manager** deployed and accessible (use `deploy_ops_manager.py` or see separate guide)
3. **API credentials** from Ops Manager (`ops-manager-api-key.json`)
4. **CA certificate** for TLS (`./certs/ca.crt` and `./certs/ca.key`)
5. **kind** binary (download from https://kind.sigs.k8s.io/)
6. **kubectl** installed

### Resource Requirements

| Resource | Minimum | Recommended |
|----------|---------|-------------|
| Docker Memory | 8 GB | 12 GB |
| Docker CPUs | 4 cores | 6 cores |
| Disk Space | 15 GB | 20 GB |

## Manual Deployment Steps

### Step 0: Reset Ops Manager Project (Required for Redeployment)

**Critical**: If you're redeploying to an existing Ops Manager project, you must reset the automation config first. Ops Manager retains TLS certificate hashes and replica set configuration from previous deployments, which will cause authentication failures.

**Option A: Reset via Ops Manager API (Recommended)**

```bash
# Get your project ID from ops-manager-api-key.json
PROJECT_ID=$(cat ops-manager-api-key.json | python -c "import sys,json; print(json.load(sys.stdin)['projects']['multiCluster']['projectId'])")
PUBLIC_KEY=$(cat ops-manager-api-key.json | python -c "import sys,json; print(json.load(sys.stdin)['publicKey'])")
PRIVATE_KEY=$(cat ops-manager-api-key.json | python -c "import sys,json; print(json.load(sys.stdin)['privateKey'])")

# Get current automation config
curl -k --digest -u "${PUBLIC_KEY}:${PRIVATE_KEY}" \
  "https://localhost:8443/api/public/v1.0/groups/${PROJECT_ID}/automationConfig" \
  -o /tmp/automation-config.json

# Reset processes, replicaSets, and auth (using Python to modify JSON)
python3 << 'PYEOF'
import json
with open('/tmp/automation-config.json', 'r') as f:
    config = json.load(f)
config['processes'] = []
config['replicaSets'] = []
config['sharding'] = []
if 'auth' in config:
    config['auth']['disabled'] = True
    config['auth']['usersWanted'] = []
if 'tls' in config:
    config['tls'] = {'clientCertificateMode': 'OPTIONAL'}
with open('/tmp/automation-config-reset.json', 'w') as f:
    json.dump(config, f)
PYEOF

# Apply the reset config
curl -k --digest -u "${PUBLIC_KEY}:${PRIVATE_KEY}" \
  -X PUT -H "Content-Type: application/json" \
  -d @/tmp/automation-config-reset.json \
  "https://localhost:8443/api/public/v1.0/groups/${PROJECT_ID}/automationConfig"

echo "Ops Manager automation config reset"
```

**Option B: Delete and Recreate Project in Ops Manager UI**

1. Go to Ops Manager UI → Organization → Projects
2. Delete the "MultiCluster" project
3. Create a new project with the same name
4. Update the `projectId` in `ops-manager-api-key.json` with the new project ID

**Option C: Use a Fresh Project Name**

Create a new project in Ops Manager with a different name and update the `ops-manager-connection` ConfigMap in Step 8.

### Step 1: Create Kind Clusters

```bash
mkdir -p .kube-multi

# Create central cluster configuration
cat > .kube-multi/central-kind-config.yaml << 'EOF'
kind: Cluster
apiVersion: kind.x-k8s.io/v1alpha4
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

# Create member cluster configuration
cat > .kube-multi/member-kind-config.yaml << 'EOF'
kind: Cluster
apiVersion: kind.x-k8s.io/v1alpha4
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

# Create the clusters
kind create cluster --name mongodb-central --config .kube-multi/central-kind-config.yaml --wait 120s
kind create cluster --name mongodb-member-1 --config .kube-multi/member-kind-config.yaml --wait 120s

# Export kubeconfigs
kind get kubeconfig --name mongodb-central > .kube-multi/central-config
kind get kubeconfig --name mongodb-member-1 > .kube-multi/member-config
```

### Step 2: Create Namespaces and Deploy CRDs

```bash
export KUBECONFIG=./.kube-multi/central-config

# Create namespaces on central cluster
kubectl create namespace mongodb
kubectl create namespace mongodb-rs

# Deploy CRDs
kubectl apply -f https://raw.githubusercontent.com/mongodb/mongodb-enterprise-kubernetes/master/crds.yaml

# Create namespace on member cluster
kubectl --kubeconfig ./.kube-multi/member-config create namespace mongodb-rs
```

### Step 3: Create Member List ConfigMap

**Important**: Each cluster name must be a KEY with empty string value.

```bash
export KUBECONFIG=./.kube-multi/central-config

kubectl apply -f - << 'EOF'
apiVersion: v1
kind: ConfigMap
metadata:
  name: mongodb-enterprise-operator-member-list
  namespace: mongodb
data:
  kind-mongodb-central: ""
  kind-mongodb-member-1: ""
EOF
```

### Step 4: Create Multi-Cluster Kubeconfig Secret

**Critical**: The operator requires this secret BEFORE it can start. The kubeconfig must use Docker network IPs.

```bash
# Get Docker network IPs for the Kind control planes
CENTRAL_IP=$(docker inspect mongodb-central-control-plane --format '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}')
MEMBER_IP=$(docker inspect mongodb-member-1-control-plane --format '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}')
echo "Central IP: $CENTRAL_IP, Member IP: $MEMBER_IP"

# Get certificate data from both kubeconfigs
CENTRAL_CA=$(kubectl --kubeconfig ./.kube-multi/central-config config view --raw -o jsonpath='{.clusters[0].cluster.certificate-authority-data}')
CENTRAL_CERT=$(kubectl --kubeconfig ./.kube-multi/central-config config view --raw -o jsonpath='{.users[0].user.client-certificate-data}')
CENTRAL_KEY=$(kubectl --kubeconfig ./.kube-multi/central-config config view --raw -o jsonpath='{.users[0].user.client-key-data}')

MEMBER_CA=$(kubectl --kubeconfig ./.kube-multi/member-config config view --raw -o jsonpath='{.clusters[0].cluster.certificate-authority-data}')
MEMBER_CERT=$(kubectl --kubeconfig ./.kube-multi/member-config config view --raw -o jsonpath='{.users[0].user.client-certificate-data}')
MEMBER_KEY=$(kubectl --kubeconfig ./.kube-multi/member-config config view --raw -o jsonpath='{.users[0].user.client-key-data}')

# Create combined kubeconfig with Docker network IPs (port 6443 is internal K8s API port)
cat > .kube-multi/operator-kubeconfig.yaml << EOF
apiVersion: v1
kind: Config
clusters:
- name: kind-mongodb-central
  cluster:
    certificate-authority-data: ${CENTRAL_CA}
    server: https://${CENTRAL_IP}:6443
- name: kind-mongodb-member-1
  cluster:
    certificate-authority-data: ${MEMBER_CA}
    server: https://${MEMBER_IP}:6443
contexts:
- name: kind-mongodb-central
  context:
    cluster: kind-mongodb-central
    user: kind-mongodb-central
- name: kind-mongodb-member-1
  context:
    cluster: kind-mongodb-member-1
    user: kind-mongodb-member-1
current-context: kind-mongodb-central
users:
- name: kind-mongodb-central
  user:
    client-certificate-data: ${CENTRAL_CERT}
    client-key-data: ${CENTRAL_KEY}
- name: kind-mongodb-member-1
  user:
    client-certificate-data: ${MEMBER_CERT}
    client-key-data: ${MEMBER_KEY}
EOF

# Create the secret
export KUBECONFIG=./.kube-multi/central-config
kubectl create secret generic mongodb-enterprise-operator-multi-cluster-kubeconfig \
  -n mongodb \
  --from-file=kubeconfig=./.kube-multi/operator-kubeconfig.yaml
```

### Step 5: Deploy Multi-Cluster Operator

Now that the kubeconfig secret exists, deploy the operator:

```bash
export KUBECONFIG=./.kube-multi/central-config

# Deploy MULTI-CLUSTER operator
kubectl apply -f https://raw.githubusercontent.com/mongodb/mongodb-enterprise-kubernetes/master/mongodb-enterprise-multi-cluster.yaml

# Configure operator to watch mongodb-rs namespace
kubectl set env deployment/mongodb-enterprise-operator-multi-cluster -n mongodb WATCH_NAMESPACE=mongodb-rs

# Wait for operator to be ready
kubectl wait --for=condition=available deployment/mongodb-enterprise-operator-multi-cluster -n mongodb --timeout=180s
```

### Step 6: Deploy RBAC on Central Cluster

```bash
export KUBECONFIG=./.kube-multi/central-config

# Create operator RBAC for mongodb-rs namespace
kubectl apply -f - << 'EOF'
apiVersion: rbac.authorization.k8s.io/v1
kind: Role
metadata:
  name: mongodb-enterprise-operator
  namespace: mongodb-rs
rules:
- apiGroups: [""]
  resources: ["services", "secrets", "configmaps", "pods", "persistentvolumeclaims"]
  verbs: ["*"]
- apiGroups: ["apps"]
  resources: ["statefulsets"]
  verbs: ["*"]
- apiGroups: ["mongodb.com"]
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
  name: mongodb-enterprise-operator-multi-cluster
  namespace: mongodb
EOF

# Create database service accounts and roles
kubectl apply -f - << 'EOF'
apiVersion: v1
kind: ServiceAccount
metadata:
  name: mongodb-enterprise-database-pods
  namespace: mongodb-rs
---
apiVersion: v1
kind: ServiceAccount
metadata:
  name: mongodb-enterprise-appdb
  namespace: mongodb-rs
---
apiVersion: rbac.authorization.k8s.io/v1
kind: Role
metadata:
  name: mongodb-enterprise-database-pods
  namespace: mongodb-rs
rules:
- apiGroups: [""]
  resources: ["secrets"]
  verbs: ["get"]
- apiGroups: [""]
  resources: ["pods"]
  verbs: ["patch", "delete", "get"]
---
apiVersion: rbac.authorization.k8s.io/v1
kind: RoleBinding
metadata:
  name: mongodb-enterprise-database-pods
  namespace: mongodb-rs
roleRef:
  apiGroup: rbac.authorization.k8s.io
  kind: Role
  name: mongodb-enterprise-database-pods
subjects:
- kind: ServiceAccount
  name: mongodb-enterprise-database-pods
  namespace: mongodb-rs
EOF
```

### Step 7: Prepare Member Cluster

```bash
export KUBECONFIG=./.kube-multi/member-config

# Create database service accounts and roles
kubectl apply -f - << 'EOF'
apiVersion: v1
kind: ServiceAccount
metadata:
  name: mongodb-enterprise-database-pods
  namespace: mongodb-rs
---
apiVersion: v1
kind: ServiceAccount
metadata:
  name: mongodb-enterprise-appdb
  namespace: mongodb-rs
---
apiVersion: rbac.authorization.k8s.io/v1
kind: Role
metadata:
  name: mongodb-enterprise-database-pods
  namespace: mongodb-rs
rules:
- apiGroups: [""]
  resources: ["secrets"]
  verbs: ["get"]
- apiGroups: [""]
  resources: ["pods"]
  verbs: ["patch", "delete", "get"]
---
apiVersion: rbac.authorization.k8s.io/v1
kind: RoleBinding
metadata:
  name: mongodb-enterprise-database-pods
  namespace: mongodb-rs
roleRef:
  apiGroup: rbac.authorization.k8s.io
  kind: Role
  name: mongodb-enterprise-database-pods
subjects:
- kind: ServiceAccount
  name: mongodb-enterprise-database-pods
  namespace: mongodb-rs
EOF
```

### Step 8: Create Ops Manager Credentials

Replace placeholder values with your actual credentials from `ops-manager-api-key.json`:

```bash
export KUBECONFIG=./.kube-multi/central-config

# Create Ops Manager API key secret
kubectl apply -f - << 'EOF'
apiVersion: v1
kind: Secret
metadata:
  name: ops-manager-admin-key
  namespace: mongodb-rs
stringData:
  publicKey: "YOUR_PUBLIC_KEY"
  privateKey: "YOUR_PRIVATE_KEY"
EOF

# Create Ops Manager connection ConfigMap
kubectl apply -f - << 'EOF'
apiVersion: v1
kind: ConfigMap
metadata:
  name: ops-manager-connection
  namespace: mongodb-rs
data:
  baseUrl: "https://host.docker.internal:8443"
  projectName: "MultiCluster"
  orgId: "YOUR_ORG_ID"
  sslMMSCAConfigMap: "ops-manager-ca"
  sslRequireValidMMSServerCertificates: "false"
EOF

# Create CA certificate ConfigMap for Ops Manager
kubectl create configmap ops-manager-ca \
  --from-file=mms-ca.crt=./certs/ca.crt \
  -n mongodb-rs
```

### Step 9: Configure Cross-Cluster DNS (CoreDNS)

**Critical**: Pods need to resolve external hostnames like `mongodb-multi-rs-0-0.central.mongodb.local`. This step configures CoreDNS for cross-cluster DNS resolution.

```bash
# Get ClusterIPs for MongoDB services (may need to be updated after pods are created)
# For now, use virtual IPs that will be routed via iptables

# Central cluster CoreDNS configuration:
# - LOCAL pods: Rewrite central.mongodb.local to internal K8s DNS
# - REMOTE pods: Map member1.mongodb.local to virtual IPs
kubectl --kubeconfig ./.kube-multi/central-config apply -f - << 'EOF'
apiVersion: v1
kind: ConfigMap
metadata:
  name: coredns
  namespace: kube-system
data:
  Corefile: |
    .:53 {
        errors
        health {
           lameduck 5s
        }
        ready
        rewrite name mongodb-multi-rs-0-0.central.mongodb.local mongodb-multi-rs-0-0.mongodb-multi-rs-0-svc.mongodb-rs.svc.cluster.local
        rewrite name mongodb-multi-rs-0-1.central.mongodb.local mongodb-multi-rs-0-1.mongodb-multi-rs-0-svc.mongodb-rs.svc.cluster.local
        rewrite name mongodb-multi-rs-0-2.central.mongodb.local mongodb-multi-rs-0-2.mongodb-multi-rs-0-svc.mongodb-rs.svc.cluster.local
        hosts {
            172.19.0.100 mongodb-multi-rs-1-0.member1.mongodb.local
            172.19.0.101 mongodb-multi-rs-1-1.member1.mongodb.local
            fallthrough
        }
        kubernetes cluster.local in-addr.arpa ip6.arpa {
           pods insecure
           fallthrough in-addr.arpa ip6.arpa
           ttl 30
        }
        prometheus :9153
        forward . /etc/resolv.conf {
           max_concurrent 1000
        }
        cache 30
        loop
        reload
        loadbalance
    }
EOF

# Member cluster CoreDNS configuration:
# - LOCAL pods: Rewrite member1.mongodb.local to internal K8s DNS
# - REMOTE pods: Map central.mongodb.local to virtual IPs
kubectl --kubeconfig ./.kube-multi/member-config apply -f - << 'EOF'
apiVersion: v1
kind: ConfigMap
metadata:
  name: coredns
  namespace: kube-system
data:
  Corefile: |
    .:53 {
        errors
        health {
           lameduck 5s
        }
        ready
        rewrite name mongodb-multi-rs-1-0.member1.mongodb.local mongodb-multi-rs-1-0.mongodb-multi-rs-1-svc.mongodb-rs.svc.cluster.local
        rewrite name mongodb-multi-rs-1-1.member1.mongodb.local mongodb-multi-rs-1-1.mongodb-multi-rs-1-svc.mongodb-rs.svc.cluster.local
        hosts {
            172.19.0.200 mongodb-multi-rs-0-0.central.mongodb.local
            172.19.0.201 mongodb-multi-rs-0-1.central.mongodb.local
            172.19.0.202 mongodb-multi-rs-0-2.central.mongodb.local
            fallthrough
        }
        kubernetes cluster.local in-addr.arpa ip6.arpa {
           pods insecure
           fallthrough in-addr.arpa ip6.arpa
           ttl 30
        }
        prometheus :9153
        forward . /etc/resolv.conf {
           max_concurrent 1000
        }
        cache 30
        loop
        reload
        loadbalance
    }
EOF

# Restart CoreDNS to pick up new config
kubectl --kubeconfig ./.kube-multi/central-config rollout restart deployment/coredns -n kube-system
kubectl --kubeconfig ./.kube-multi/member-config rollout restart deployment/coredns -n kube-system
```

### Step 10: Configure Cross-Cluster Network Routing (iptables)

**Critical**: Route virtual IPs to NodePorts on the other cluster. This requires:
- **PREROUTING** rules: For traffic from pods (different network namespace)
- **OUTPUT** rules: For traffic from the node itself
- **MASQUERADE** rule: So return traffic can find its way back

```bash
# Get Docker container names
CENTRAL_CONTAINER="mongodb-central-control-plane"
MEMBER_CONTAINER="mongodb-member-1-control-plane"

# Get Docker IPs
CENTRAL_IP=$(docker inspect $CENTRAL_CONTAINER --format '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}')
MEMBER_IP=$(docker inspect $MEMBER_CONTAINER --format '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}')

echo "Central IP: $CENTRAL_IP, Member IP: $MEMBER_IP"

# On CENTRAL cluster: Route member virtual IPs to member cluster's NodePorts
# PREROUTING for pod traffic, OUTPUT for node traffic
docker exec $CENTRAL_CONTAINER iptables -t nat -A PREROUTING -d 172.19.0.100 -p tcp --dport 27017 -j DNAT --to-destination $MEMBER_IP:30200
docker exec $CENTRAL_CONTAINER iptables -t nat -A PREROUTING -d 172.19.0.101 -p tcp --dport 27017 -j DNAT --to-destination $MEMBER_IP:30201
docker exec $CENTRAL_CONTAINER iptables -t nat -A OUTPUT -d 172.19.0.100 -p tcp --dport 27017 -j DNAT --to-destination $MEMBER_IP:30200
docker exec $CENTRAL_CONTAINER iptables -t nat -A OUTPUT -d 172.19.0.101 -p tcp --dport 27017 -j DNAT --to-destination $MEMBER_IP:30201

# On MEMBER cluster: Route central virtual IPs to central cluster's NodePorts
docker exec $MEMBER_CONTAINER iptables -t nat -A PREROUTING -d 172.19.0.200 -p tcp --dport 27017 -j DNAT --to-destination $CENTRAL_IP:30100
docker exec $MEMBER_CONTAINER iptables -t nat -A PREROUTING -d 172.19.0.201 -p tcp --dport 27017 -j DNAT --to-destination $CENTRAL_IP:30101
docker exec $MEMBER_CONTAINER iptables -t nat -A PREROUTING -d 172.19.0.202 -p tcp --dport 27017 -j DNAT --to-destination $CENTRAL_IP:30102
docker exec $MEMBER_CONTAINER iptables -t nat -A OUTPUT -d 172.19.0.200 -p tcp --dport 27017 -j DNAT --to-destination $CENTRAL_IP:30100
docker exec $MEMBER_CONTAINER iptables -t nat -A OUTPUT -d 172.19.0.201 -p tcp --dport 27017 -j DNAT --to-destination $CENTRAL_IP:30101
docker exec $MEMBER_CONTAINER iptables -t nat -A OUTPUT -d 172.19.0.202 -p tcp --dport 27017 -j DNAT --to-destination $CENTRAL_IP:30102

# Add MASQUERADE rule for return traffic (required for cross-cluster communication)
docker exec $CENTRAL_CONTAINER iptables -t nat -C POSTROUTING -o eth0 -j MASQUERADE 2>/dev/null || \
  docker exec $CENTRAL_CONTAINER iptables -t nat -A POSTROUTING -o eth0 -j MASQUERADE
docker exec $MEMBER_CONTAINER iptables -t nat -C POSTROUTING -o eth0 -j MASQUERADE 2>/dev/null || \
  docker exec $MEMBER_CONTAINER iptables -t nat -A POSTROUTING -o eth0 -j MASQUERADE

echo "iptables rules configured"
```

### Step 11: Create TLS Certificates

```bash
mkdir -p certs/mongodb-multi

# Create OpenSSL config
cat > certs/mongodb-multi/mongodb-ext.cnf << 'EOF'
[req]
distinguished_name = req_distinguished_name
req_extensions = v3_req
prompt = no

[req_distinguished_name]
CN = mongodb-multi-rs

[v3_req]
basicConstraints = CA:FALSE
keyUsage = critical, digitalSignature, keyEncipherment
extendedKeyUsage = serverAuth, clientAuth
subjectAltName = @alt_names

[alt_names]
DNS.1 = mongodb-multi-rs-0-0.mongodb-multi-rs-svc.mongodb-rs.svc.cluster.local
DNS.2 = mongodb-multi-rs-0-1.mongodb-multi-rs-svc.mongodb-rs.svc.cluster.local
DNS.3 = mongodb-multi-rs-0-2.mongodb-multi-rs-svc.mongodb-rs.svc.cluster.local
DNS.4 = mongodb-multi-rs-1-0.mongodb-multi-rs-svc.mongodb-rs.svc.cluster.local
DNS.5 = mongodb-multi-rs-1-1.mongodb-multi-rs-svc.mongodb-rs.svc.cluster.local
DNS.6 = mongodb-multi-rs-0-0.central.mongodb.local
DNS.7 = mongodb-multi-rs-0-1.central.mongodb.local
DNS.8 = mongodb-multi-rs-0-2.central.mongodb.local
DNS.9 = mongodb-multi-rs-1-0.member1.mongodb.local
DNS.10 = mongodb-multi-rs-1-1.member1.mongodb.local
DNS.11 = mongodb-multi-rs-svc.mongodb-rs.svc.cluster.local
DNS.12 = *.mongodb-multi-rs-svc.mongodb-rs.svc.cluster.local
DNS.13 = localhost
IP.1 = 127.0.0.1
EOF

# Generate server certificate
openssl genrsa -out certs/mongodb-multi/mongodb.key 2048
openssl req -new -key certs/mongodb-multi/mongodb.key \
  -out certs/mongodb-multi/mongodb.csr \
  -config certs/mongodb-multi/mongodb-ext.cnf
openssl x509 -req -in certs/mongodb-multi/mongodb.csr \
  -CA certs/ca.crt -CAkey certs/ca.key \
  -CAcreateserial -out certs/mongodb-multi/mongodb.crt \
  -days 365 -extensions v3_req \
  -extfile certs/mongodb-multi/mongodb-ext.cnf

# Deploy certificates to CENTRAL cluster
export KUBECONFIG=./.kube-multi/central-config

kubectl create configmap mongodb-ca \
  --from-file=ca-pem=./certs/ca.crt \
  -n mongodb-rs

kubectl create secret tls mongodb-mongodb-multi-rs-cert \
  --cert=./certs/mongodb-multi/mongodb.crt \
  --key=./certs/mongodb-multi/mongodb.key \
  -n mongodb-rs

kubectl create secret tls mongodb-mongodb-multi-rs-agent-certs \
  --cert=./certs/mongodb-multi/mongodb.crt \
  --key=./certs/mongodb-multi/mongodb.key \
  -n mongodb-rs

# Deploy certificates to MEMBER cluster
export KUBECONFIG=./.kube-multi/member-config

kubectl create configmap mongodb-ca \
  --from-file=ca-pem=./certs/ca.crt \
  -n mongodb-rs

kubectl create secret tls mongodb-mongodb-multi-rs-cert \
  --cert=./certs/mongodb-multi/mongodb.crt \
  --key=./certs/mongodb-multi/mongodb.key \
  -n mongodb-rs

kubectl create secret tls mongodb-mongodb-multi-rs-agent-certs \
  --cert=./certs/mongodb-multi/mongodb.crt \
  --key=./certs/mongodb-multi/mongodb.key \
  -n mongodb-rs
```

### Step 12: Pre-Create NodePort Services with Fixed Ports

**Why pre-create services**: The MongoDB operator reuses existing services if they match the expected naming convention. By pre-creating services with fixed NodePorts, the operator preserves the port assignments instead of assigning random ports.

**Important**: Each cluster needs only its own services. Apply only the relevant services to each cluster.

```bash
# Apply CENTRAL cluster services (ports 30100, 30101, 30102)
kubectl --kubeconfig ./.kube-multi/central-config apply -f - << 'EOF'
apiVersion: v1
kind: Service
metadata:
  name: mongodb-multi-rs-0-0-svc-external
  namespace: mongodb-rs
  labels:
    controller: mongodb-enterprise-operator
    statefulset.kubernetes.io/pod-name: mongodb-multi-rs-0-0
spec:
  type: NodePort
  ports:
  - name: mongodb
    port: 27017
    targetPort: 27017
    nodePort: 30100
  selector:
    controller: mongodb-enterprise-operator
    statefulset.kubernetes.io/pod-name: mongodb-multi-rs-0-0
---
apiVersion: v1
kind: Service
metadata:
  name: mongodb-multi-rs-0-1-svc-external
  namespace: mongodb-rs
  labels:
    controller: mongodb-enterprise-operator
    statefulset.kubernetes.io/pod-name: mongodb-multi-rs-0-1
spec:
  type: NodePort
  ports:
  - name: mongodb
    port: 27017
    targetPort: 27017
    nodePort: 30101
  selector:
    controller: mongodb-enterprise-operator
    statefulset.kubernetes.io/pod-name: mongodb-multi-rs-0-1
---
apiVersion: v1
kind: Service
metadata:
  name: mongodb-multi-rs-0-2-svc-external
  namespace: mongodb-rs
  labels:
    controller: mongodb-enterprise-operator
    statefulset.kubernetes.io/pod-name: mongodb-multi-rs-0-2
spec:
  type: NodePort
  ports:
  - name: mongodb
    port: 27017
    targetPort: 27017
    nodePort: 30102
  selector:
    controller: mongodb-enterprise-operator
    statefulset.kubernetes.io/pod-name: mongodb-multi-rs-0-2
EOF

# Apply MEMBER cluster services (ports 30200, 30201)
kubectl --kubeconfig ./.kube-multi/member-config apply -f - << 'EOF'
apiVersion: v1
kind: Service
metadata:
  name: mongodb-multi-rs-1-0-svc-external
  namespace: mongodb-rs
  labels:
    controller: mongodb-enterprise-operator
    statefulset.kubernetes.io/pod-name: mongodb-multi-rs-1-0
spec:
  type: NodePort
  ports:
  - name: mongodb
    port: 27017
    targetPort: 27017
    nodePort: 30200
  selector:
    controller: mongodb-enterprise-operator
    statefulset.kubernetes.io/pod-name: mongodb-multi-rs-1-0
---
apiVersion: v1
kind: Service
metadata:
  name: mongodb-multi-rs-1-1-svc-external
  namespace: mongodb-rs
  labels:
    controller: mongodb-enterprise-operator
    statefulset.kubernetes.io/pod-name: mongodb-multi-rs-1-1
spec:
  type: NodePort
  ports:
  - name: mongodb
    port: 27017
    targetPort: 27017
    nodePort: 30201
  selector:
    controller: mongodb-enterprise-operator
    statefulset.kubernetes.io/pod-name: mongodb-multi-rs-1-1
EOF
```

### Step 13: Deploy MongoDBMultiCluster Resource

```bash
export KUBECONFIG=./.kube-multi/central-config

kubectl apply -f - << 'EOF'
apiVersion: mongodb.com/v1
kind: MongoDBMultiCluster
metadata:
  name: mongodb-multi-rs
  namespace: mongodb-rs
spec:
  version: "7.0.25-ent"
  type: ReplicaSet

  opsManager:
    configMapRef:
      name: ops-manager-connection

  credentials: ops-manager-admin-key

  clusterSpecList:
    - clusterName: kind-mongodb-central
      members: 3
      externalAccess:
        externalDomain: central.mongodb.local
        externalService:
          spec:
            type: NodePort
            port: 27017

    - clusterName: kind-mongodb-member-1
      members: 2
      externalAccess:
        externalDomain: member1.mongodb.local
        externalService:
          spec:
            type: NodePort
            port: 27017

  connectivity:
    replicaSetHorizons:
      - "external": "localhost:30100"
      - "external": "localhost:30101"
      - "external": "localhost:30102"
      - "external": "localhost:30200"
      - "external": "localhost:30201"

  agent:
    startupOptions:
      tlsRequireValidMMSServerCertificates: "false"

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
EOF

# Watch the deployment
kubectl get mongodbmulticluster -n mongodb-rs -w
```

### Step 14: Create MongoDB Users

#### SCRAM User (Username/Password)

```bash
export KUBECONFIG=./.kube-multi/central-config

kubectl apply -f - << 'EOF'
apiVersion: v1
kind: Secret
metadata:
  name: mongodb-admin-password
  namespace: mongodb-rs
stringData:
  password: "YourSecurePassword123!"
---
apiVersion: mongodb.com/v1
kind: MongoDBUser
metadata:
  name: admin
  namespace: mongodb-rs
spec:
  passwordSecretKeyRef:
    name: mongodb-admin-password
    key: password
  username: admin
  db: admin
  mongodbResourceRef:
    name: mongodb-multi-rs
    namespace: mongodb-rs
  roles:
    - db: admin
      name: root
EOF
```

#### X509 User (Certificate-based)

Generate a client certificate for X509 authentication:

```bash
# Create OpenSSL config for client certificate
cat > certs/mongodb-multi/client-ext.cnf << 'EOF'
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

# Generate client key and certificate
openssl genrsa -out certs/mongodb-multi/client.key 2048
openssl req -new -key certs/mongodb-multi/client.key \
  -out certs/mongodb-multi/client.csr \
  -config certs/mongodb-multi/client-ext.cnf
openssl x509 -req -in certs/mongodb-multi/client.csr \
  -CA certs/ca.crt -CAkey certs/ca.key \
  -CAcreateserial -out certs/mongodb-multi/client.crt \
  -days 365 -extensions v3_req \
  -extfile certs/mongodb-multi/client-ext.cnf

# Create combined PEM file for mongosh
cat certs/mongodb-multi/client.crt certs/mongodb-multi/client.key > certs/mongodb-multi/client.pem
```

Create the X509 MongoDB user:

```bash
export KUBECONFIG=./.kube-multi/central-config

kubectl apply -f - << 'EOF'
apiVersion: mongodb.com/v1
kind: MongoDBUser
metadata:
  name: mongodb-x509-user
  namespace: mongodb-rs
spec:
  username: "CN=x509-client,OU=clients,O=MongoDB"
  db: "$external"
  mongodbResourceRef:
    name: mongodb-multi-rs
  roles:
    - db: admin
      name: clusterAdmin
    - db: admin
      name: userAdminAnyDatabase
    - db: admin
      name: readWriteAnyDatabase
    - db: admin
      name: dbAdminAnyDatabase
EOF
```

## Monitoring Deployment

```bash
export KUBECONFIG=./.kube-multi/central-config

# Check MongoDBMultiCluster status
kubectl get mongodbmulticluster -n mongodb-rs

# Check pods on both clusters
kubectl get pods -n mongodb-rs
kubectl --kubeconfig ./.kube-multi/member-config get pods -n mongodb-rs

# Check operator logs
kubectl logs -n mongodb deployment/mongodb-enterprise-operator-multi-cluster --tail=100
```

## Connection Instructions

**Using SCRAM authentication:**

```bash
mongosh "mongodb://localhost:30100,localhost:30101,localhost:30102,localhost:30200,localhost:30201/?replicaSet=mongodb-multi-rs&tls=true&tlsCAFile=./certs/ca.crt" \
  --username admin \
  --authenticationDatabase admin
```

**Using X509 authentication:**

```bash
mongosh "mongodb://localhost:30100,localhost:30101,localhost:30102,localhost:30200,localhost:30201/?replicaSet=mongodb-multi-rs&tls=true&tlsCAFile=./certs/ca.crt&authMechanism=MONGODB-X509&authSource=\$external" \
  --tlsCertificateKeyFile ./certs/mongodb-multi/client.pem
```

## Cleanup

```bash
export KUBECONFIG=./.kube-multi/central-config
kubectl delete mongodbmulticluster mongodb-multi-rs -n mongodb-rs
sleep 30

kind delete cluster --name mongodb-central
kind delete cluster --name mongodb-member-1

rm -rf .kube-multi/ certs/mongodb-multi/
```

## Troubleshooting

### Operator pod stuck in ContainerCreating

The multi-cluster operator requires the kubeconfig secret BEFORE it starts:
```bash
kubectl get secret mongodb-enterprise-operator-multi-cluster-kubeconfig -n mongodb
```

### Operator not reconciling

Check operator logs:
```bash
kubectl logs -n mongodb deployment/mongodb-enterprise-operator-multi-cluster --tail=50
```

Verify member list ConfigMap format (cluster names as KEYS):
```bash
kubectl get configmap mongodb-enterprise-operator-member-list -n mongodb -o yaml
```

### Pods running but MongoDBMultiCluster stuck in Pending/Failed

This usually means cross-cluster connectivity is not working. Check:

1. **NodePorts not pre-created**: The iptables rules expect fixed NodePorts (30100-30102, 30200-30201). If you skipped Step 12, the operator assigns random ports. Either run Step 12 before deploying, or manually patch the services.

2. **Missing iptables PREROUTING rules**: Pod traffic goes through PREROUTING chain, not just OUTPUT. Verify both chains have rules:
```bash
docker exec mongodb-central-control-plane iptables -t nat -L PREROUTING -n | grep 172.19
docker exec mongodb-central-control-plane iptables -t nat -L OUTPUT -n | grep 172.19
```

3. **Test cross-cluster connectivity from a pod**:
```bash
kubectl exec mongodb-multi-rs-0-0 -n mongodb-rs -c mongodb-enterprise-database -- \
  bash -c "timeout 5 bash -c '</dev/tcp/172.19.0.100/27017' && echo OK || echo FAILED"
```

### Authentication failed - Certificate hash mismatch

Error in operator logs: `Cannot read certificate file: /mongodb-automation/tls/<HASH>`

This happens when Ops Manager has stale TLS certificate hashes from a previous deployment. Solution: Run Step 0 to reset the Ops Manager automation config.

### CAFilePath error when enabling authentication

Error: `The required attribute tls.CAFilePath or tls.CAFilePathWindows was not specified`

This indicates the Ops Manager automation config needs to be reset. The old config has TLS settings that conflict with the new deployment.
