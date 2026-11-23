FROM debian:bookworm-slim

RUN echo 'slapd slapd/no_configuration boolean true' | debconf-set-selections \
    && apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
       slapd ldap-utils python3 ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && mkdir -p /run/ldap /var/lib/ldap \
    && chown -R 10001:10001 /run/ldap /var/lib/ldap

COPY container/ /opt/ldap/
USER 10001:10001
EXPOSE 1636
ENTRYPOINT ["python3", "/opt/ldap/start.py"]
