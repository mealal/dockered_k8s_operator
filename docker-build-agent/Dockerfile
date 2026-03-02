# MongoDB Automation Agent — Built from Scratch
#
# Downloads the MongoDB Agent RPM from the Ops Manager instance and
# installs it on a clean RockyLinux 8 base. The agent manages mongod
# lifecycle (download, start, configure, upgrade) as directed by Ops Manager.
#
# The mongod binary is NOT included in this image — the agent downloads
# it from Ops Manager at runtime based on the automation config.

FROM rockylinux:8

LABEL maintainer="mongodb-agent-deployer"
LABEL description="MongoDB Automation Agent for Ops Manager-managed deployments"

# Install dependencies required by the MongoDB Agent and mongod
RUN dnf install -y --allowerasing \
    cyrus-sasl cyrus-sasl-gssapi cyrus-sasl-plain \
    krb5-libs libcurl net-snmp openldap openssl \
    procps-ng curl hostname net-tools \
    && dnf clean all

# The agent RPM is downloaded from Ops Manager at build time.
# Usage: docker build --build-arg OPS_MANAGER_URL=https://host.docker.internal:8443 .
ARG OPS_MANAGER_URL=https://host.docker.internal:8443

# Download and install the MongoDB Agent from Ops Manager
RUN curl -kOL "${OPS_MANAGER_URL}/download/agent/automation/mongodb-mms-automation-agent-manager-latest.x86_64.rhel8.rpm" \
    && rpm -ivh mongodb-mms-automation-agent-manager-latest.x86_64.rhel8.rpm \
    && rm -f mongodb-mms-automation-agent-manager-latest.x86_64.rhel8.rpm

# Create required directories
RUN mkdir -p /data/db /var/run/mongodb-mms-automation \
        /var/log/mongodb-mms-automation \
        /var/lib/mongodb-mms-automation \
    && chown -R mongod:mongod /data /var/run/mongodb-mms-automation \
        /var/log/mongodb-mms-automation \
        /var/lib/mongodb-mms-automation

# Copy entrypoint and fix Windows CRLF line endings
COPY entrypoint.sh /entrypoint.sh
RUN sed -i 's/\r$//' /entrypoint.sh && chmod +x /entrypoint.sh

# Expose MongoDB and agent ports
EXPOSE 27017

# Data volume for MongoDB
VOLUME ["/data/db"]

ENTRYPOINT ["/entrypoint.sh"]
