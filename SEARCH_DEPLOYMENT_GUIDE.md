# MongoDB Search & Vector Search — Deployment Guide

Step-by-step guide for deploying MongoDB Community 8.2 with standalone `mongot` in Docker, managed by Ops Manager. This implements **Option 2** from [LOCAL_SEARCH_OPTIONS.md](LOCAL_SEARCH_OPTIONS.md).

## Architecture

```
  ┌──────────────────┐       Docker Network (ops-manager-network)
  │  ops-manager     │
  │  :8443 (HTTPS)   │◄────── Automation Agents report here
  │  OM 8.0.20       │        & download MongoDB binaries
  └──────────────────┘
           │
           │  API (Digest Auth)     Docker Network (mongodb-community-network)
           │                ┌──────────────────────────────────────────────────┐
           │                │                                                  │
           │    ┌───────────┴──┐   ┌──────────────┐   ┌──────────────┐       │
           │    │ mongo-agent-0│   │ mongo-agent-1│   │ mongo-agent-2│       │
           └───►│ Agent+mongod │   │ Agent+mongod │   │ Agent+mongod │       │
                │ :27017→27017 │   │ :27017→27018 │   │ :27017→27019 │       │
                │ RS: rs-comm. │   │ RS: rs-comm. │   │ RS: rs-comm. │       │
                └──────┬───────┘   └──────┬───────┘   └──────┬───────┘       │
                       │ gRPC :27027      │ gRPC :27027      │ gRPC :27027   │
                ┌──────┴───────┐   ┌──────┴───────┐   ┌──────┴───────┐       │
                │   mongot-0   │   │   mongot-1   │   │   mongot-2   │       │
                │  Lucene idx  │   │  Lucene idx  │   │  Lucene idx  │       │
                │  (Java/JRE)  │   │  (Java/JRE)  │   │  (Java/JRE)  │       │
                └──────────────┘   └──────────────┘   └──────────────┘       │
                                                                              │
                └──────────────────────────────────────────────────────────────┘

Client → mongo-agent-N:27017 (all queries including $search/$vectorSearch)
mongod → mongot via gRPC on port 27027 (internal, never exposed)
mongot → mongod via SCRAM auth (mongotUser with searchCoordinator role)
```

## Prerequisites

| Requirement | Details |
|-------------|---------|
| Docker | Docker Desktop v4.31+ (Windows/macOS) or Docker Engine v27.0+ (Linux) |
| RAM | 8 GB+ recommended (Ops Manager + mongod + mongot are memory-intensive) |
| Disk | 15 GB+ free for images, data, and Lucene indexes |
| Python 3.8+ | For running deployment scripts |
| Ports | 8443 (Ops Manager), 27017-27019 (MongoDB) free on host |

## Component Versions

| Component | Version | Source |
|-----------|---------|--------|
| Ops Manager | 8.0.20 | Custom image built from RPM on RockyLinux 8 |
| MongoDB Community | 8.2.5 | Downloaded by Ops Manager Agent (hybrid mode) |
| mongot (Community Search) | 0.60.1 | Custom image from `mongodb/mongodb-community-search:0.60.1` |
| Automation Agent | Latest | Downloaded from Ops Manager at image build time |

---

## Step 1: Deploy Ops Manager

Ops Manager manages the MongoDB replica set lifecycle (binary download, startup, configuration, upgrades).

```bash
python deploy_ops_manager.py
```

This:
1. Generates self-signed TLS certificates
2. Builds a custom Ops Manager Docker image (RockyLinux 8 + OM 8.0.20 RPM)
3. Deploys MongoDB 6.0 AppDB container
4. Deploys Ops Manager with HTTPS on port 8443
5. Creates an admin user and API key
6. Saves credentials to `ops-manager-api-key.json`

**Wait for Ops Manager** — first startup takes 3-5 minutes. The script waits automatically.

### Verify Ops Manager

```bash
# Check container is running
docker ps --filter "name=ops-manager"

# Open UI (self-signed cert warning expected)
# https://localhost:8443
```

---

## Step 2: Deploy 3-Node Replica Set

Deploy an Ops Manager-managed 3-node MongoDB 8.2.5 replica set.

```bash
python deploy_mongodb_community.py
```

This script:
1. Reads Ops Manager API credentials from `ops-manager-api-key.json`
2. Creates a project ("CommunitySearchPOC") in Ops Manager
3. Builds a Docker image containing the MongoDB Automation Agent
4. Deploys 3 agent containers on a shared Docker network
5. Pushes an automation config to Ops Manager defining the replica set
6. Waits for agents to download MongoDB 8.2.5 and initialize the RS

The **Automation Agent** handles the full mongod lifecycle: it downloads the MongoDB binary from Ops Manager, starts mongod with the correct configuration, initializes the replica set, and maintains the desired state.

### Verify RS Status

```bash
# Enter an agent container
docker exec -it mongo-agent-1 bash

# Find and run mongosh (agent installs it in a versioned directory)
MONGOSH=$(ls -d /var/lib/mongodb-mms-automation/mongosh-*/bin/mongosh | head -1)
$MONGOSH --port 27017 --eval "rs.status().members.map(m => m.name + ' ' + m.stateStr)"
```

Expected output: 1 PRIMARY + 2 SECONDARY.

---

## Step 3: Deploy mongot

Build a custom mongot image from scratch and deploy alongside the replica set.

```bash
python deploy_mongot.py
```

This script:
1. **Builds a custom Docker image** — multi-stage build extracting the mongot runtime from `mongodb/mongodb-community-search:0.60.1` into RockyLinux 8
2. **Updates the Ops Manager automation config** — adds `mongotHost`, `searchIndexManagementHostAndPort`, `skipAuthenticationToSearchIndexManagementServer`, and `useGrpcForSearch` setParameters to each mongod process
3. **Waits for rolling restart** — agents restart each mongod one at a time with the new parameters
4. **Creates a `mongotUser`** on the RS with the `searchCoordinator` role (required by mongot for SCRAM auth)
5. **Deploys 3 mongot containers** (one per mongod member) on the same Docker network
6. Each mongot authenticates to its corresponding mongod using a password file

### Custom Image Details

The Dockerfile (`docker-build-mongot/Dockerfile`) uses a multi-stage build:

```
Stage 1: mongodb/mongodb-community-search:0.60.1  →  extract /mongot-community/
Stage 2: rockylinux:8  →  clean base + mongot runtime (Java + Lucene) + entrypoint
```

The entrypoint generates a YAML config file from environment variables and starts mongot with `--config`.

### Verify mongot Containers

```bash
# Check all containers are running
docker ps --filter "name=mongot-"

# Check mongot logs (should show "Starting gRPC server" and RS discovery)
docker logs mongot-0
```

---

## Step 4: Validate Search Features

Connect to the primary and validate `$search` and `$vectorSearch` functionality.

### 4.1 Connect to mongosh

```bash
docker exec -it mongo-agent-1 bash
MONGOSH=$(ls -d /var/lib/mongodb-mms-automation/mongosh-*/bin/mongosh | head -1)
$MONGOSH --port 27017
```

### 4.2 Insert Sample Documents

```javascript
use searchtest

db.movies.insertMany([
  {
    title: "The Matrix",
    genre: "sci-fi",
    plot: "A computer hacker learns about the true nature of reality and his role in the war against its controllers.",
    year: 1999,
    embedding: [0.1, 0.8, 0.3, 0.5, 0.2, 0.9, 0.4, 0.7]
  },
  {
    title: "Inception",
    genre: "sci-fi",
    plot: "A thief who steals corporate secrets through dream-sharing technology is given the inverse task of planting an idea.",
    year: 2010,
    embedding: [0.2, 0.7, 0.4, 0.6, 0.1, 0.8, 0.5, 0.3]
  },
  {
    title: "The Shawshank Redemption",
    genre: "drama",
    plot: "Two imprisoned men bond over a number of years, finding solace and eventual redemption through acts of common decency.",
    year: 1994,
    embedding: [0.9, 0.1, 0.7, 0.2, 0.8, 0.3, 0.6, 0.4]
  },
  {
    title: "Interstellar",
    genre: "sci-fi",
    plot: "A team of explorers travel through a wormhole in space in an attempt to ensure humanity's survival.",
    year: 2014,
    embedding: [0.15, 0.85, 0.35, 0.55, 0.25, 0.95, 0.45, 0.65]
  },
  {
    title: "The Godfather",
    genre: "crime",
    plot: "The aging patriarch of an organized crime dynasty transfers control of his clandestine empire to his reluctant son.",
    year: 1972,
    embedding: [0.8, 0.2, 0.6, 0.3, 0.7, 0.1, 0.5, 0.9]
  }
])
```

### 4.3 Create a Full-Text Search Index

```javascript
db.movies.createSearchIndex("default", {
  mappings: {
    dynamic: true,
    fields: {
      genre: [
        { type: "string" },
        { type: "token" }
      ]
    }
  }
})
```

The `token` type on `genre` is required for faceted search (`$searchMeta`) in step 4.6.

### 4.4 Wait for Index to Be Ready

Search indexes build asynchronously. Check status:

```javascript
db.runCommand({ listSearchIndexes: "movies" })
```

This typically takes 10-30 seconds for small collections.

### 4.5 Run a `$search` Query

```javascript
db.movies.aggregate([
  {
    $search: {
      text: {
        query: "hacker reality",
        path: "plot"
      }
    }
  },
  {
    $project: {
      title: 1,
      plot: 1,
      score: { $meta: "searchScore" }
    }
  }
])
```

Expected: Returns "The Matrix" with the highest score.

### 4.6 Run a `$searchMeta` Query

```javascript
db.movies.aggregate([
  {
    $searchMeta: {
      index: "default",
      facet: {
        facets: {
          genreFacet: { type: "string", path: "genre" }
        }
      }
    }
  }
])
```

Expected: 5 total documents with 3 genre buckets (sci-fi: 3, crime: 1, drama: 1).

### 4.7 Create a Vector Search Index

```javascript
db.movies.createSearchIndex(
  "vector_index",
  "vectorSearch",
  {
    fields: [
      {
        type: "vector",
        path: "embedding",
        numDimensions: 8,
        similarity: "cosine"
      }
    ]
  }
)
```

### 4.8 Run a `$vectorSearch` Query

```javascript
db.movies.aggregate([
  {
    $vectorSearch: {
      index: "vector_index",
      path: "embedding",
      queryVector: [0.12, 0.82, 0.32, 0.52, 0.22, 0.92, 0.42, 0.62],
      numCandidates: 10,
      limit: 3
    }
  },
  {
    $project: {
      title: 1,
      genre: 1,
      score: { $meta: "vectorSearchScore" }
    }
  }
])
```

Expected: "Interstellar" and "The Matrix" should score highest.

---

## Port Reference

| Container | Internal Port | Host Port | Protocol |
|-----------|--------------|-----------|----------|
| ops-manager | 8443 | 8443 | HTTPS (Ops Manager UI + API) |
| ops-manager-appdb | 27017 | — | MongoDB wire (internal) |
| mongo-agent-0 | 27017 | 27017 | MongoDB wire protocol |
| mongo-agent-1 | 27017 | 27018 | MongoDB wire protocol |
| mongo-agent-2 | 27017 | 27019 | MongoDB wire protocol |
| mongot-0 | 27027 | — | gRPC (internal only) |
| mongot-1 | 27027 | — | gRPC (internal only) |
| mongot-2 | 27027 | — | gRPC (internal only) |

---

## Cleanup

Remove everything (containers, volumes, network, data):

```bash
# Remove mongot first, then RS, then Ops Manager
python deploy_mongot.py --cleanup
python deploy_mongodb_community.py --cleanup
python deploy_ops_manager.py --cleanup
```

---

## Troubleshooting

### Ops Manager not ready within timeout

Ops Manager first startup can take 5+ minutes. Check logs:
```bash
docker logs ops-manager 2>&1 | tail -20
```

### Agent containers not converging

Check agent logs inside the container:
```bash
docker exec mongo-agent-0 bash -c 'tail -50 /var/log/mongodb-mms-automation/automation-agent.log'
```

Common issues:
- **Download timeouts**: Ops Manager hybrid mode downloads binaries from the internet — slow connections cause timeouts
- **Invalid setParameter**: MongoDB version must match the parameters used. MongoDB 8.2+ is required for `useGrpcForSearch` and `searchCoordinator` role

### mongot container exits immediately

Check logs:
```bash
docker logs mongot-0
```

Common causes:
- **`username is required`**: mongot 0.60.1 requires SCRAM auth. Ensure `deploy_mongot.py` creates the `mongotUser`
- **`permissions are too permissive`**: Password file must be 600. The entrypoint copies it with correct permissions
- **`unrecognized field "tls"`**: Don't include `tls` at the `replicaSet` level in the mongot config

### Search index stays in BUILDING state

- mongot needs time to tail Change Streams and build the Lucene index
- For small collections (< 100 docs), this should take under 30 seconds
- Check mongot logs: `docker logs mongot-0`

### "mongot is not running" error from mongosh

Verify:
```bash
# mongot containers are running
docker ps --filter "name=mongot-"

# mongod has mongotHost parameter set
docker exec mongo-agent-0 bash -c 'cat /data/db/automation-mongod.conf | grep mongot'
# Should show: mongotHost: mongot-0:27027

# mongot can reach mongod (same Docker network)
docker network inspect mongodb-community-network
```

### MongoDB version incompatibilities

| Feature | Minimum Version |
|---------|----------------|
| `mongotHost` setParameter | 8.0.0 |
| `searchIndexManagementHostAndPort` | 8.0.0 |
| `useGrpcForSearch` | 8.2.0 |
| `searchCoordinator` role | 8.2.0 |
| `skipAuthenticationToSearchIndexManagementServer` | 8.0.0 |

**MongoDB 8.2+ is required** for full Community Search support.
