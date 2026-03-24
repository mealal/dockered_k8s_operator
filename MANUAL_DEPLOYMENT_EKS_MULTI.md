# MCK (MongoDB Controllers for Kubernetes) - Multi-Cluster AWS EKS Deployment Guide

This guide provides step-by-step instructions for deploying MongoDB across multiple AWS EKS clusters using MCK (MongoDB Controllers for Kubernetes).

## Architecture Overview

```
                    +------------------------------------------------------------------+
                    |                         AWS Cloud                                 |
                    |                                                                   |
+--------------+    |  +-------------------------+    +-------------------------+      |
| Ops Manager  |<---+--| EKS Central Cluster     |    | EKS Member Cluster      |      |
|              |    |  | (mongodb-central)       |    | (mongodb-member-1)      |      |
|              |    |  |  +------------------+   |    |  +------------------+   |      |
+--------------+    |  |  | MongoDB Operator |   |    |  | MongoDB Pods (2) |   |      |
                    |  |  | + Pods (3)       |   |    |  +------------------+   |      |
                    |  |  +------------------+   |    |           |             |      |
                    |  |           |             |    |           v             |      |
                    |  |           v             |    |  +------------------+   |      |
                    |  |  +------------------+   |    |  | NLB per Pod      |   |      |
                    |  |  | NLB per Pod      |   |    |  | :10901           |   |      |
                    |  |  | :10901           |   |    |  +------------------+   |      |
                    |  |  +------------------+   |    |           |             |      |
                    |  +-----------+-------------+    +-----------+-------------+      |
                    |              |                              |                    |
                    |              +---------- VPC Peering -------+                    |
                    |                              |                                   |
                    |              +---------------v---------------+                   |
                    |              | Route 53 Private Hosted Zone  |                   |
                    |              | (mongodb.local)               |                   |
                    |              +-------------------------------+                   |
                    +------------------------------------------------------------------+
```

## Prerequisites

### 1. EKS Infrastructure

Before starting this guide, complete the EKS infrastructure provisioning:

**[EKS_PROVISIONING.md](EKS_PROVISIONING.md)** - Create EKS clusters, VPC peering, and storage configuration

After completing EKS provisioning, you should have:

- Two EKS clusters running (mongodb-central, mongodb-member-1)
- Kubeconfig files in `.kube-eks/central-config` and `.kube-eks/member-config`
- VPC peering configured between clusters
- Security groups allowing MongoDB traffic (port 10901)
- EBS CSI driver installed with gp3 StorageClass

### 2. Ops Manager

You need a pre-existing MongoDB Ops Manager instance accessible from both EKS clusters.

**Requirements:**

1. Ops Manager accessible from both EKS VPCs (via VPC peering, public endpoint, or AWS PrivateLink)
2. API key with appropriate permissions (Organization Owner or Project Owner)
3. CA certificate if using HTTPS with self-signed certificates

**Required Information:**

- Base URL (e.g., `https://ops-manager.example.com:8443`)
- Organization ID
- Public API Key
- Private API Key
- CA certificate file (if using HTTPS with self-signed certs)

### 3. Load Environment Variables

If you saved environment variables during provisioning:

```bash
source .kube-eks/env-vars.sh
```

Or set them manually:

```bash
CENTRAL_VPC=$(aws eks describe-cluster --name mongodb-central --region us-east-1 \
  --query 'cluster.resourcesVpcConfig.vpcId' --output text)
MEMBER_VPC=$(aws eks describe-cluster --name mongodb-member-1 --region us-east-1 \
  --query 'cluster.resourcesVpcConfig.vpcId' --output text)
CENTRAL_SG=$(aws eks describe-cluster --name mongodb-central --region us-east-1 \
  --query 'cluster.resourcesVpcConfig.clusterSecurityGroupId' --output text)
MEMBER_SG=$(aws eks describe-cluster --name mongodb-member-1 --region us-east-1 \
  --query 'cluster.resourcesVpcConfig.clusterSecurityGroupId' --output text)
```

---

## Step 1: Create Namespaces

```bash
export KUBECONFIG=.kube-eks/central-config

# Create namespaces on central cluster
kubectl create namespace mongodb
kubectl create namespace eksrsoppoc1d

# Create namespace on member cluster
kubectl --kubeconfig .kube-eks/member-config create namespace eksrsoppoc1d
```

---

## Step 2: Create Member List ConfigMap

```bash
export KUBECONFIG=.kube-eks/central-config

kubectl apply -f - << 'EOF'
apiVersion: v1
kind: ConfigMap
metadata:
  name: mongodb-kubernetes-operator-member-list
  namespace: mongodb
data:
  # Cluster names must match context names in kubeconfig
  mongodb-central: ""
  mongodb-member-1: ""
EOF
```

---

## Step 3: Create Service Accounts for Cross-Cluster Access

The operator needs to access both clusters. For EKS, we use service account tokens instead of IAM exec credentials (which don't work inside pods).

### Create Service Account and Token on Central Cluster

```bash
export KUBECONFIG=.kube-eks/central-config

kubectl apply -f - << 'EOF'
apiVersion: v1
kind: ServiceAccount
metadata:
  name: mongodb-operator-remote
  namespace: eksrsoppoc1d
---
apiVersion: v1
kind: Secret
metadata:
  name: mongodb-operator-remote-token
  namespace: eksrsoppoc1d
  annotations:
    kubernetes.io/service-account.name: mongodb-operator-remote
type: kubernetes.io/service-account-token
---
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRoleBinding
metadata:
  name: mongodb-operator-remote-binding
roleRef:
  apiGroup: rbac.authorization.k8s.io
  kind: ClusterRole
  name: cluster-admin
subjects:
- kind: ServiceAccount
  name: mongodb-operator-remote
  namespace: eksrsoppoc1d
EOF

# Wait for token to be generated
sleep 5

# Get the token
CENTRAL_TOKEN=$(kubectl get secret mongodb-operator-remote-token -n eksrsoppoc1d \
  -o jsonpath='{.data.token}' | base64 -d)
echo "Central Token: ${CENTRAL_TOKEN:0:50}..."
```

### Create Service Account and Token on Member Cluster

```bash
kubectl --kubeconfig .kube-eks/member-config apply -f - << 'EOF'
apiVersion: v1
kind: ServiceAccount
metadata:
  name: mongodb-operator-remote
  namespace: eksrsoppoc1d
---
apiVersion: v1
kind: Secret
metadata:
  name: mongodb-operator-remote-token
  namespace: eksrsoppoc1d
  annotations:
    kubernetes.io/service-account.name: mongodb-operator-remote
type: kubernetes.io/service-account-token
---
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRoleBinding
metadata:
  name: mongodb-operator-remote-binding
roleRef:
  apiGroup: rbac.authorization.k8s.io
  kind: ClusterRole
  name: cluster-admin
subjects:
- kind: ServiceAccount
  name: mongodb-operator-remote
  namespace: eksrsoppoc1d
EOF

# Wait for token to be generated
sleep 5

# Get the token
MEMBER_TOKEN=$(kubectl --kubeconfig .kube-eks/member-config get secret mongodb-operator-remote-token \
  -n eksrsoppoc1d -o jsonpath='{.data.token}' | base64 -d)
echo "Member Token: ${MEMBER_TOKEN:0:50}..."
```

---

## Step 4: Create Multi-Cluster Kubeconfig Secret

Create a kubeconfig that uses service account tokens (not IAM exec):

```bash
# Get EKS API endpoints and CA certificates
CENTRAL_ENDPOINT=$(aws eks describe-cluster --name mongodb-central --region us-east-1 \
  --query 'cluster.endpoint' --output text)
MEMBER_ENDPOINT=$(aws eks describe-cluster --name mongodb-member-1 --region us-east-1 \
  --query 'cluster.endpoint' --output text)

CENTRAL_CA=$(aws eks describe-cluster --name mongodb-central --region us-east-1 \
  --query 'cluster.certificateAuthority.data' --output text)
MEMBER_CA=$(aws eks describe-cluster --name mongodb-member-1 --region us-east-1 \
  --query 'cluster.certificateAuthority.data' --output text)

# Create combined kubeconfig with SERVICE ACCOUNT TOKENS (not IAM exec)
cat > .kube-eks/operator-kubeconfig-token.yaml << EOF
apiVersion: v1
kind: Config
clusters:
- name: mongodb-central
  cluster:
    certificate-authority-data: ${CENTRAL_CA}
    server: ${CENTRAL_ENDPOINT}
- name: mongodb-member-1
  cluster:
    certificate-authority-data: ${MEMBER_CA}
    server: ${MEMBER_ENDPOINT}
contexts:
- name: mongodb-central
  context:
    cluster: mongodb-central
    user: mongodb-central
- name: mongodb-member-1
  context:
    cluster: mongodb-member-1
    user: mongodb-member-1
current-context: mongodb-central
users:
- name: mongodb-central
  user:
    token: ${CENTRAL_TOKEN}
- name: mongodb-member-1
  user:
    token: ${MEMBER_TOKEN}
EOF

# Create the secret
export KUBECONFIG=.kube-eks/central-config
kubectl create secret generic mongodb-kubernetes-operator-multi-cluster-kubeconfig \
  -n mongodb \
  --from-file=kubeconfig=.kube-eks/operator-kubeconfig-token.yaml
```

---

## Step 5: Deploy MCK Operator via Helm

```bash
export KUBECONFIG=.kube-eks/central-config

# Deploy MCK operator with multi-cluster configuration via Helm
helm repo add mongodb https://mongodb.github.io/helm-charts
helm repo update

helm install mongodb-kubernetes-operator mongodb/mongodb-kubernetes \
  --namespace mongodb --create-namespace \
  --set operator.watchNamespace=eksrsoppoc1d \
  --set multiCluster.clusters[0]=mongodb-central \
  --set multiCluster.clusters[1]=mongodb-member-1 \
  --set multiCluster.kubeConfigSecretName=mongodb-kubernetes-operator-multi-cluster-kubeconfig

# Wait for operator to be ready
kubectl wait --for=condition=available deployment/mongodb-kubernetes-operator -n mongodb --timeout=300s
```

---

## Step 6: Deploy RBAC on Both Clusters

### Central Cluster RBAC

```bash
export KUBECONFIG=.kube-eks/central-config

kubectl apply -f - << 'EOF'
apiVersion: rbac.authorization.k8s.io/v1
kind: Role
metadata:
  name: mongodb-kubernetes-operator
  namespace: eksrsoppoc1d
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
  name: mongodb-kubernetes-operator
  namespace: eksrsoppoc1d
roleRef:
  apiGroup: rbac.authorization.k8s.io
  kind: Role
  name: mongodb-kubernetes-operator
subjects:
- kind: ServiceAccount
  name: mongodb-kubernetes-operator
  namespace: mongodb
---
apiVersion: v1
kind: ServiceAccount
metadata:
  name: mongodb-kubernetes-database-pods
  namespace: eksrsoppoc1d
---
apiVersion: v1
kind: ServiceAccount
metadata:
  name: mongodb-kubernetes-appdb
  namespace: eksrsoppoc1d
---
apiVersion: rbac.authorization.k8s.io/v1
kind: Role
metadata:
  name: mongodb-kubernetes-database-pods
  namespace: eksrsoppoc1d
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
  name: mongodb-kubernetes-database-pods
  namespace: eksrsoppoc1d
roleRef:
  apiGroup: rbac.authorization.k8s.io
  kind: Role
  name: mongodb-kubernetes-database-pods
subjects:
- kind: ServiceAccount
  name: mongodb-kubernetes-database-pods
  namespace: eksrsoppoc1d
EOF
```

### Member Cluster RBAC

```bash
kubectl --kubeconfig .kube-eks/member-config apply -f - << 'EOF'
apiVersion: v1
kind: ServiceAccount
metadata:
  name: mongodb-kubernetes-database-pods
  namespace: eksrsoppoc1d
---
apiVersion: v1
kind: ServiceAccount
metadata:
  name: mongodb-kubernetes-appdb
  namespace: eksrsoppoc1d
---
apiVersion: rbac.authorization.k8s.io/v1
kind: Role
metadata:
  name: mongodb-kubernetes-database-pods
  namespace: eksrsoppoc1d
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
  name: mongodb-kubernetes-database-pods
  namespace: eksrsoppoc1d
roleRef:
  apiGroup: rbac.authorization.k8s.io
  kind: Role
  name: mongodb-kubernetes-database-pods
subjects:
- kind: ServiceAccount
  name: mongodb-kubernetes-database-pods
  namespace: eksrsoppoc1d
EOF
```

---

## Step 7: Create Ops Manager Credentials

Replace placeholder values with your actual Ops Manager credentials.

```bash
export KUBECONFIG=.kube-eks/central-config

# Set your Ops Manager credentials
OPS_MANAGER_URL="https://YOUR_OPS_MANAGER_IP:8443"
PUBLIC_KEY="YOUR_PUBLIC_KEY"
PRIVATE_KEY="YOUR_PRIVATE_KEY"
ORG_ID="YOUR_ORG_ID"
PROJECT_NAME="eks-multi-cluster"

# Create Ops Manager API key secret
kubectl apply -f - << EOF
apiVersion: v1
kind: Secret
metadata:
  name: ops-manager-admin-key
  namespace: eksrsoppoc1d
stringData:
  publicKey: "${PUBLIC_KEY}"
  privateKey: "${PRIVATE_KEY}"
EOF

# Create Ops Manager connection ConfigMap
kubectl apply -f - << EOF
apiVersion: v1
kind: ConfigMap
metadata:
  name: ops-manager-connection
  namespace: eksrsoppoc1d
data:
  baseUrl: "${OPS_MANAGER_URL}"
  projectName: "${PROJECT_NAME}"
  orgId: "${ORG_ID}"
  sslMMSCAConfigMap: "ops-manager-ca"
  sslRequireValidMMSServerCertificates: "false"
EOF

# Copy the CA certificate from your Ops Manager server
# (adjust the path and credentials as needed for your environment)
scp -i YOUR_KEY.pem user@OPS_MANAGER_HOST:/path/to/ca.crt .kube-eks/ops-manager-ca.crt

# Create CA certificate ConfigMap
kubectl create configmap ops-manager-ca \
  --from-file=mms-ca.crt=.kube-eks/ops-manager-ca.crt \
  -n eksrsoppoc1d
```

---

## Step 8: Create Route 53 Private Hosted Zone for DNS Resolution

Pods need to resolve hostnames across clusters using Route 53.

```bash
# Create private hosted zone
HOSTED_ZONE_ID=$(aws route53 create-hosted-zone \
  --name mongodb.local \
  --vpc VPCRegion=us-east-1,VPCId=$CENTRAL_VPC \
  --caller-reference "mongodb-$(date +%s)" \
  --hosted-zone-config PrivateZone=true \
  --query 'HostedZone.Id' \
  --output text --region us-east-1)

# Remove the /hostedzone/ prefix if present
HOSTED_ZONE_ID=${HOSTED_ZONE_ID#/hostedzone/}

echo "Hosted Zone ID: $HOSTED_ZONE_ID"

# Associate with member VPC
aws route53 associate-vpc-with-hosted-zone \
  --hosted-zone-id $HOSTED_ZONE_ID \
  --vpc VPCRegion=us-east-1,VPCId=$MEMBER_VPC \
  --region us-east-1

# Save for later use
echo "export HOSTED_ZONE_ID=$HOSTED_ZONE_ID" >> .kube-eks/env-vars.sh
```

DNS records will be created after NLBs are provisioned (Step 12).

---

## Step 9: Configure CoreDNS for Route 53 Resolution

**Critical**: EKS pods use CoreDNS which doesn't automatically resolve Route 53 Private Hosted Zone records. You must configure CoreDNS to forward `.mongodb.local` queries to the VPC DNS resolver.

```bash
# Patch CoreDNS on CENTRAL cluster
kubectl --kubeconfig .kube-eks/central-config patch configmap coredns -n kube-system --type=json -p='[
  {"op": "replace", "path": "/data/Corefile", "value": ".:53 {\n    errors\n    health {\n        lameduck 5s\n    }\n    ready\n    kubernetes cluster.local in-addr.arpa ip6.arpa {\n      pods insecure\n      fallthrough in-addr.arpa ip6.arpa\n    }\n    prometheus :9153\n    forward . /etc/resolv.conf\n    cache 30\n    loop\n    reload\n    loadbalance\n}\nmongodb.local:53 {\n    errors\n    cache 30\n    forward . 169.254.169.253\n}"}
]'

# Restart CoreDNS on central cluster
kubectl --kubeconfig .kube-eks/central-config rollout restart deployment coredns -n kube-system

# Patch CoreDNS on MEMBER cluster
kubectl --kubeconfig .kube-eks/member-config patch configmap coredns -n kube-system --type=json -p='[
  {"op": "replace", "path": "/data/Corefile", "value": ".:53 {\n    errors\n    health {\n        lameduck 5s\n    }\n    ready\n    kubernetes cluster.local in-addr.arpa ip6.arpa {\n      pods insecure\n      fallthrough in-addr.arpa ip6.arpa\n    }\n    prometheus :9153\n    forward . /etc/resolv.conf\n    cache 30\n    loop\n    reload\n    loadbalance\n}\nmongodb.local:53 {\n    errors\n    cache 30\n    forward . 169.254.169.253\n}"}
]'

# Restart CoreDNS on member cluster
kubectl --kubeconfig .kube-eks/member-config rollout restart deployment coredns -n kube-system

# Wait for CoreDNS to be ready
kubectl --kubeconfig .kube-eks/central-config rollout status deployment coredns -n kube-system
kubectl --kubeconfig .kube-eks/member-config rollout status deployment coredns -n kube-system
```

This adds a forwarding rule that sends all `*.mongodb.local` DNS queries to the VPC DNS resolver (169.254.169.253), which then resolves them via Route 53 Private Hosted Zones.

---

## Step 10: Update Security Groups for NLB Access

The NLBs need to accept traffic from both VPCs:

```bash
# Allow MongoDB port 10901 from central VPC to member cluster
aws ec2 authorize-security-group-ingress \
  --group-id $MEMBER_SG \
  --protocol tcp \
  --port 10901 \
  --cidr 10.0.0.0/16 \
  --region us-east-1 || true

# Allow MongoDB port 10901 from member VPC to central cluster
aws ec2 authorize-security-group-ingress \
  --group-id $CENTRAL_SG \
  --protocol tcp \
  --port 10901 \
  --cidr 10.1.0.0/16 \
  --region us-east-1 || true

# IMPORTANT: NLBs may be internet-facing even with "internal" annotation
# Add rule for public access if needed for testing
aws ec2 authorize-security-group-ingress \
  --group-id $CENTRAL_SG \
  --protocol tcp \
  --port 10901 \
  --cidr 0.0.0.0/0 \
  --region us-east-1 || true

aws ec2 authorize-security-group-ingress \
  --group-id $MEMBER_SG \
  --protocol tcp \
  --port 10901 \
  --cidr 0.0.0.0/0 \
  --region us-east-1 || true

# Allow NodePort range for cross-cluster access (required for NLB health checks)
aws ec2 authorize-security-group-ingress \
  --group-id $CENTRAL_SG \
  --protocol tcp \
  --port 30000-32767 \
  --cidr 10.1.0.0/16 \
  --region us-east-1 || true

aws ec2 authorize-security-group-ingress \
  --group-id $MEMBER_SG \
  --protocol tcp \
  --port 30000-32767 \
  --cidr 10.0.0.0/16 \
  --region us-east-1 || true
```

---

## Step 11: Create TLS Certificates (Optional - for TLS deployments)

```bash
mkdir -p certs/mongodb-multi

# Create OpenSSL config with all SANs
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
# Internal K8s DNS names (central cluster - StatefulSet 0)
DNS.1 = mongodb-multi-rs-0-0.mongodb-multi-rs-0-svc.eksrsoppoc1d.svc.cluster.local
DNS.2 = mongodb-multi-rs-0-1.mongodb-multi-rs-0-svc.eksrsoppoc1d.svc.cluster.local
DNS.3 = mongodb-multi-rs-0-2.mongodb-multi-rs-0-svc.eksrsoppoc1d.svc.cluster.local
# Internal K8s DNS names (member cluster - StatefulSet 1)
DNS.4 = mongodb-multi-rs-1-0.mongodb-multi-rs-1-svc.eksrsoppoc1d.svc.cluster.local
DNS.5 = mongodb-multi-rs-1-1.mongodb-multi-rs-1-svc.eksrsoppoc1d.svc.cluster.local
# External domain names (Route 53)
DNS.6 = mongodb-multi-rs-0-0.central.mongodb.local
DNS.7 = mongodb-multi-rs-0-1.central.mongodb.local
DNS.8 = mongodb-multi-rs-0-2.central.mongodb.local
DNS.9 = mongodb-multi-rs-1-0.member1.mongodb.local
DNS.10 = mongodb-multi-rs-1-1.member1.mongodb.local
# Wildcard and localhost
DNS.11 = *.mongodb-multi-rs-svc.eksrsoppoc1d.svc.cluster.local
DNS.12 = *.central.mongodb.local
DNS.13 = *.member1.mongodb.local
DNS.14 = localhost
IP.1 = 127.0.0.1
EOF

# Generate server certificate signed by Ops Manager CA
openssl genrsa -out certs/mongodb-multi/mongodb.key 2048
openssl req -new -key certs/mongodb-multi/mongodb.key \
  -out certs/mongodb-multi/mongodb.csr \
  -config certs/mongodb-multi/mongodb-ext.cnf
openssl x509 -req -in certs/mongodb-multi/mongodb.csr \
  -CA .kube-eks/ops-manager-ca.crt -CAkey PATH_TO_CA_KEY \
  -CAcreateserial -out certs/mongodb-multi/mongodb.crt \
  -days 365 -extensions v3_req \
  -extfile certs/mongodb-multi/mongodb-ext.cnf

# Deploy certificates to CENTRAL cluster
export KUBECONFIG=.kube-eks/central-config

kubectl create configmap mongodb-ca \
  --from-file=ca-pem=.kube-eks/ops-manager-ca.crt \
  -n eksrsoppoc1d

kubectl create secret tls mongodb-mongodb-multi-rs-cert \
  --cert=certs/mongodb-multi/mongodb.crt \
  --key=certs/mongodb-multi/mongodb.key \
  -n eksrsoppoc1d

kubectl create secret tls mongodb-mongodb-multi-rs-agent-certs \
  --cert=certs/mongodb-multi/mongodb.crt \
  --key=certs/mongodb-multi/mongodb.key \
  -n eksrsoppoc1d

# Deploy certificates to MEMBER cluster
kubectl --kubeconfig .kube-eks/member-config create configmap mongodb-ca \
  --from-file=ca-pem=.kube-eks/ops-manager-ca.crt \
  -n eksrsoppoc1d

kubectl --kubeconfig .kube-eks/member-config create secret tls mongodb-mongodb-multi-rs-cert \
  --cert=certs/mongodb-multi/mongodb.crt \
  --key=certs/mongodb-multi/mongodb.key \
  -n eksrsoppoc1d

kubectl --kubeconfig .kube-eks/member-config create secret tls mongodb-mongodb-multi-rs-agent-certs \
  --cert=certs/mongodb-multi/mongodb.crt \
  --key=certs/mongodb-multi/mongodb.key \
  -n eksrsoppoc1d
```

---

## Step 12: Deploy MongoDBMultiCluster Resource

```bash
export KUBECONFIG=.kube-eks/central-config

kubectl apply -f - << 'EOF'
apiVersion: mongodb.com/v1
kind: MongoDBMultiCluster
metadata:
  name: mongodb-multi-rs
  namespace: eksrsoppoc1d
spec:
  version: "7.0.25-ent"
  type: ReplicaSet

  # Configure MongoDB to listen on port 10901
  additionalMongodConfig:
    net:
      port: 10901

  opsManager:
    configMapRef:
      name: ops-manager-connection

  credentials: ops-manager-admin-key

  clusterSpecList:
    - clusterName: mongodb-central
      members: 3
      externalAccess:
        externalDomain: central.mongodb.local
        externalService:
          spec:
            type: LoadBalancer
            ports:
              - port: 10901
                targetPort: 10901
          annotations:
            service.beta.kubernetes.io/aws-load-balancer-type: "nlb"
            service.beta.kubernetes.io/aws-load-balancer-scheme: "internal"

    - clusterName: mongodb-member-1
      members: 2
      externalAccess:
        externalDomain: member1.mongodb.local
        externalService:
          spec:
            type: LoadBalancer
            ports:
              - port: 10901
                targetPort: 10901
          annotations:
            service.beta.kubernetes.io/aws-load-balancer-type: "nlb"
            service.beta.kubernetes.io/aws-load-balancer-scheme: "internal"

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
        storage: "10Gi"
        storageClass: gp3

  security:
    certsSecretPrefix: mongodb
    tls:
      ca: mongodb-ca

EOF

# Watch the deployment
kubectl get mdbmc -n eksrsoppoc1d -w
```

---

## Step 13: Create Route 53 DNS Records

After the MongoDBMultiCluster resource is created, NLBs will be provisioned. Get their DNS names and create Route 53 records.

```bash
# Wait for NLBs to be provisioned (2-3 minutes)
echo "Waiting for NLBs to be provisioned..."
sleep 120

# Get NLB DNS names for central cluster
CENTRAL_NLB_0=$(kubectl --kubeconfig .kube-eks/central-config get svc mongodb-multi-rs-0-0-svc-external \
  -n eksrsoppoc1d -o jsonpath='{.status.loadBalancer.ingress[0].hostname}' 2>/dev/null)
CENTRAL_NLB_1=$(kubectl --kubeconfig .kube-eks/central-config get svc mongodb-multi-rs-0-1-svc-external \
  -n eksrsoppoc1d -o jsonpath='{.status.loadBalancer.ingress[0].hostname}' 2>/dev/null)
CENTRAL_NLB_2=$(kubectl --kubeconfig .kube-eks/central-config get svc mongodb-multi-rs-0-2-svc-external \
  -n eksrsoppoc1d -o jsonpath='{.status.loadBalancer.ingress[0].hostname}' 2>/dev/null)

# Get NLB DNS names for member cluster
MEMBER_NLB_0=$(kubectl --kubeconfig .kube-eks/member-config get svc mongodb-multi-rs-1-0-svc-external \
  -n eksrsoppoc1d -o jsonpath='{.status.loadBalancer.ingress[0].hostname}' 2>/dev/null)
MEMBER_NLB_1=$(kubectl --kubeconfig .kube-eks/member-config get svc mongodb-multi-rs-1-1-svc-external \
  -n eksrsoppoc1d -o jsonpath='{.status.loadBalancer.ingress[0].hostname}' 2>/dev/null)

echo "Central NLBs: $CENTRAL_NLB_0, $CENTRAL_NLB_1, $CENTRAL_NLB_2"
echo "Member NLBs: $MEMBER_NLB_0, $MEMBER_NLB_1"

# Create Route 53 records
aws route53 change-resource-record-sets --hosted-zone-id $HOSTED_ZONE_ID --change-batch '{
  "Changes": [
    {"Action": "UPSERT", "ResourceRecordSet": {"Name": "mongodb-multi-rs-0-0.central.mongodb.local", "Type": "CNAME", "TTL": 60, "ResourceRecords": [{"Value": "'$CENTRAL_NLB_0'"}]}},
    {"Action": "UPSERT", "ResourceRecordSet": {"Name": "mongodb-multi-rs-0-1.central.mongodb.local", "Type": "CNAME", "TTL": 60, "ResourceRecords": [{"Value": "'$CENTRAL_NLB_1'"}]}},
    {"Action": "UPSERT", "ResourceRecordSet": {"Name": "mongodb-multi-rs-0-2.central.mongodb.local", "Type": "CNAME", "TTL": 60, "ResourceRecords": [{"Value": "'$CENTRAL_NLB_2'"}]}},
    {"Action": "UPSERT", "ResourceRecordSet": {"Name": "mongodb-multi-rs-1-0.member1.mongodb.local", "Type": "CNAME", "TTL": 60, "ResourceRecords": [{"Value": "'$MEMBER_NLB_0'"}]}},
    {"Action": "UPSERT", "ResourceRecordSet": {"Name": "mongodb-multi-rs-1-1.member1.mongodb.local", "Type": "CNAME", "TTL": 60, "ResourceRecords": [{"Value": "'$MEMBER_NLB_1'"}]}}
  ]
}' --region us-east-1

echo "Route 53 DNS records created"
```

---

## Step 14: Verify Deployment

```bash
export KUBECONFIG=.kube-eks/central-config

# Check MongoDBMultiCluster status (should be Running)
kubectl get mdbmc -n eksrsoppoc1d

# Check pods on central cluster
kubectl get pods -n eksrsoppoc1d

# Check pods on member cluster
kubectl --kubeconfig .kube-eks/member-config get pods -n eksrsoppoc1d

# Check services and NLBs
kubectl get svc -n eksrsoppoc1d

# Check operator logs
kubectl logs -n mongodb deployment/mongodb-kubernetes-operator --tail=50
```

Expected output:
```
NAME               PHASE     AGE
mongodb-multi-rs   Running   10m

NAME                   READY   STATUS    RESTARTS   AGE
mongodb-multi-rs-0-0   1/1     Running   0          10m
mongodb-multi-rs-0-1   1/1     Running   0          9m
mongodb-multi-rs-0-2   1/1     Running   0          8m

NAME                   READY   STATUS    RESTARTS   AGE
mongodb-multi-rs-1-0   1/1     Running   0          10m
mongodb-multi-rs-1-1   1/1     Running   0          9m
```

---

## Step 15: Create MongoDB Users (Optional)

```bash
export KUBECONFIG=.kube-eks/central-config

kubectl apply -f - << 'EOF'
apiVersion: v1
kind: Secret
metadata:
  name: mongodb-admin-password
  namespace: eksrsoppoc1d
stringData:
  password: "YourSecurePassword123!"
---
apiVersion: mongodb.com/v1
kind: MongoDBUser
metadata:
  name: admin
  namespace: eksrsoppoc1d
spec:
  passwordSecretKeyRef:
    name: mongodb-admin-password
    key: password
  username: admin
  db: admin
  mongodbResourceRef:
    name: mongodb-multi-rs
    namespace: eksrsoppoc1d
  roles:
    - db: admin
      name: root
EOF
```

---

## Connection Instructions

### From Within AWS VPC

Connect from an EC2 instance or pod within the VPC:

```bash
# Using SCRAM authentication (via Route 53 DNS)
mongosh "mongodb://mongodb-multi-rs-0-0.central.mongodb.local:10901,mongodb-multi-rs-0-1.central.mongodb.local:10901,mongodb-multi-rs-0-2.central.mongodb.local:10901,mongodb-multi-rs-1-0.member1.mongodb.local:10901,mongodb-multi-rs-1-1.member1.mongodb.local:10901/?replicaSet=mongodb-multi-rs&tls=true&tlsCAFile=./certs/ca.crt" \
  --username admin \
  --authenticationDatabase admin
```

### From Outside AWS (Requires Public NLBs or VPN)

If NLBs are internet-facing, you can connect using NLB DNS names directly:

```bash
# Get NLB DNS names
kubectl get svc -n eksrsoppoc1d -o wide

# Connect using NLB DNS names
mongosh "mongodb://NLB_DNS_NAME_0:10901,NLB_DNS_NAME_1:10901,.../?replicaSet=mongodb-multi-rs&tls=true&tlsCAFile=./certs/ca.crt" \
  --username admin \
  --authenticationDatabase admin
```

---

## Cleanup

```bash
# Delete MongoDB resources
export KUBECONFIG=.kube-eks/central-config
kubectl delete mdbmc mongodb-multi-rs -n eksrsoppoc1d
sleep 60

# Delete Route 53 records and hosted zone
aws route53 list-resource-record-sets --hosted-zone-id $HOSTED_ZONE_ID --query 'ResourceRecordSets[?Type!=`NS` && Type!=`SOA`]' --output json > /tmp/records.json
# (delete records first, then delete zone)
aws route53 delete-hosted-zone --id $HOSTED_ZONE_ID

# Clean local files
rm -rf certs/mongodb-multi/ .kube-eks/

# For full infrastructure cleanup, see EKS_PROVISIONING.md
```

---

## Troubleshooting

### Operator Cannot Access Member Cluster

**Symptom**: Operator logs show connection errors to member cluster.

**Cause**: The operator is using IAM exec credentials which don't work inside pods.

**Solution**: Use service account tokens instead of IAM exec. See Step 3-4.

```bash
# Check if the kubeconfig uses tokens (correct) or exec (incorrect)
kubectl get secret mongodb-kubernetes-operator-multi-cluster-kubeconfig -n mongodb -o jsonpath='{.data.kubeconfig}' | base64 -d | grep -E "(token:|exec:)"
```

### MongoDBMultiCluster Stuck in Pending

**Symptom**: Resource stays in Pending phase.

**Common Causes**:
1. Ops Manager not accessible from pods
2. API credentials incorrect
3. Project already has conflicting configuration

**Solutions**:
```bash
# Check operator logs
kubectl logs -n mongodb deployment/mongodb-kubernetes-operator --tail=100

# Verify Ops Manager connectivity from a pod
kubectl run -it --rm debug --image=curlimages/curl --restart=Never -- \
  curl -sk https://OPS_MANAGER_IP:8443/api/public/v1.0/groups

# Reset Ops Manager automation config via API if needed
```

### "Replica set id already being used by unmanaged replica set"

**Symptom**: Error in operator logs about duplicate replica set.

**Cause**: Previous deployment left state in Ops Manager.

**Solution**: Reset Ops Manager automation config via API or delete and recreate the project.

### Cross-Cluster Connectivity Issues

**Symptom**: Pods can't reach MongoDB on other cluster.

**Checks**:
```bash
# 1. Verify VPC peering is active
aws ec2 describe-vpc-peering-connections --vpc-peering-connection-ids $PEERING_ID

# 2. Verify route tables have cross-VPC routes
aws ec2 describe-route-tables --filters "Name=vpc-id,Values=$CENTRAL_VPC" --region us-east-1

# 3. Verify security groups allow MongoDB port
aws ec2 describe-security-groups --group-ids $CENTRAL_SG --region us-east-1

# 4. Test connectivity from a pod
kubectl run -it --rm debug --image=busybox --restart=Never -- \
  nc -vz mongodb-multi-rs-1-0.member1.mongodb.local 10901
```

### NLB Not Provisioning

**Symptom**: Service External-IP shows `<pending>`.

**Checks**:
```bash
# Check service events
kubectl describe svc mongodb-multi-rs-0-0-svc-external -n eksrsoppoc1d

# Check if AWS Load Balancer Controller is installed (not required for in-tree provisioner)
kubectl get pods -n kube-system | grep aws-load-balancer
```

### DNS Resolution Failing

**Symptom**: Pods can't resolve `*.mongodb.local` hostnames.

**Checks**:
```bash
# Test DNS from a pod
kubectl run -it --rm debug --image=busybox --restart=Never -- \
  nslookup mongodb-multi-rs-0-0.central.mongodb.local

# Verify Route 53 records
aws route53 list-resource-record-sets --hosted-zone-id $HOSTED_ZONE_ID

# Verify VPC is associated with hosted zone
aws route53 get-hosted-zone --id $HOSTED_ZONE_ID
```

### TLS Certificate Errors

**Symptom**: MongoDB pods fail with TLS errors.

**Checks**:
```bash
# Verify certificate SANs cover all hostnames
openssl x509 -in certs/mongodb-multi/mongodb.crt -text -noout | grep -A1 "Subject Alternative Name"

# Check if cert secret exists
kubectl get secret mongodb-mongodb-multi-rs-cert -n eksrsoppoc1d
```

---

## References

- [MongoDB Controllers for Kubernetes (MCK) Documentation](https://www.mongodb.com/docs/kubernetes/current/)
- [Multi-Cluster Overview](https://www.mongodb.com/docs/kubernetes/current/multi-cluster-overview/)
- [Amazon EKS Documentation](https://docs.aws.amazon.com/eks/)
- [eksctl Documentation](https://eksctl.io/)
- [AWS VPC Peering](https://docs.aws.amazon.com/vpc/latest/peering/)
- [AWS Route 53 Private Hosted Zones](https://docs.aws.amazon.com/Route53/latest/DeveloperGuide/hosted-zones-private.html)
