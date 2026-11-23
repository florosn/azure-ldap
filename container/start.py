"""Configure an independent MDB database with TLS-only multi-provider replication."""

import os
from pathlib import Path
import re
import subprocess


def quoted(value):
    if any(c in value for c in "\r\n\x00"):
        raise ValueError("Configuration values must not contain line breaks or NUL")
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def main():
    os.umask(0o077)
    base = os.environ["LDAP_BASE_DN"]
    if not re.fullmatch(r"dc=[a-zA-Z0-9-]+(?:,dc=[a-zA-Z0-9-]+)*", base):
        raise ValueError("LDAP_BASE_DN must be a comma-separated sequence of dc= RDNs")
    ordinal = int(os.environ["HOSTNAME"].rsplit("-", 1)[1])
    if ordinal not in range(3):
        raise ValueError("This deployment requires exactly three LDAP peers")
    domain = os.environ["LDAP_PEER_DOMAIN"]
    if not re.fullmatch(r"[a-z0-9.-]+", domain):
        raise ValueError("Invalid LDAP_PEER_DOMAIN")
    password_file = Path("/run/secrets/admin-password")
    password = password_file.read_text()
    if len(password) < 16:
        raise ValueError("Use an LDAP admin password of at least 16 characters")
    password_hash = subprocess.check_output(
        ["slappasswd", "-T", str(password_file)], text=True
    ).strip()
    rootdn = f"cn=admin,{base}"
    config = f"""
include /etc/ldap/schema/core.schema
include /etc/ldap/schema/cosine.schema
include /etc/ldap/schema/nis.schema
include /etc/ldap/schema/inetorgperson.schema
pidfile /run/ldap/slapd.pid
argsfile /run/ldap/slapd.args
modulepath /usr/lib/ldap
moduleload back_mdb
moduleload syncprov
serverID {ordinal + 1}
TLSCertificateFile /run/tls/tls.crt
TLSCertificateKeyFile /run/tls/tls.key
TLSCACertificateFile /run/tls/ca.crt
TLSProtocolMin 3.3
database mdb
maxsize 10737418240
suffix {quoted(base)}
rootdn {quoted(rootdn)}
rootpw {password_hash}
directory /var/lib/ldap
index objectClass eq
index entryUUID eq
index entryCSN eq
access to dn.base="" by * read
access to attrs=userPassword
    by self write
    by anonymous auth
    by * none
access to *
    by users read
    by * none
overlay syncprov
syncprov-checkpoint 100 10
syncprov-sessionlog 1000
"""
    for peer in range(3):
        if peer == ordinal:
            continue
        config += f"""
syncrepl rid={peer + 1:03}
    provider=ldaps://ldap-{peer}.{domain}:1636
    bindmethod=simple
    binddn={quoted(rootdn)}
    credentials={quoted(password)}
    searchbase={quoted(base)}
    type=refreshAndPersist
    retry="5 5 30 +"
    timeout=10
    schemachecking=on
    tls_reqcert=demand
    tls_cacert=/run/tls/ca.crt
"""
    config += "\nmirrormode TRUE\n"
    config_path = Path("/run/ldap/slapd.conf")
    config_path.write_text(config)
    subprocess.run(["slaptest", "-f", str(config_path), "-u"], check=True)
    if not Path("/var/lib/ldap/data.mdb").exists():
        # Only one peer seeds the suffix; the others receive it through syncrepl.
        restore = Path("/var/lib/ldap/restore.ldif")
        if restore.exists():
            try:
                subprocess.run(
                    ["slapadd", "-f", str(config_path), "-l", str(restore)], check=True
                )
            except subprocess.CalledProcessError:
                # Never serve a partially imported directory after a restart.
                Path("/var/lib/ldap/data.mdb").unlink(missing_ok=True)
                Path("/var/lib/ldap/lock.mdb").unlink(missing_ok=True)
                raise
            restore.unlink()
        elif ordinal == 0:
            dc = base.split(",", 1)[0].split("=", 1)[1]
            seed = (
                f"dn: {base}\nobjectClass: top\nobjectClass: dcObject\n"
                f"objectClass: organization\no: LDAP directory\ndc: {dc}\n\n"
            )
            subprocess.run(
                ["slapadd", "-f", str(config_path)], input=seed, text=True, check=True
            )
    os.execvp("slapd", [
        "slapd", "-f", str(config_path), "-h",
        "ldaps://0.0.0.0:1636/ ldapi://%2Frun%2Fldap%2Fldapi", "-d", "256",
    ])


if __name__ == "__main__":
    main()
