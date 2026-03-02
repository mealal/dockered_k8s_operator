# MongoDB Search & Vector Search - Deployment Options Review

A comprehensive review of all available methods for deploying MongoDB Atlas Search and Vector Search — locally, on remote servers (EC2/VMs), and alongside Ops Manager. This document covers requirements, trade-offs, and decision guidance without implementation details.

## Background

### What Is mongot?

MongoDB Atlas Search and Vector Search are powered by **mongot** — a Java-based search engine built on Apache Lucene. It provides:

- **Full-text search** via `$search` and `$searchMeta` aggregation stages
- **Vector/semantic search** via `$vectorSearch` aggregation stage (ANN and ENN)
- Autocomplete, fuzzy matching, faceting, scoring, synonyms, custom analyzers, hybrid search

### How mongot Works

- `mongot` runs as a **separate process** from `mongod` (the database engine)
- Communication between `mongod` and `mongot` uses **gRPC on port 27027** (configurable)
- **Clients never connect to mongot directly** — all queries go through `mongod`, which proxies search requests to `mongot` via gRPC
- `mongot` **tails Change Streams** from `mongod` to build and maintain Lucene indexes asynchronously
- **Replica set is always required** (even single-node) because Change Streams depend on the oplog
- Search indexes are **eventually consistent** — there is always a delay between document writes and search index updates

### Search Index Management

Search indexes are managed through **mongosh**, **MongoDB Compass** (v1.40+), or **driver APIs** (`createSearchIndex()`, `listSearchIndexes()`, `updateSearchIndex()`, `dropSearchIndex()`). There is no centralized management plane for search indexes in any self-managed deployment option.

### Supported Deployment Paths — Component Version Matrix

| Deployment Path | mongod | mongot | K8s Operator | Ops Manager (for mongod) | Options |
|----------------|--------|--------|-------------|------------------------|---------|
| `mongodb-atlas-local` Docker image | Bundled | Bundled | Not required | Not applicable | 1 |
| Standalone `mongot` binary | Community 8.2+ | `mongodb-community-search:0.53.1`+ | Not required | Ops Manager 8.0.13+ | 2 |
| K8s Operator + Enterprise | Enterprise 8.0.10+ | Operator-managed | MongoDB Controllers for K8s v1.4+ | Ops Manager 8.0.0+ (for 8.0.x), 8.0.13+ (for 8.2) | 3 |
| K8s Operator + Community | Community 8.2+ | `mongodb-community-search:0.53.1`+ | MongoDB Controllers for K8s v1.5+/v1.6 | Ops Manager 8.0.13+ | 4 |

The standalone `mongot` binary is available for Community Edition only. Enterprise `mongot` deployments require the Kubernetes Operator.

---

## Options at a Glance

| # | Option | Min MongoDB Version | Production Ready | License | Maturity | Platform |
|---|--------|-------------------|-----------------|---------|----------|----------|
| 1 | `mongodb-atlas-local` (Docker / Atlas CLI / Compose / Testcontainers) | Bundled | No (dev/test) | Proprietary | GA | macOS, Windows, Linux |
| 2 | Community 8.2+ with standalone `mongot` | Community 8.2+ | Not yet | SSPL v1 | Public Preview | Linux only |
| 3 | Enterprise Server + Kubernetes Operator | Enterprise 8.0.10+ | Not yet | Enterprise subscription | Public Preview | Linux only |
| 4 | Community 8.2+ with Kubernetes Operator | Community 8.2+ | Not yet | SSPL v1 | Public Preview | Linux only |

All options provide identical search feature parity: `$search`, `$searchMeta`, `$vectorSearch`, autocomplete, fuzzy matching, faceting, scoring, synonyms, custom analyzers, and hybrid search.

Any of the above can also be deployed on a remote server (EC2, VM, bare metal) — see the [Remote Deployment](#remote-deployment-ec2-vms-bare-metal) section.

---

## Option 1: `mongodb-atlas-local` Docker Image

### Description

An official MongoDB Docker image that bundles `mongod` and `mongot` into a single container, creating a single-node replica set with full Atlas Search and Vector Search capabilities. Both processes start automatically; a built-in health check ensures readiness before accepting connections.

This image is the foundation for multiple deployment variants — direct Docker, Atlas CLI, Docker Compose, and Testcontainers — which differ only in how the container lifecycle is managed.

### Requirements

| Requirement | Details |
|-------------|---------|
| Docker | Docker Desktop v4.31+ (macOS/Windows) or Docker Engine v27.0+ (Linux) |
| RAM | 2 GB minimum recommended |
| Disk | Sufficient for data + Lucene indexes |
| Network | Port 27017 (mongod client access) |
| MongoDB account | Not required |
| OS | macOS, Windows, Linux (any Docker-supported platform) |

### Deployment Variants

#### 1a. Direct Docker

Run the image directly with `docker run`. Fastest path to a working search environment — single command, zero configuration.

#### 1b. Atlas CLI

The official MongoDB Atlas CLI (`atlascli`) wraps the same Docker image with a higher-level CLI for container lifecycle, data management, and search index operations. Provides built-in search index management via JSON definitions. No Atlas account required for local use.

**Additional requirement:** `atlascli` installed (available via Homebrew, apt, yum, MSI, or direct download).

#### 1c. Docker Compose

Use the image in a Docker Compose configuration for multi-container local environments — alongside application services, seed data scripts, or monitoring tools.

**Key constraint:** The image's ENTRYPOINT must not be overridden — using a `command:` directive breaks the initialization sequence that starts both `mongod` and `mongot`. Mount volumes at `/data/db` and `/data/mongot` for persistence.

**Additional requirement:** Docker Compose v2.0+ (included with Docker Desktop).

#### 1d. Testcontainers

Testcontainers libraries for Java, Go, Node.js, and .NET provide programmatic lifecycle management for ephemeral integration test environments. The test framework spins up a fresh container before tests, provides connection details, and tears it down after completion.

Search index creation is async — tests must poll/retry until indexes reach READY status (typically 10-30 seconds container startup).

**Additional requirements:** Language-specific Testcontainers library (`testcontainers-java`, `testcontainers-go`, `@testcontainers/mongodb-atlas-local`, or .NET equivalent) and a test framework (JUnit, Go testing, Jest/Mocha, xUnit, etc.).

### Pros

- **Fastest setup** — single command (Docker) or declarative config (Compose), zero configuration
- **Cross-platform** — works on macOS, Windows, and Linux via Docker
- **Full feature parity** with Atlas cloud search capabilities
- **GA and officially supported** by MongoDB for dev/test use
- **Self-contained** — no external dependencies beyond Docker
- **Versioned image tags** available (e.g., `:8.0.6`) for reproducible environments
- **Built-in health check** — both `mongod` and `mongot` readiness verified before connections accepted
- **Multiple lifecycle options** — direct Docker, CLI-managed, Compose-orchestrated, or Testcontainers-automated
- **No Atlas account needed** for any variant

### Cons

- **Single-node only** — no multi-node replica sets or sharded clusters
- **Not supported for production** — MongoDB explicitly limits this to dev/test
- **Resource contention** — `mongod` and `mongot` share CPU, RAM, and I/O within one container
- **Proprietary license** — cannot inspect or modify the bundled `mongot` binary
- **No authentication** configured by default
- **No TLS** configured by default
- **No independent scaling** — cannot allocate separate resources to `mongod` vs `mongot`
- **ENTRYPOINT restriction** (Compose variant) — cannot use `command:` directive

### References

- Docker Hub: https://hub.docker.com/r/mongodb/mongodb-atlas-local
- Atlas CLI local deployments: https://www.mongodb.com/docs/atlas/cli/current/atlas-cli-deploy-local/
- Atlas CLI Docker deployments: https://www.mongodb.com/docs/atlas/cli/current/atlas-cli-deploy-docker/
- Atlas CLI Docker Compose: https://www.mongodb.com/docs/atlas/cli/current/atlas-cli-docker-compose/
- Testcontainers MongoDB Atlas module: https://testcontainers.com/modules/mongodb-atlas/
- Java Testcontainers: https://java.testcontainers.org/modules/databases/mongodb/
- Go Testcontainers: https://golang.testcontainers.org/modules/mongodb-atlaslocal/
- Example repo: https://github.com/mongodb-developer/atlas-search-local-testing

---

## Option 2: MongoDB Community Edition 8.2+ with Standalone `mongot`

### Description

Starting with MongoDB 8.2 (released late 2025), the `mongot` search engine is available as a **separate downloadable binary** that runs alongside MongoDB Community Edition. This is the most significant development for self-managed MongoDB search — it brings `$search`, `$searchMeta`, and `$vectorSearch` to self-managed deployments for free under SSPL.

`mongod` and `mongot` run as separate processes. `mongot` communicates with `mongod` via gRPC on port 27027 and tails Change Streams to build Lucene indexes. The `mongot` source code is published on GitHub under SSPL.

### Requirements

| Requirement | Details |
|-------------|---------|
| OS | **Linux only** — no macOS or Windows support in the preview |
| MongoDB Server | MongoDB Community Edition 8.2+ |
| mongot binary | Downloaded separately from MongoDB download center |
| Replica set | Required (even single-node) for Change Streams |
| RAM | 4 GB+ recommended (mongot uses significant memory for Lucene caches) |
| Disk | Separate storage recommended for Lucene indexes |
| Java | JRE bundled with mongot (no separate install needed) |
| Ops Manager | Optional for `mongod` management: **Ops Manager 8.0.13+** required for production MongoDB 8.2 (earlier 8.0.x supports 8.2 as preview only). Ops Manager manages `mongod` only — `mongot` is managed separately |

Docker images available:
- `mongodb/mongodb-community-server:8.2.0-ubi9` (mongod)
- `mongodb/mongodb-community-search:0.53.1` (mongot)

### Pros

- **Free and open source** — SSPL v1 license, source code on GitHub
- **Separate processes** — `mongod` and `mongot` can be independently sized, monitored, and restarted
- **Closest to production architecture** — mirrors how search works in Atlas and Enterprise deployments
- **Full feature parity** — `$search`, `$searchMeta`, `$vectorSearch`, autocomplete, faceting, hybrid search
- **Docker images available** — both `mongod` and `mongot` available as separate containers
- **Can be Docker Composed** — two-container setup with `mongod` and `mongot` as separate services
- **Multi-node capable** — can scale `mongot` instances alongside `mongod` replica set members
- **No proprietary dependencies** — fully open and inspectable

### Cons

- **Public Preview** — not recommended for production; APIs and behavior may change
- **Linux only** — no macOS or Windows support for the `mongot` binary or Docker image
- **Medium setup complexity** — requires configuring replica set, starting two processes, coordinating ports
- **Manual management** — no operator or automation; upgrades, monitoring, and scaling are all manual
- **Resource-intensive** — `mongot` is memory-hungry; co-located deployments face resource contention
- **GA date not announced** — no committed timeline from MongoDB

### References

- Blog: https://www.mongodb.com/company/blog/product-release-announcements/supercharge-self-managed-apps-search-vector-search-capabilities
- mongot source (SSPL): https://github.com/mongodb/mongot
- Community Edition: https://www.mongodb.com/products/self-managed/community-edition
- Community demo: https://github.com/markusos/mongo-search-demo

---

## Option 3: MongoDB Enterprise Server + Kubernetes Operator (`MongoDBSearch` CR)

### Description

The **MongoDB Controllers for Kubernetes** (the unified operator, v1.4+) can deploy `mongot` search pods alongside MongoDB Enterprise Server in Kubernetes using a `MongoDBSearch` Custom Resource. The operator deploys `mongot` pods as a separate StatefulSet with persistent storage. `mongot` pods connect to the `mongod` replica set via gRPC. When both are inside Kubernetes, the operator auto-configures authentication (keyfile) and networking.

Two architecture patterns are supported:
- **Internal**: both `mongod` and `mongot` pods inside Kubernetes, fully operator-managed
- **External**: `mongot` pods in Kubernetes connect to a `mongod` replica set running outside Kubernetes (bare metal, VMs, etc.)

### Requirements

| Requirement | Details |
|-------------|---------|
| Kubernetes | v1.24+ |
| Operator | MongoDB Controllers for Kubernetes v1.4+ |
| MongoDB Server | Enterprise Server 8.0.10+ (operator v1.4) or 8.2+ (operator v1.5+) |
| License | **MongoDB Enterprise Advanced subscription (paid)** |
| Storage | PersistentVolumeClaims for Lucene indexes |
| RAM | 1-4 GB per `mongot` pod recommended (configurable via podSpec) |
| Networking | gRPC connectivity between `mongod` and `mongot` pods |
| Ops Manager | Optional for `mongod` management: **Ops Manager 8.0.0+** required for Enterprise Server 8.0.x; **Ops Manager 8.0.13+** required for Enterprise Server 8.2 in production (earlier 8.0.x for preview only). Ops Manager manages `mongod` only — `mongot` is exclusively managed by the Kubernetes Operator |

**Important**: The unified operator (MongoDB Controllers for Kubernetes v1.4+) is a **different project** from the MongoDB Enterprise Kubernetes Operator v1.33 used in many existing deployments. Migration may be required.

### Pros

- **Operator-managed lifecycle** — deployment, scaling, upgrades, and health monitoring handled by the operator
- **Persistent storage** — Lucene indexes stored on PVCs, surviving pod restarts
- **Auto-configured authentication** — keyfile auth between `mongod` and `mongot` configured automatically
- **Supports external mongod** — `mongot` in Kubernetes can connect to `mongod` running on VMs or bare metal
- **Scalable** — `mongot` replica count configurable independently of `mongod`
- **Kubernetes-native monitoring** — integrates with Prometheus, standard K8s observability
- **Resource isolation** — `mongot` pods have dedicated CPU/memory limits separate from `mongod`

### Cons

- **Public Preview** — not production-ready; APIs and behavior may change
- **Paid Enterprise subscription required**
- **Kubernetes infrastructure required** — significant operational overhead if not already running K8s
- **High setup complexity** — operator installation, CRD management, RBAC, networking, storage classes
- **Different operator from v1.33** — existing Enterprise Kubernetes Operator deployments must migrate to the unified operator
- **Linux-only** containers

### References

- Deployment overview: https://www.mongodb.com/docs/kubernetes/current/fts-vs-deployment/
- Install with Enterprise: https://www.mongodb.com/docs/kubernetes/current/tutorial/install-fts-vs-with-enterprise/
- External mongod: https://www.mongodb.com/docs/kubernetes/current/tutorial/install-fts-vs-with-external-enterprise/
- Architecture: https://www.mongodb.com/docs/atlas/architecture/current/solutions-library/search-enterprise-server/

---

## Option 4: MongoDB Community 8.2+ with Kubernetes Operator

### Description

Starting with MongoDB Controllers for Kubernetes Operator v1.5+/v1.6, the operator supports deploying `mongot` alongside MongoDB Community Edition 8.2+ in Kubernetes using the same `MongoDBSearch` Custom Resource. Identical to Option 3 in operator mechanics but targeting Community Edition images instead of Enterprise.

### Requirements

| Requirement | Details |
|-------------|---------|
| Kubernetes | v1.24+ |
| Operator | MongoDB Controllers for Kubernetes v1.5+ or v1.6 |
| MongoDB Server | Community Edition 8.2+ |
| License | SSPL v1 (free) |
| Storage | PersistentVolumeClaims for Lucene indexes |
| RAM | 1-4 GB per `mongot` pod recommended |
| Networking | gRPC connectivity between `mongod` and `mongot` pods |
| Ops Manager | Optional for `mongod` management: **Ops Manager 8.0.13+** required for production MongoDB 8.2 (earlier 8.0.x supports 8.2 as preview only). Ops Manager manages `mongod` only — `mongot` is exclusively managed by the Kubernetes Operator |

Docker images:
- Operator: MongoDB Controllers for Kubernetes v1.5+/v1.6
- mongod: `mongodb/mongodb-community-server:8.2.0-ubi9`
- mongot: `mongodb/mongodb-community-search:0.53.1`

### Pros

- **Free** — no Enterprise subscription required, SSPL licensed
- **Operator-managed** — same automation benefits as Option 3 (lifecycle, scaling, health)
- **Persistent storage** for Lucene indexes via PVCs
- **Resource isolation** between `mongod` and `mongot` pods
- **Kubernetes-native monitoring** and observability
- **Community images** — openly available, no license restrictions

### Cons

- **Public Preview** — not production-ready
- **Kubernetes infrastructure required** — significant overhead
- **High setup complexity** — operator installation, CRDs, RBAC, storage
- **Different operator from v1.33** — not compatible with existing MongoDB Enterprise Kubernetes Operator deployments
- **Linux-only** containers
- **GA date not announced**

### References

- Install with Community: https://www.mongodb.com/docs/kubernetes/v1.6/tutorial/install-fts-vs-with-community/

---

## Remote Deployment (EC2, VMs, Bare Metal)

Any of Options 1-4 can be deployed on a **remote server** (EC2 instance, on-prem VM, bare metal) and accessed from a local workstation. This is a deployment model rather than a distinct technology. Especially useful for teams that need shared search environments, persistent test data, or Linux-based `mongot` when developing on macOS/Windows.

### How Remote Access Works

Clients connect to `mongod` on the remote server. Since single-node replica sets advertise internal hostnames, the `directConnection=true` connection parameter is required to prevent the driver from attempting to connect to internal/container hostnames.

Access methods include: direct IP connection, SSH tunneling, VPN/VPC peering, and AWS SSM port forwarding.

### Deployment Models

**Co-located (recommended for simplicity)**: Both `mongod` and `mongot` on the same remote host. Only port 27017 needs external exposure. The gRPC port (27027) stays on localhost.

**Dedicated (separate hosts)**: `mongot` on a different host from `mongod`. Useful when search workloads have very different resource profiles. Requires both port 27017 (wire protocol) and port 27027 (gRPC) open between hosts.

### Additional Requirements (on top of the base option)

| Requirement | Details |
|-------------|---------|
| Remote server | EC2 instance, on-prem VM, or bare metal server |
| OS (server) | Linux (required for standalone `mongot`); any Docker-supported OS for Option 1 |
| Docker (if applicable) | Docker Engine on the remote server for container-based options |
| Network | Port 27017 accessible from client workstation; port 27027 between hosts (dedicated model only) |
| Security | Firewall rules, security groups; TLS certificates with appropriate SANs for the server hostname, IPs, and DNS names |
| Client | `directConnection=true` required for single-node replica set access |

### EC2/VM Sizing Recommendations

| Workload | Instance Type | vCPU | RAM | Storage | Notes |
|----------|--------------|------|-----|---------|-------|
| Dev/test (co-located) | t3.large | 2 | 8 GB | 50 GB gp3 | Sufficient for small datasets |
| Medium (co-located) | m6i.xlarge | 4 | 16 GB | 100 GB gp3 | mongot benefits from RAM for Lucene caches |
| Large (co-located) | r6i.2xlarge | 8 | 64 GB | 500 GB gp3 | RAM-optimized for large indexes |
| Dedicated mongot only | r6i.xlarge | 4 | 32 GB | 200 GB gp3 | When running mongot on a separate host |

Key sizing considerations: `mongot` is memory-hungry (Lucene index caching), storage I/O matters (use gp3 or io2), and vector search with high-dimensional embeddings consumes significant additional RAM.

### Networking & Security (AWS Example)

Required security group rules:
- **Inbound TCP 27017** from client IP / VPC CIDR (mongod client access)
- **Inbound TCP 22** from admin IP (SSH management)
- **Inbound/outbound TCP 27017 and TCP 27027** between hosts (dedicated model only)
- TLS certificates should include SANs for: server hostname, private IP, public IP (if applicable), and any DNS names used for access

### Pros

- **Shared team access** — multiple developers can connect to the same search-enabled MongoDB instance
- **Persistent data** — survives container restarts and developer machine reboots
- **Solves the Linux-only constraint** — macOS/Windows developers can use standalone `mongot` via a Linux remote server
- **Flexible base option** — can run any of Options 1-4 on the remote server
- **Production-like environment** — closer to real deployment topology than local Docker
- **Multiple secure access methods** — SSH tunneling, VPN, SSM port forwarding avoid direct port exposure

### Cons

- **Network latency** — remote access adds latency compared to local Docker
- **`directConnection=true` required** — for single-node replica sets, driver must be told to skip topology discovery
- **Security exposure** — must properly configure firewalls, TLS, and authentication; public port exposure is risky
- **Ongoing cost** — EC2 instances and associated storage incur charges
- **Infrastructure management** — server patching, monitoring, backups become your responsibility
- **Same base option limitations apply** — e.g., if using `mongodb-atlas-local` remotely, still single-node and dev/test only

---

## Ops Manager Integration

### Scope

Ops Manager manages `mongod` processes (both Community and Enterprise editions). `mongot` processes, search indexes, and Lucene index storage are managed separately — either via the Kubernetes Operator (`MongoDBSearch` CR) or manually.

### Management Plane Separation

For deployments that include both Ops Manager and search capabilities, the management responsibilities split:

**Ops Manager handles:**
- `mongod` deployment lifecycle, monitoring, alerting, backup/restore, configuration, version upgrades, user/role management

**Managed separately:**
- `mongot` deployment — via Kubernetes Operator (`MongoDBSearch` CR) or manually
- `mongot` monitoring — via Kubernetes metrics or manual
- Search index creation/management — via mongosh, Compass, or driver APIs
- `mongot` version upgrades — via Kubernetes Operator or manual binary swap
- Lucene index storage — via PVCs (Kubernetes) or local disk

`mongot` connects to `mongod` via gRPC; Ops Manager agents are unaware of `mongot`. There is no conflict between the two. Ops Manager backups capture search index metadata in `mongod` but not Lucene index files in `mongot` (which are rebuilt automatically from Change Streams on restore).

### Recommended Approach for Ops Manager Environments

| Scenario | Recommended Path | Component Versions Required |
|----------|-----------------|---------------------------|
| K8s + Ops Manager + Enterprise 8.0.10+ | Add `MongoDBSearch` CR | MongoDB Controllers for K8s v1.4+, Ops Manager 8.0.0+ |
| K8s + Ops Manager + Enterprise 8.2+ | Add `MongoDBSearch` CR | MongoDB Controllers for K8s v1.5+, Ops Manager 8.0.13+ |
| K8s + Ops Manager + Community 8.2+ | Add `MongoDBSearch` CR | MongoDB Controllers for K8s v1.5+/v1.6, Ops Manager 8.0.13+ |
| VMs + Ops Manager + Community 8.2+ | Deploy standalone `mongot` manually | Ops Manager 8.0.13+, `mongot` binary from download center |
| New deployment evaluation | Use `mongodb-atlas-local` Docker image | Docker only (no Ops Manager needed) |

---

## Comparison Tables

### Operational Comparison

| Aspect | Option 1 (atlas-local) | Option 2 (Community standalone) | Options 3-4 (Kubernetes) |
|--------|------------------------|--------------------------------|--------------------------|
| Min MongoDB version | Bundled (no constraint) | Community 8.2+ | Enterprise 8.0.10+ / Community 8.2+ |
| Setup complexity | Low | Medium | High |
| Infrastructure | Docker only | Linux server(s) | Kubernetes cluster |
| Persistence | Optional (Docker volumes) | Native disk | PVCs in Kubernetes |
| Scaling | None (single-node) | Manual | Operator-managed |
| Monitoring | Basic (container health) | Manual | Kubernetes-native |
| Upgrades | Pull new image tag | Manual binary swap | Operator-managed |
| Platform (developer) | macOS, Windows, Linux | Linux only | Linux only (K8s nodes) |
| Authentication | None by default | Configurable | Keyfile (auto-configured) |
| TLS | Not configured by default | Configurable | Configurable |
| Multi-node | No | Yes (manual) | Yes (operator-managed) |

### Cost Comparison

| Option | Software Cost | Infrastructure Cost |
|--------|-------------|-------------------|
| Option 1 | Free | Local machine resources only |
| Option 2 | Free (SSPL) | Linux server (or Docker on Linux) |
| Option 3 | Enterprise subscription (paid) | Kubernetes cluster |
| Option 4 | Free (SSPL) | Kubernetes cluster |
| Remote deployment | Depends on base option | EC2/VM ongoing charges + storage |

---

## Decision Guide

### For local development

**Use Option 1 (Docker image or Docker Compose).**

Fastest path to a working search environment. No Kubernetes needed. Works on macOS, Windows, and Linux. Choose Docker Compose (1c) when you need MongoDB alongside other services.

### For automated testing / CI

**Use Option 1d (Testcontainers).**

Ephemeral containers with programmatic lifecycle management. Available for Java, Go, Node.js, and .NET. Best when you need deterministic, isolated test runs for search features.

### For self-managed deployment evaluation

**Use Option 2 (Community 8.2 + standalone mongot).**

Closest to a production-like self-managed setup. Free under SSPL. Currently Linux-only and in Public Preview. Best for evaluating the architecture and performance characteristics of self-managed search.

### For Kubernetes-based deployment evaluation

**Use Option 3 (Enterprise + K8s Operator) or Option 4 (Community + K8s Operator).**

Operator-managed `mongot` pods with persistent storage. Best for evaluating the Kubernetes deployment model. Both are in Public Preview; Option 3 requires a paid subscription, Option 4 is free.

### For shared team environments or macOS/Windows developers needing standalone mongot

**Deploy remotely** using any of the above on an EC2 instance or VM. Access via `directConnection=true`, SSH tunnel, or VPN. See the [Remote Deployment](#remote-deployment-ec2-vms-bare-metal) section.

### For existing Ops Manager environments

**Ops Manager manages `mongod` only.** Deploy `mongot` separately — via Kubernetes Operator (`MongoDBSearch` CR) for K8s environments, or standalone binary for VM-based environments with Community 8.2+. See the [Ops Manager Integration](#ops-manager-integration) section for the full component version matrix.

---

## References

### Official Documentation

- Atlas Search deployment options: https://www.mongodb.com/docs/atlas/atlas-search/about/deployment-options/
- Atlas CLI local deployments: https://www.mongodb.com/docs/atlas/cli/current/atlas-cli-deploy-local/
- Atlas CLI Docker deployments: https://www.mongodb.com/docs/atlas/cli/current/atlas-cli-deploy-docker/
- Atlas CLI Docker Compose: https://www.mongodb.com/docs/atlas/cli/current/atlas-cli-docker-compose/
- K8s Operator search deployment: https://www.mongodb.com/docs/kubernetes/current/fts-vs-deployment/
- K8s Operator with Enterprise: https://www.mongodb.com/docs/kubernetes/current/tutorial/install-fts-vs-with-enterprise/
- K8s Operator with Community: https://www.mongodb.com/docs/kubernetes/v1.6/tutorial/install-fts-vs-with-community/
- K8s Operator external mongod: https://www.mongodb.com/docs/kubernetes/current/tutorial/install-fts-vs-with-external-enterprise/
- Enterprise Search architecture: https://www.mongodb.com/docs/atlas/architecture/current/solutions-library/search-enterprise-server/

### Docker Images

- `mongodb/mongodb-atlas-local`: https://hub.docker.com/r/mongodb/mongodb-atlas-local
- `mongodb/mongodb-community-server`: https://hub.docker.com/r/mongodb/mongodb-community-server
- `mongodb/mongodb-community-search`: https://hub.docker.com/r/mongodb/mongodb-community-search

### Source Code & Community

- mongot source (SSPL): https://github.com/mongodb/mongot
- Atlas Search local testing examples: https://github.com/mongodb-developer/atlas-search-local-testing
- Community demo: https://github.com/markusos/mongo-search-demo

### Blog Posts & Announcements

- Local development for Atlas Search: https://www.mongodb.com/blog/post/introducing-local-development-experience-atlas-search-vector-search-atlas-cli
- Search for self-managed: https://www.mongodb.com/company/blog/product-release-announcements/supercharge-self-managed-apps-search-vector-search-capabilities
- MongoDB 8.2 release: https://www.mongodb.com/products/updates/mongodb-8-2-is-now-available/
