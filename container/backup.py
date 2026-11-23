"""Periodically export MDB and upload block blobs using Azure workload identity."""

from datetime import datetime, timezone
import base64
from email.utils import formatdate
import gzip
import json
import logging
import os
from pathlib import Path
import subprocess
import tempfile
import time
import urllib.parse
import urllib.request
import uuid


def request(url, data, headers):
    req = urllib.request.Request(
        url, data=data, headers={**headers, "x-ms-date": formatdate(usegmt=True)},
        method="PUT",
    )
    with urllib.request.urlopen(req, timeout=120) as response:
        response.read()


def backup():
    # slapcat is supported against a running MDB backend; never copy live MDB files.
    with tempfile.TemporaryFile() as raw, tempfile.TemporaryFile() as compressed:
        subprocess.run(
            ["slapcat", "-f", "/run/ldap/slapd.conf"], stdout=raw, check=True
        )
        if raw.tell() == 0:
            raise RuntimeError("Directory has not been seeded")
        raw.seek(0)
        with gzip.GzipFile(fileobj=compressed, mode="wb") as archive:
            while chunk := raw.read(8 * 1024 * 1024):
                archive.write(chunk)
        compressed.seek(0)
        assertion = Path(os.environ["AZURE_FEDERATED_TOKEN_FILE"]).read_text()
        form = urllib.parse.urlencode({
            "client_id": os.environ["AZURE_CLIENT_ID"],
            "scope": "https://storage.azure.com/.default",
            "grant_type": "client_credentials",
            "client_assertion_type": "urn:ietf:params:oauth:client-assertion-type:jwt-bearer",
            "client_assertion": assertion,
        }).encode()
        token_url = (
            "https://login.microsoftonline.com/"
            + urllib.parse.quote(os.environ["AZURE_TENANT_ID"], safe="")
            + "/oauth2/v2.0/token"
        )
        with urllib.request.urlopen(token_url, data=form, timeout=60) as response:
            token = json.load(response)["access_token"]
        account = os.environ["BACKUP_STORAGE_ACCOUNT"]
        if not account.isalnum():
            raise ValueError("Invalid storage account name")
        name = datetime.now(timezone.utc).strftime("%Y/%m/%d/%H%M%S-") + str(uuid.uuid4())
        url = f"https://{account}.blob.core.windows.net/ldap-backups/{name}.ldif.gz"
        headers = {"Authorization": "Bearer " + token, "x-ms-version": "2023-11-03"}
        blocks = []
        while chunk := compressed.read(8 * 1024 * 1024):
            block = base64.b64encode(f"{len(blocks):08}".encode()).decode()
            request(url + "?comp=block&blockid=" + urllib.parse.quote(block, safe=""),
                    chunk, headers)
            blocks.append(f"<Latest>{block}</Latest>")
        request(url + "?comp=blocklist",
                ("<BlockList>" + "".join(blocks) + "</BlockList>").encode(),
                {**headers, "Content-Type": "application/xml"})
        logging.info("Uploaded backup %s.ldif.gz", name)


def main():
    logging.basicConfig(level=logging.INFO)
    while True:
        try:
            if Path("/run/ldap/ldapi").exists() and Path("/var/lib/ldap/data.mdb").exists():
                backup()
        except Exception:
            # Do not log request bodies, tokens, LDAP credentials, or directory data.
            logging.error("LDAP backup failed; check identity, storage, and database health")
        time.sleep(int(os.environ.get("BACKUP_INTERVAL_SECONDS", "3600")))


if __name__ == "__main__":
    main()
