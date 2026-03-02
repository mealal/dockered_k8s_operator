#!/bin/bash
set -e

# Required environment variables
: "${MMS_GROUP_ID:?MMS_GROUP_ID must be set (project ID from Ops Manager)}"
: "${MMS_API_KEY:?MMS_API_KEY must be set (agent API key from Ops Manager)}"
: "${MMS_BASE_URL:?MMS_BASE_URL must be set (Ops Manager URL)}"

CONFIG_FILE="/etc/mongodb-mms/automation-agent.config"

echo "=========================================="
echo "MongoDB Automation Agent"
echo "=========================================="
echo "Ops Manager URL: ${MMS_BASE_URL}"
echo "Project ID:      ${MMS_GROUP_ID}"
echo "Hostname:        $(hostname)"
echo "=========================================="

# Configure the agent
sed -i "s|^mmsGroupId=.*|mmsGroupId=${MMS_GROUP_ID}|" "$CONFIG_FILE"
sed -i "s|^mmsApiKey=.*|mmsApiKey=${MMS_API_KEY}|" "$CONFIG_FILE"
sed -i "s|^mmsBaseUrl=.*|mmsBaseUrl=${MMS_BASE_URL}|" "$CONFIG_FILE"

# Disable TLS verification for self-signed Ops Manager certs (POC only)
if ! grep -q "^sslRequireValidMMSServerCertificates=" "$CONFIG_FILE"; then
    echo "sslRequireValidMMSServerCertificates=false" >> "$CONFIG_FILE"
else
    sed -i "s|^sslRequireValidMMSServerCertificates=.*|sslRequireValidMMSServerCertificates=false|" "$CONFIG_FILE"
fi

# Ensure data directories exist with correct ownership
mkdir -p /data/db /var/run/mongodb-mms-automation /var/log/mongodb-mms-automation
chown -R mongod:mongod /data /var/run/mongodb-mms-automation /var/log/mongodb-mms-automation

echo ""
echo "Starting MongoDB Automation Agent..."

# Start the agent as mongod user (the agent will start mongod as needed)
exec /opt/mongodb-mms-automation/bin/mongodb-mms-automation-agent \
    -f "$CONFIG_FILE" \
    -pidfilepath /var/run/mongodb-mms-automation/mongodb-mms-automation-agent.pid
