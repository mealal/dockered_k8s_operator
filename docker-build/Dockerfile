# MongoDB Ops Manager with HTTPS Support
FROM rockylinux:8

LABEL maintainer="ops-manager-deployer"
LABEL version="7.0.20"

# Install dependencies
RUN dnf install -y --allowerasing \
    curl procps net-tools cyrus-sasl cyrus-sasl-gssapi \
    cyrus-sasl-plain krb5-libs libcurl openldap openssl \
    ncurses java-11-openjdk-headless \
    && dnf clean all

# Download and install MongoDB Ops Manager
RUN curl -fSL "https://downloads.mongodb.com/on-prem-mms/rpm/mongodb-mms-7.0.20.500.20251204T1317Z.x86_64.rpm" -o /tmp/mongodb-mms.rpm \
    && rpm -ivh /tmp/mongodb-mms.rpm \
    && rm -f /tmp/mongodb-mms.rpm

# Create directories and set permissions
RUN mkdir -p /etc/mongodb-mms/certs /data/appdb /data/backup /opt/mongodb/mms/mongodb-releases \
    && chown -R mongodb-mms:mongodb-mms /etc/mongodb-mms \
    && chown -R mongodb-mms:mongodb-mms /data \
    && chown -R mongodb-mms:mongodb-mms /opt/mongodb/mms/conf \
    && chown -R mongodb-mms:mongodb-mms /opt/mongodb/mms/mongodb-releases

# Copy entrypoint script
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

# Expose ports
EXPOSE 8080 8443

# Environment variables
ENV MMS_HTTPS_PORT=8443
ENV MMS_HTTP_PORT=8080

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=120s --retries=5 \
    CMD curl -k -f https://localhost:8443/user/login || exit 1

# Volumes for persistence
VOLUME ["/data/appdb", "/data/backup", "/opt/mongodb/mms/logs"]

# Run as mongodb-mms user
USER mongodb-mms

ENTRYPOINT ["/entrypoint.sh"]
