#!/bin/bash
set -e

MONGOT_DIR="/opt/mongot"
MONGOT_BIN="${MONGOT_DIR}/mongot"
CONFIG_FILE="/etc/mongot/config.yml"
DATA_DIR="${DATA_DIR:-/data/mongot}"
MONGOD_TARGET="${MONGOD_HOST_AND_PORT:-mongod-0:27017}"
GRPC_ADDRESS="${MONGOT_GRPC_ADDRESS:-0.0.0.0:27027}"
MONGOT_USERNAME="${MONGOT_USERNAME:-mongotUser}"
MONGOT_PWFILE="${MONGOT_PWFILE:-/etc/mongot/pwfile}"
MONGOT_AUTH_SOURCE="${MONGOT_AUTH_SOURCE:-admin}"

echo "=========================================="
echo "MongoDB Community Search (mongot)"
echo "=========================================="
echo "mongod target:   ${MONGOD_TARGET}"
echo "gRPC address:    ${GRPC_ADDRESS}"
echo "Data directory:  ${DATA_DIR}"
echo "Username:        ${MONGOT_USERNAME}"
echo "Auth source:     ${MONGOT_AUTH_SOURCE}"
echo "=========================================="

# Verify mongot binary exists
if [ ! -f "${MONGOT_BIN}" ]; then
    echo "ERROR: mongot script not found at ${MONGOT_BIN}"
    echo "Available files in ${MONGOT_DIR}/:"
    ls -la "${MONGOT_DIR}/" 2>/dev/null || echo "  (directory not found)"
    exit 1
fi

# Ensure data directory exists
mkdir -p "${DATA_DIR}"

# Copy password file with correct permissions (must be 600)
if [ -f "${MONGOT_PWFILE}" ]; then
    SECURE_PWFILE="/tmp/mongot-pwfile"
    cp "${MONGOT_PWFILE}" "${SECURE_PWFILE}"
    chmod 600 "${SECURE_PWFILE}"
    MONGOT_PWFILE="${SECURE_PWFILE}"
    echo "Password file secured: ${MONGOT_PWFILE}"
fi

# Generate config file from environment variables
cat > "${CONFIG_FILE}" <<EOF
syncSource:
  replicaSet:
    hostAndPort: "${MONGOD_TARGET}"
    username: ${MONGOT_USERNAME}
    passwordFile: ${MONGOT_PWFILE}
    authSource: ${MONGOT_AUTH_SOURCE}
storage:
  dataPath: "${DATA_DIR}"
server:
  grpc:
    address: "${GRPC_ADDRESS}"
    tls:
      mode: "disabled"
logging:
  verbosity: INFO
EOF

echo ""
echo "Generated config:"
cat "${CONFIG_FILE}"
echo ""
echo "Starting mongot..."

# Execute mongot with config file (replaces shell process)
exec "${MONGOT_BIN}" --config "${CONFIG_FILE}"
