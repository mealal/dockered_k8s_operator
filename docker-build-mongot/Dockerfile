# MongoDB Community Search (mongot) — Built from Scratch
#
# Multi-stage build: extracts the mongot runtime from the official MongoDB
# Community Search image, then packages it into a clean RockyLinux 8 base
# with a custom entrypoint that generates config from environment variables.
#
# mongot is a Java/Lucene-based search engine that provides $search,
# $searchMeta, and $vectorSearch aggregation stages for MongoDB.

# Stage 1: Extract mongot files from official image
FROM mongodb/mongodb-community-search:0.60.1 AS source

# Stage 2: Build clean image from scratch
FROM rockylinux:8

LABEL maintainer="mongot-deployer"
LABEL version="0.60.1"
LABEL description="MongoDB Community Search (mongot) — custom build"

# Install minimal runtime dependencies
RUN dnf install -y --allowerasing \
    procps net-tools \
    && dnf clean all

# Copy the entire mongot-community directory from the official image
COPY --from=source /mongot-community/ /opt/mongot/

# Create mongot user and directories
RUN useradd -r -s /bin/false mongot \
    && mkdir -p /data/mongot /var/log/mongot /etc/mongot \
    && chown -R mongot:mongot /data/mongot /var/log/mongot /opt/mongot /etc/mongot

# Copy entrypoint script and fix Windows CRLF line endings
COPY entrypoint.sh /entrypoint.sh
RUN sed -i 's/\r$//' /entrypoint.sh && chmod +x /entrypoint.sh

# Default gRPC port for mongod <-> mongot communication
EXPOSE 27027

# Environment variables (overridable at runtime)
ENV MONGOD_HOST_AND_PORT="mongod-0:27017"
ENV MONGOT_GRPC_ADDRESS="0.0.0.0:27027"
ENV DATA_DIR="/data/mongot"

# Data volume for Lucene indexes
VOLUME ["/data/mongot"]

# Health check — verify mongot process is running
HEALTHCHECK --interval=15s --timeout=5s --start-period=30s --retries=3 \
    CMD pgrep -f mongot > /dev/null || exit 1

USER mongot

ENTRYPOINT ["/entrypoint.sh"]
