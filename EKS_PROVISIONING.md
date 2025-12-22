# AWS EKS Multi-Cluster Provisioning Guide

This guide covers the infrastructure provisioning steps for deploying MongoDB across multiple AWS EKS clusters. Complete these steps before proceeding with the MongoDB deployment in [MANUAL_DEPLOYMENT_EKS_MULTI.md](MANUAL_DEPLOYMENT_EKS_MULTI.md).

## Architecture Overview

```
                    ┌─────────────────────────────────────────────────────────┐
                    │                    AWS Cloud                             │
                    │                                                          │
┌──────────────┐    │  ┌─────────────────────┐    ┌─────────────────────┐     │
│ Ops Manager  │◄───┼──│   EKS Central       │    │   EKS Member        │     │
│ (On-prem or  │    │  │   Cluster           │    │   Cluster           │     │
│  AWS)        │    │  │  ┌───────────────┐  │    │  ┌───────────────┐  │     │
└──────────────┘    │  │  │ MongoDB       │  │    │  │ MongoDB       │  │     │
                    │  │  │ Operator      │  │    │  │ Pods (2)      │  │     │
                    │  │  │ + Pods (3)    │  │    │  └───────────────┘  │     │
                    │  │  └───────────────┘  │    │                     │     │
                    │  │         │           │    │          │          │     │
                    │  │         ▼           │    │          ▼          │     │
                    │  │  ┌───────────────┐  │    │  ┌───────────────┐  │     │
                    │  │  │ NLB/NodePort  │  │    │  │ NLB/NodePort  │  │     │
                    │  │  │ :30100-30102  │  │    │  │ :30200-30201  │  │     │
                    │  │  └───────────────┘  │    │  └───────────────┘  │     │
                    │  └─────────┬───────────┘    └──────────┬──────────┘     │
                    │            │                           │                 │
                    │            └───────────┬───────────────┘                 │
                    │                        │                                 │
                    │            ┌───────────▼───────────┐                     │
                    │            │   VPC Peering or      │                     │
                    │            │   Transit Gateway     │                     │
                    │            └───────────────────────┘                     │
                    └─────────────────────────────────────────────────────────┘
```

## Prerequisites

### Required Tools

1. **AWS CLI** v2.x configured with appropriate credentials
2. **eksctl** v0.150+ for EKS cluster management
3. **kubectl** v1.28+
4. **OpenSSL** for TLS certificate generation
5. **Helm** v3.x (optional, for cert-manager)

### AWS Requirements

- IAM permissions to create EKS clusters, VPCs, security groups, and load balancers
- Two AWS regions or availability zones (for true multi-region deployment)
- VPC peering or Transit Gateway configured between clusters (if cross-VPC)

### Ops Manager Requirements

- MongoDB Ops Manager accessible from EKS clusters
- API credentials (public key, private key, org ID)
- CA certificate if using HTTPS with self-signed certificates

### Verify Prerequisites

```bash
# Check AWS CLI
aws --version
aws sts get-caller-identity

# Check eksctl
eksctl version

# Check kubectl
kubectl version --client

# Check OpenSSL
openssl version
```

## Network Planning

### IP Address Ranges

Plan non-overlapping CIDR ranges for VPC peering:

| Cluster | VPC CIDR | Pod CIDR | Service CIDR |
|---------|----------|----------|--------------|
| Central | 10.0.0.0/16 | 10.0.0.0/16 | 172.20.0.0/16 |
| Member | 10.1.0.0/16 | 10.1.0.0/16 | 172.21.0.0/16 |

### External Access Strategy

Choose one of the following for external MongoDB access:

| Option | Pros | Cons |
|--------|------|------|
| **Network Load Balancer (NLB)** | Static IPs, TCP passthrough, TLS termination at MongoDB | Higher cost |
| **NodePort + Security Groups** | Lower cost, simple | Requires node IP management |
| **AWS PrivateLink** | Private connectivity, no public exposure | Complex setup |

This guide uses **NodePort** for the minimal test configuration and **NLB** for production.

---

## Step 1: Create EKS Clusters

### 1.1 Create Central Cluster

```bash
# Create cluster configuration
cat > eks-central-config.yaml << 'EOF'
apiVersion: eksctl.io/v1alpha5
kind: ClusterConfig

metadata:
  name: mongodb-central
  region: us-east-1
  version: "1.29"

vpc:
  cidr: 10.0.0.0/16
  nat:
    gateway: Single

managedNodeGroups:
  - name: mongodb-nodes
    instanceType: t3.large       # 2 vCPU, 8GB RAM - minimum for MongoDB
    desiredCapacity: 2           # 2 nodes for central (operator + 3 MongoDB pods)
    minSize: 1
    maxSize: 3
    volumeSize: 30
    volumeType: gp3
    labels:
      role: mongodb
    iam:
      withAddonPolicies:
        ebs: true

iam:
  withOIDC: true
EOF

# Create the cluster (takes 15-20 minutes)
eksctl create cluster -f eks-central-config.yaml

# Verify cluster
kubectl get nodes
```

### 1.2 Create Member Cluster

```bash
# Create member cluster - same region to simplify VPC peering
cat > eks-member-config.yaml << 'EOF'
apiVersion: eksctl.io/v1alpha5
kind: ClusterConfig

metadata:
  name: mongodb-member-1
  region: us-east-1
  version: "1.29"

vpc:
  cidr: 10.1.0.0/16
  nat:
    gateway: Single

managedNodeGroups:
  - name: mongodb-nodes
    instanceType: t3.large       # 2 vCPU, 8GB RAM
    desiredCapacity: 1           # 1 node for member (2 MongoDB pods)
    minSize: 1
    maxSize: 2
    volumeSize: 30
    volumeType: gp3
    labels:
      role: mongodb
    iam:
      withAddonPolicies:
        ebs: true

iam:
  withOIDC: true
EOF

# Create the cluster
eksctl create cluster -f eks-member-config.yaml

# Verify cluster
kubectl get nodes
```

### 1.3 Save Kubeconfig Files

```bash
mkdir -p .kube-eks

# Get kubeconfig for central cluster
aws eks update-kubeconfig --name mongodb-central --region us-east-1 --kubeconfig .kube-eks/central-config

# Get kubeconfig for member cluster
aws eks update-kubeconfig --name mongodb-member-1 --region us-east-1 --kubeconfig .kube-eks/member-config

# Verify both clusters
kubectl --kubeconfig .kube-eks/central-config get nodes
kubectl --kubeconfig .kube-eks/member-config get nodes
```

---

## Step 2: Configure Cross-Cluster Networking

### 2.1 Set Up VPC Peering

```bash
# Get VPC IDs
CENTRAL_VPC=$(aws eks describe-cluster --name mongodb-central --region us-east-1 \
  --query 'cluster.resourcesVpcConfig.vpcId' --output text)
MEMBER_VPC=$(aws eks describe-cluster --name mongodb-member-1 --region us-east-1 \
  --query 'cluster.resourcesVpcConfig.vpcId' --output text)

echo "Central VPC: $CENTRAL_VPC"
echo "Member VPC: $MEMBER_VPC"

# Create VPC peering connection (from central to member)
PEERING_ID=$(aws ec2 create-vpc-peering-connection \
  --vpc-id $CENTRAL_VPC \
  --peer-vpc-id $MEMBER_VPC \
  --peer-region us-east-1 \
  --query 'VpcPeeringConnection.VpcPeeringConnectionId' \
  --output text \
  --region us-east-1)

echo "Peering Connection ID: $PEERING_ID"

# Accept the peering connection (in member region)
aws ec2 accept-vpc-peering-connection \
  --vpc-peering-connection-id $PEERING_ID \
  --region us-east-1
```

### 2.2 Update Route Tables

```bash
# Get route table IDs for central cluster
CENTRAL_ROUTE_TABLES=$(aws ec2 describe-route-tables \
  --filters "Name=vpc-id,Values=$CENTRAL_VPC" \
  --query 'RouteTables[*].RouteTableId' \
  --output text \
  --region us-east-1)

# Get route table IDs for member cluster
MEMBER_ROUTE_TABLES=$(aws ec2 describe-route-tables \
  --filters "Name=vpc-id,Values=$MEMBER_VPC" \
  --query 'RouteTables[*].RouteTableId' \
  --output text \
  --region us-east-1)

# Add routes from central to member (10.1.0.0/16)
for rt in $CENTRAL_ROUTE_TABLES; do
  aws ec2 create-route \
    --route-table-id $rt \
    --destination-cidr-block 10.1.0.0/16 \
    --vpc-peering-connection-id $PEERING_ID \
    --region us-east-1 || true
done

# Add routes from member to central (10.0.0.0/16)
for rt in $MEMBER_ROUTE_TABLES; do
  aws ec2 create-route \
    --route-table-id $rt \
    --destination-cidr-block 10.0.0.0/16 \
    --vpc-peering-connection-id $PEERING_ID \
    --region us-east-1 || true
done
```

### 2.3 Update Security Groups

```bash
# Get node security groups
CENTRAL_SG=$(aws eks describe-cluster --name mongodb-central --region us-east-1 \
  --query 'cluster.resourcesVpcConfig.clusterSecurityGroupId' --output text)
MEMBER_SG=$(aws eks describe-cluster --name mongodb-member-1 --region us-east-1 \
  --query 'cluster.resourcesVpcConfig.clusterSecurityGroupId' --output text)

# Allow MongoDB traffic (port 10901) from member VPC to central
aws ec2 authorize-security-group-ingress \
  --group-id $CENTRAL_SG \
  --protocol tcp \
  --port 10901 \
  --cidr 10.1.0.0/16 \
  --region us-east-1

# Allow MongoDB traffic from central VPC to member
aws ec2 authorize-security-group-ingress \
  --group-id $MEMBER_SG \
  --protocol tcp \
  --port 10901 \
  --cidr 10.0.0.0/16 \
  --region us-east-1

# Allow NodePort range (30000-32767) for cross-cluster access
aws ec2 authorize-security-group-ingress \
  --group-id $CENTRAL_SG \
  --protocol tcp \
  --port 30000-32767 \
  --cidr 10.1.0.0/16 \
  --region us-east-1

aws ec2 authorize-security-group-ingress \
  --group-id $MEMBER_SG \
  --protocol tcp \
  --port 30000-32767 \
  --cidr 10.0.0.0/16 \
  --region us-east-1
```

---

## Step 3: Install EBS CSI Driver

Required for persistent storage on EKS.

```bash
# Central cluster
eksctl create addon --name aws-ebs-csi-driver --cluster mongodb-central --region us-east-1 --force

# Member cluster
eksctl create addon --name aws-ebs-csi-driver --cluster mongodb-member-1 --region us-east-1 --force

# Create storage class on both clusters
for config in .kube-eks/central-config .kube-eks/member-config; do
  kubectl --kubeconfig $config apply -f - << 'EOF'
apiVersion: storage.k8s.io/v1
kind: StorageClass
metadata:
  name: gp3
  annotations:
    storageclass.kubernetes.io/is-default-class: "true"
provisioner: ebs.csi.aws.com
parameters:
  type: gp3
  encrypted: "true"
volumeBindingMode: WaitForFirstConsumer
allowVolumeExpansion: true
EOF
done
```

---

## Environment Variables Summary

After completing the provisioning steps, you should have the following environment variables set. Save these for use in the MongoDB deployment guide:

```bash
# VPC IDs
echo "CENTRAL_VPC=$CENTRAL_VPC"
echo "MEMBER_VPC=$MEMBER_VPC"

# Security Groups
echo "CENTRAL_SG=$CENTRAL_SG"
echo "MEMBER_SG=$MEMBER_SG"

# VPC Peering
echo "PEERING_ID=$PEERING_ID"
```

You can save these to a file for later use:

```bash
cat > .kube-eks/env-vars.sh << EOF
export CENTRAL_VPC=$CENTRAL_VPC
export MEMBER_VPC=$MEMBER_VPC
export CENTRAL_SG=$CENTRAL_SG
export MEMBER_SG=$MEMBER_SG
export PEERING_ID=$PEERING_ID
EOF
```

---

## Verification Checklist

Before proceeding to MongoDB deployment, verify:

- [ ] Both EKS clusters are running and accessible
- [ ] VPC peering connection is active
- [ ] Route tables have cross-VPC routes
- [ ] Security groups allow MongoDB traffic (port 10901) and NodePort range
- [ ] EBS CSI driver is installed on both clusters
- [ ] gp3 StorageClass is set as default on both clusters
- [ ] Kubeconfig files are saved in `.kube-eks/`

```bash
# Quick verification commands
kubectl --kubeconfig .kube-eks/central-config get nodes
kubectl --kubeconfig .kube-eks/member-config get nodes
kubectl --kubeconfig .kube-eks/central-config get sc
kubectl --kubeconfig .kube-eks/member-config get sc
aws ec2 describe-vpc-peering-connections --vpc-peering-connection-ids $PEERING_ID --query 'VpcPeeringConnections[0].Status.Code' --output text
```

---

## Cleanup

To delete the EKS infrastructure:

```bash
# Delete EKS clusters (this also deletes node groups and associated resources)
eksctl delete cluster --name mongodb-central --region us-east-1
eksctl delete cluster --name mongodb-member-1 --region us-east-1

# Delete VPC peering (if clusters deletion doesn't remove it)
aws ec2 delete-vpc-peering-connection --vpc-peering-connection-id $PEERING_ID --region us-east-1

# Clean local files
rm -rf .kube-eks/
rm -f eks-central-config.yaml eks-member-config.yaml
```

---

## Next Steps

Once the EKS infrastructure is provisioned, proceed to deploy MongoDB:

**[MANUAL_DEPLOYMENT_EKS_MULTI.md](MANUAL_DEPLOYMENT_EKS_MULTI.md)** - Deploy MongoDB Enterprise Kubernetes Operator and multi-cluster replica set

---

## References

- [Amazon EKS Documentation](https://docs.aws.amazon.com/eks/)
- [eksctl Documentation](https://eksctl.io/)
- [AWS VPC Peering](https://docs.aws.amazon.com/vpc/latest/peering/)
- [AWS EBS CSI Driver](https://docs.aws.amazon.com/eks/latest/userguide/ebs-csi.html)
