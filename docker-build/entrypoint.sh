#!/bin/bash
set -e

CONFIG_FILE="/opt/mongodb/mms/conf/conf-mms.properties"
CERT_DIR="/etc/mongodb-mms/certs"

# Function to update or add a property in the config file
update_config() {
    local key="$1"
    local value="$2"
    if grep -q "^${key}=" "$CONFIG_FILE" 2>/dev/null; then
        sed -i "s|^${key}=.*|${key}=${value}|" "$CONFIG_FILE"
    else
        echo "${key}=${value}" >> "$CONFIG_FILE"
    fi
}

echo "Configuring MongoDB Ops Manager..."

# Skip initial UI setup wizard
update_config "mms.ignoreInitialUiSetup" "true"

# Email configuration (required)
FROM_EMAIL="${MMS_FROM_EMAIL:-ops-manager@${MMS_EMAIL_DOMAIN:-localhost.local}}"
REPLY_EMAIL="${MMS_REPLY_EMAIL:-ops-manager@${MMS_EMAIL_DOMAIN:-localhost.local}}"
ADMIN_EMAIL="${MMS_ADMIN_EMAIL:-admin@${MMS_EMAIL_DOMAIN:-localhost.local}}"

update_config "mms.fromEmailAddr" "$FROM_EMAIL"
update_config "mms.replyToEmailAddr" "$REPLY_EMAIL"
update_config "mms.adminEmailAddr" "$ADMIN_EMAIL"

# SMTP configuration
update_config "mms.mail.transport" "smtp"
update_config "mms.mail.hostname" "${MMS_SMTP_HOSTNAME:-localhost}"
update_config "mms.mail.port" "${MMS_SMTP_PORT:-25}"

# User registration settings
update_config "mms.user.bypassInviteForExistingUsers" "true"
update_config "mms.userSvcClass" "com.xgen.svc.mms.svc.user.UserSvcDb"
update_config "mms.user.invitationOnly" "false"

# Disable API access list requirement for Kubernetes operator connectivity
# This allows API calls from any IP address without whitelist restrictions
update_config "mms.publicApi.whitelistEnabled" "false"

# Configure Hybrid Mode for MongoDB binary downloads
# In Hybrid Mode, Ops Manager downloads binaries from the internet and serves them to agents
# This allows agents without internet access to get binaries from Ops Manager
update_config "automation.versions.source" "hybrid"

# Configure the directory where Ops Manager stores MongoDB binaries
update_config "automation.versions.directory" "/opt/mongodb/mms/mongodb-releases/"

echo "Email configuration completed"
echo "Hybrid Mode enabled: Agents will download MongoDB binaries from Ops Manager"

# HTTPS configuration
if [ -f "${CERT_DIR}/server.pem" ] && [ -f "${CERT_DIR}/ca.crt" ]; then
    echo "Configuring HTTPS..."
    update_config "mms.https.PEMKeyFile" "${CERT_DIR}/server.pem"
    update_config "mms.https.CAFile" "${CERT_DIR}/ca.crt"
    update_config "mms.https.ClientCertificateMode" "None"
    if [ -n "$MMS_HTTPS_PORT" ]; then
        update_config "BASE_PORT" "${MMS_HTTPS_PORT}"
    fi
fi

# Database configuration
if [ -n "$MONGO_URI" ]; then
    update_config "mongo.mongoUri" "$MONGO_URI"
    echo "MongoDB URI configured"
fi

# Central URL
if [ -n "$MMS_CENTRAL_URL" ]; then
    update_config "mms.centralUrl" "$MMS_CENTRAL_URL"
fi

# Encryption key
if [ -n "$MMS_ENCRYPTION_KEY" ]; then
    update_config "mongodb.encryption.key" "$MMS_ENCRYPTION_KEY"
else
    RANDOM_KEY=$(openssl rand -base64 24)
    update_config "mongodb.encryption.key" "$RANDOM_KEY"
fi

# Gen key
if [ -n "$MMS_GEN_KEY" ]; then
    update_config "mms.genKey" "$MMS_GEN_KEY"
fi

echo ""
echo "=========================================="
echo "Ops Manager Configuration Summary:"
echo "=========================================="
echo "Skip UI Setup Wizard: true"
echo "From Email: $FROM_EMAIL"
echo "SMTP Host: ${MMS_SMTP_HOSTNAME:-localhost}"
echo "Central URL: ${MMS_CENTRAL_URL:-not set}"
echo "=========================================="
echo ""

echo "Starting MongoDB Ops Manager..."
/opt/mongodb/mms/bin/mongodb-mms start

echo "Ops Manager started. Tailing logs..."
tail -f /opt/mongodb/mms/logs/mms0.log
