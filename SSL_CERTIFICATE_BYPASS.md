# SSL Certificate Verification Bypass for Testing

This document describes how to bypass SSL certificate verification when deploying MongoDB with the Enterprise Kubernetes Operator. This is useful for testing environments where certificates may have hostname/IP mismatches.

> **WARNING**: Disabling SSL certificate verification is **INSECURE** and should **NEVER** be used in production. It makes connections susceptible to man-in-the-middle attacks.

## Problem Description

When deploying MongoDB to Kubernetes with the Enterprise Operator connecting to Ops Manager over HTTPS, you may encounter certificate validation errors such as:

```
x509: cannot validate certificate for 192.168.65.254 because it doesn't contain any IP SANs
```

This occurs when:
- The Ops Manager TLS certificate contains only hostnames (DNS SANs), not IP addresses
- The Kubernetes cluster connects to Ops Manager using an IP address
- Or vice versa - certificate has IPs but connection uses hostname

## Solution Overview

To bypass certificate verification, three components must be configured:

| Component | Setting | Purpose |
|-----------|---------|---------|
| ConfigMap | `sslRequireValidMMSServerCertificates: "false"` | Operator's connection to Ops Manager |
| MongoDB CRD | `agent.startupOptions.tlsRequireValidMMSServerCertificates: "false"` | Automation agent's connection |
| MongoDB CRD | `env.SSL_REQUIRE_VALID_MMS_CERTIFICATES: "false"` | Agent download script |

## Configuration Files

### 1. Ops Manager Connection ConfigMap

```yaml
# k8s/ops-manager-configmap.yaml
apiVersion: v1
kind: ConfigMap
metadata:
  name: ops-manager-connection
  namespace: mongodb-rs
data:
  # Ops Manager URL - can use IP address even if cert has hostnames
  baseUrl: "https://192.168.65.254:8443"

  # Project configuration
  projectName: "Default"
  orgId: "YOUR_ORG_ID"

  # IMPORTANT: Set to "false" to skip certificate validation
  # This allows the operator to connect despite certificate mismatches
  sslRequireValidMMSServerCertificates: "false"

  # NOTE: When sslRequireValidMMSServerCertificates is "false",
  # you can omit sslMMSCAConfigMap entirely
  # sslMMSCAConfigMap: "ops-manager-ca"  # Not needed when skipping validation
```

### 2. MongoDB ReplicaSet Configuration

```yaml
# k8s/mongodb-replicaset.yaml
apiVersion: mongodb.com/v1
kind: MongoDB
metadata:
  name: mongodb-rs
  namespace: mongodb-rs
spec:
  members: 3
  version: "7.0.25-ent"
  type: ReplicaSet

  # Ops Manager connection reference
  opsManager:
    configMapRef:
      name: ops-manager-connection
  credentials: ops-manager-admin-key

  # Agent Configuration - controls automation agent behavior
  agent:
    startupOptions:
      # Skip TLS certificate validation for automation agent
      # This is passed as -tlsRequireValidMMSServerCertificates=false flag
      tlsRequireValidMMSServerCertificates: "false"

  persistent: true

  # Pod Configuration
  podSpec:
    podTemplate:
      spec:
        containers:
          - name: mongodb-enterprise-database
            # Environment variables for the container
            env:
              # CRITICAL: This env var controls the agent-launcher script
              # which downloads the MongoDB agent binary from Ops Manager
              # Without this, the download will fail with certificate errors
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
```

## How Each Setting Works

### 1. ConfigMap: `sslRequireValidMMSServerCertificates`

This setting is read by the **MongoDB Enterprise Kubernetes Operator** when it connects to Ops Manager to:
- Verify credentials
- Create/read projects
- Push automation configuration

```yaml
data:
  sslRequireValidMMSServerCertificates: "false"
```

### 2. Agent StartupOptions: `tlsRequireValidMMSServerCertificates`

This setting is passed to the **MongoDB Automation Agent** as a command-line flag. The agent uses this when:
- Connecting to Ops Manager to receive configuration
- Sending monitoring data
- Reporting status

```yaml
agent:
  startupOptions:
    tlsRequireValidMMSServerCertificates: "false"
```

The operator converts this to the environment variable `AGENT_FLAGS`:
```
AGENT_FLAGS=-logFile=/var/log/mongodb-mms-automation/automation-agent.log,-tlsRequireValidMMSServerCertificates=false,
```

### 3. Environment Variable: `SSL_REQUIRE_VALID_MMS_CERTIFICATES`

This environment variable is read by the **agent-launcher script** (`/opt/scripts/agent-launcher.sh`) inside the MongoDB pod. The script downloads the MongoDB agent binary from Ops Manager before starting the automation agent.

```yaml
env:
  - name: SSL_REQUIRE_VALID_MMS_CERTIFICATES
    value: "false"
```

When set to `"false"`, the script uses `curl -k` (insecure) to download the agent.

**This is the most commonly missed setting** - without it, you'll see errors like:
```json
{"logType":"agent-launcher-script","contents":"Downloading a Mongodb Agent from https://192.168.65.254:8443"}
{"logType":"agent-launcher-script","contents":"Error while downloading the Mongodb agent"}
```

## Using the Deployment Script

The `deploy_mongodb_k8s.py` script supports a `--ssl-skip-verify` flag that configures all three settings automatically:

```bash
# Deploy with SSL verification disabled
python deploy_mongodb_k8s.py \
  --skip-preflight \
  --ops-manager-url "https://192.168.65.254:8443" \
  --ssl-skip-verify \
  --wait

# The script will:
# 1. Set sslRequireValidMMSServerCertificates: "false" in ConfigMap
# 2. Set tlsRequireValidMMSServerCertificates: "false" in agent.startupOptions
# 3. Set SSL_REQUIRE_VALID_MMS_CERTIFICATES: "false" env var on pods
# 4. Skip creating the CA ConfigMap (not needed when skipping validation)
```

## Verification

After deployment, verify the settings are applied:

### Check ConfigMap
```bash
kubectl get configmap ops-manager-connection -n mongodb-rs -o yaml
```

Expected output should include:
```yaml
data:
  sslRequireValidMMSServerCertificates: "false"
```

### Check Pod Environment Variables
```bash
kubectl get pod mongodb-rs-0 -n mongodb-rs -o yaml | grep -A2 "SSL_REQUIRE"
```

Expected output:
```yaml
- name: SSL_REQUIRE_VALID_MMS_CERTIFICATES
  value: "false"
```

### Check Agent Flags
```bash
kubectl get pod mongodb-rs-0 -n mongodb-rs -o yaml | grep "AGENT_FLAGS" -A1
```

Expected output should include:
```yaml
- name: AGENT_FLAGS
  value: -logFile=...,-tlsRequireValidMMSServerCertificates=false,
```

## Troubleshooting

### Error: "Error while downloading the Mongodb agent"

**Cause**: `SSL_REQUIRE_VALID_MMS_CERTIFICATES` environment variable is not set or set to `"true"`.

**Solution**: Add the environment variable to the pod spec:
```yaml
podSpec:
  podTemplate:
    spec:
      containers:
        - name: mongodb-enterprise-database
          env:
            - name: SSL_REQUIRE_VALID_MMS_CERTIFICATES
              value: "false"
```

### Error: "x509: cannot validate certificate for IP because it doesn't contain any IP SANs"

**Cause**: The certificate doesn't include the IP address being used to connect.

**Solution**: Either:
1. Use this SSL bypass configuration (for testing only)
2. Regenerate the certificate with the correct IP SANs
3. Use a hostname that matches the certificate's DNS SANs

### Error: Operator fails to connect to Ops Manager

**Cause**: `sslRequireValidMMSServerCertificates` not set in ConfigMap.

**Solution**: Ensure the ConfigMap includes:
```yaml
data:
  sslRequireValidMMSServerCertificates: "false"
```

## Production Recommendations

For production environments, **do not use these bypass settings**. Instead:

1. **Generate proper certificates** with all required SANs:
   ```
   DNS.1 = ops-manager.example.com
   DNS.2 = host.docker.internal
   IP.1 = 192.168.65.254
   IP.2 = 127.0.0.1
   ```

2. **Use consistent addressing** - either always use hostnames or always use IPs, matching your certificate.

3. **Use a proper CA** - either a trusted public CA or a well-managed internal CA with proper certificate distribution.

4. **Configure the CA ConfigMap** properly:
   ```yaml
   data:
     sslMMSCAConfigMap: "ops-manager-ca"
     sslRequireValidMMSServerCertificates: "true"  # or omit (defaults to true)
   ```

## References

- [MongoDB Enterprise Kubernetes Operator - Create Project Using ConfigMap](https://www.mongodb.com/docs/kubernetes-operator/current/tutorial/create-project-using-configmap/)
- [MongoDB Agent Settings - tlsRequireValidMMSServerCertificates](https://www.mongodb.com/docs/ops-manager/current/reference/mongodb-agent-settings/)
- [Configure MongoDB Agent for TLS](https://www.mongodb.com/docs/ops-manager/current/tutorial/configure-mongodb-agent-for-tls/)
