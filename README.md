# OpenLDAP on Azure

A TLS-only OpenLDAP container, a three-zone AKS deployment, and geo-replicated
directory backups. This is a deployment foundation; validate capacity, replication,
restore time, network connectivity, and your directory's schema before production.

## Architecture and guarantees

- Three writable OpenLDAP MDB peers use **OpenLDAP syncrepl**, each with its own
  32-GiB Azure Disk. Kubernetes spreads them across three availability zones.
  An internal Azure load balancer exposes LDAPS on port 636. On-premises clients
  need VPN/ExpressRoute connectivity to the AKS VNet and private DNS for the service.
- Peers use authenticated, certificate-verified TLS, including replication traffic.
  There is no plaintext LDAP listener. Non-root containers have read-only root
  filesystems; secrets are mounted, not baked into images or Terraform state.
- Multi-provider replication is **asynchronous**, not a quorum/consensus system.
  Peers continue serving through a zone failure, but recent writes may be lost
  before replication. Avoid concurrent changes to the same entry, especially
  during network partitions; prefer a designated writer in clients with failover
  to another peer. Test application retry/reconnect behavior and convergence.
- Each peer exports an hourly `slapcat` LDIF backup to a private Blob container
  using Azure workload identity (no storage keys). Backup storage uses **RA-GZRS**:
  synchronous replication across primary-region zones and asynchronous storage
  replication to Azure's paired secondary region. Secondary region selection is
  controlled by Azure, not this deployment. Read the Terraform outputs for the pair.
- Geo-replicated backups provide **cold regional disaster recovery**, not an
  active LDAP endpoint in a second region. Regional failover is manual: provision
  compute in a healthy region, restore one backup, then switch client DNS.
  RPO includes the backup interval, LDAP replication lag, and storage replication
  lag; RTO includes cluster creation and restore. There is no zero-data-loss claim.
  Never share or geo-copy live MDB files between writable slapd instances.
- Azure Disk LRS volumes remain pinned to their original zone. During an outage,
  the two surviving peers serve traffic; the failed peer waits for its zone/disk.
  A disruption budget protects against voluntary simultaneous disruptions.

## Prerequisites

Docker, Azure CLI, Terraform >=1.6, kubectl, kubelogin, and `envsubst`; an Azure
subscription and permissions to create AKS, storage, identities, and role
assignments. Choose a region supporting **three AKS zones and RA-GZRS**, with quota
for three `Standard_D2s_v5` nodes. Confirm nodes actually occupy all three zones;
the pods deliberately remain Pending rather than silently colocating zones.

Examples use Bash. Set `REPO` to your clone's **absolute path**:

```sh
export REPO=/absolute/path/to/azure-ldap
export LDAP_IMAGE=yourregistry.azurecr.io/openldap:1
docker build --pull -t "$LDAP_IMAGE" "$REPO"
docker push "$LDAP_IMAGE"
```

Use an immutable tag or digest and rebuild regularly for Debian security updates.
For a private Azure Container Registry, grant the AKS kubelet identity `AcrPull`
(for example `az aks update --attach-acr REGISTRY_NAME ...`) before deployment.
The image includes standard core, cosine, nis, and inetOrgPerson schemas.

## Provision Azure infrastructure

```sh
az login
export TF_VAR_subscription_id="$(az account show --query id -o tsv)"
export TF_VAR_storage_account_name=YOURUNIQUESTORAGENAME
export TF_VAR_admin_group_object_ids='["YOUR-ENTRA-ADMIN-GROUP-OBJECT-ID"]'
export TF_VAR_api_server_authorized_ip_ranges='["YOUR-ADMIN-PUBLIC-IP/32"]'
terraform -chdir="$REPO/deploy" init
terraform -chdir="$REPO/deploy" plan -out=deployment.tfplan
terraform -chdir="$REPO/deploy" apply deployment.tfplan
az aks get-credentials --resource-group azure-ldap --name azure-ldap
kubelogin convert-kubeconfig -l azurecli
kubectl get nodes -L topology.kubernetes.io/zone
export BACKUP_CLIENT_ID="$(terraform -chdir="$REPO/deploy" output -raw backup_client_id)"
export BACKUP_STORAGE_ACCOUNT="$(terraform -chdir="$REPO/deploy" output -raw backup_storage_account)"
export LDAP_BASE_DN=dc=example,dc=com
```

Defaults use the `azure-ldap` resource group/cluster in `westeurope`; override
Terraform inputs and CLI resource names together. Use a secured remote Terraform
state backend for team/production operation. AKS uses Entra RBAC, disables local
accounts, and restricts API access to your supplied administrator CIDRs.
Storage's HTTPS endpoint is reachable publicly but requires Entra authorization;
anonymous access and shared keys are disabled. Apply private endpoints/firewall
rules to match your organization’s network requirements before production use.

## Certificates and secrets

Obtain a certificate and private key from your trusted CA. Its SANs must cover
**all** of these names:

- `ldap-0.ldap-headless.ldap.svc.cluster.local`
- `ldap-1.ldap-headless.ldap.svc.cluster.local`
- `ldap-2.ldap-headless.ldap.svc.cluster.local`
- The client-facing DNS name, e.g. `ldap.internal.example.com`

An appropriate wildcard can cover the three peer names. `tls.crt` must include
the server certificate and any intermediate chain; `ca.crt` contains the trusted
CA chain. Use absolute paths to your certificate and password files:

```sh
kubectl create namespace ldap --dry-run=client -o yaml | kubectl apply -f -
kubectl -n ldap create secret generic ldap-tls \
  --from-file=tls.crt=/secure/path/tls.crt \
  --from-file=tls.key=/secure/path/tls.key \
  --from-file=ca.crt=/secure/path/ca.crt
kubectl -n ldap create secret generic ldap-admin \
  --from-file=/secure/path/admin-password
envsubst '${LDAP_IMAGE} ${LDAP_BASE_DN} ${BACKUP_CLIENT_ID} ${BACKUP_STORAGE_ACCOUNT}' \
  < "$REPO/deploy/ldap.yaml" | kubectl apply -f -
kubectl -n ldap rollout status statefulset/ldap
kubectl -n ldap get pods -o wide
kubectl -n ldap get service ldap
```

The password file must contain at least 16 characters, with **no trailing newline**.
All peers must use the same password and CA. Protect the admin account: it also
authenticates replication. The base DN supports `dc=` components only. Point your
private DNS name at the internal service IP; clients must trust the CA and use
that DNS name, not the IP. Authenticated users can read non-password attributes;
only the root DN `cn=admin,$LDAP_BASE_DN` can modify arbitrary entries, and users
can change their own passwords. Review ACLs for your applications.

Rotate secrets by updating the Kubernetes Secrets and performing a controlled
StatefulSet rollout. Password rotation temporarily interrupts replication until
all peers have restarted. Test and monitor convergence before resuming writes.
Keep the replica count at **three**; replica IDs and peer URLs are configured for
ordinals 0–2. The MDB map limit is 10 GiB per peer; adjust it and storage/scratch
capacity together if needed.

## Migrate an existing directory

1. Inventory source suffixes, schema/OIDs, overlays, ACLs, password hash support,
   entry counts, and application bind/search behavior. This image's schema and
   ACLs are not automatically equivalent to an arbitrary source directory.
2. Rehearse an offline source `slapcat` export and restore. For other LDAP servers,
   use their supported LDIF export and convert schema/passwords as necessary.
   The export's suffix must match `LDAP_BASE_DN`. Do not migrate `cn=config`,
   MDB files, replication settings, or TLS private keys.
3. Deploy Azure infrastructure and secrets, but keep the StatefulSet at zero
   replicas until the seed is staged using the restore procedure below.
4. Quiesce source writes, take a final export, and stage that LDIF on **only**
   `data-ldap-0`. Start the peers; the other two must have empty databases and
   receive the directory through syncrepl.
5. Compare data and password binds on every peer; test writes, deletes, TLS, and
   zone failure/reconnect behavior. Switch application DNS/configuration only
   after convergence. Keep the old source read-only for a planned rollback window;
   never allow independent writes in the old and new directories.

## Regional recovery / LDIF seed

Backups contain sensitive directory attributes and password hashes. Restrict
Blob data access, protect downloaded files, and delete local exports after use.
Versioning and 30-day soft deletion protect accidental deletion; **there is no
automatic expiry policy**. Set retention/lifecycle and backup alerting for your
compliance and cost requirements. Monitor sidecar errors and age of the latest
committed blob; a running pod alone does not prove backup success.

1. Fence the old deployment from clients before promoting a restored copy.
   Inspect storage's last-sync time and list committed blobs through the
   `secondary_blob_endpoint` Terraform output. Use a DR operator identity with
   `Storage Blob Data Reader` and download a selected complete backup:

   ```sh
   az storage blob list --account-name "$BACKUP_STORAGE_ACCOUNT" \
     --container-name ldap-backups --auth-mode login \
     --blob-endpoint "https://${BACKUP_STORAGE_ACCOUNT}-secondary.blob.core.windows.net" \
     --query '[].name' -o tsv
   az storage blob download --account-name "$BACKUP_STORAGE_ACCOUNT" \
     --container-name ldap-backups --name "$BACKUP_BLOB" --auth-mode login \
     --blob-endpoint "https://${BACKUP_STORAGE_ACCOUNT}-secondary.blob.core.windows.net" \
     --file /secure/path/backup.ldif.gz
   gzip -dc /secure/path/backup.ldif.gz > /secure/path/restore.ldif
   ```

   Do not assume the lexicographically latest backup is the most converged peer.
   Check its contents/contextCSNs, integrity, age, and suffix. During planned
   recovery, stop writes and confirm replication before taking a final backup.
2. Provision another cluster in a healthy three-zone region using a **separate
   Terraform state**, a different `name`, and a new unique storage account name.
   Do not apply a region change to the original state during an incident.
   Recreate secrets using your independently secured CA/key/password material.
   Backups contain directory data, not Kubernetes Secrets or custom image/schema.
3. Render the manifest but set `replicas: 0` before applying. Create the first
   PVC, then stage the uncompressed LDIF with a temporary pod mounting it:

   ```sh
   kubectl apply -f - <<'YAML'
   apiVersion: v1
   kind: PersistentVolumeClaim
   metadata:
     name: data-ldap-0
     namespace: ldap
   spec:
     accessModes: ["ReadWriteOnce"]
     storageClassName: ldap-zonal
     resources:
       requests:
         storage: 32Gi
   YAML
   kubectl -n ldap run ldap-restore --image="$LDAP_IMAGE" --restart=Never \
     --overrides='{
       "spec": {
         "securityContext": {
           "runAsNonRoot": true, "runAsUser": 10001, "runAsGroup": 10001,
           "fsGroup": 10001, "seccompProfile": {"type": "RuntimeDefault"}
         },
         "containers": [{
           "name": "ldap-restore", "image": "'"$LDAP_IMAGE"'",
           "command": ["sleep", "3600"],
           "securityContext": {
             "allowPrivilegeEscalation": false, "readOnlyRootFilesystem": true,
             "capabilities": {"drop": ["ALL"]}
           },
           "volumeMounts": [{"name": "data", "mountPath": "/var/lib/ldap"}]
         }],
         "volumes": [{"name": "data", "persistentVolumeClaim": {"claimName": "data-ldap-0"}}]
       }
     }'
   kubectl -n ldap wait --for=condition=Ready pod/ldap-restore --timeout=300s
   kubectl -n ldap cp /secure/path/restore.ldif ldap-restore:/var/lib/ldap/restore.ldif
   kubectl -n ldap delete pod ldap-restore --wait=true
   kubectl -n ldap scale statefulset/ldap --replicas=3
   kubectl -n ldap rollout status statefulset/ldap
   ```

   Use **empty, new PVCs**, never overlay restored data on existing MDB files.
   Startup imports `restore.ldif` only when `data.mdb` does not exist and removes
   the seed after successful import. Other peers obtain their seed over TLS.
4. Verify entries, password binds, writes and deletes through each peer, replication
   convergence, and new backup uploads. Only then change private DNS to the new
   internal service IP. Do not reconnect stale peers; rebuild them from the new
   authoritative directory. Rehearse this runbook to establish measured RPO/RTO.

## Validation and operations

```sh
terraform -chdir="$REPO/deploy" fmt -check
terraform -chdir="$REPO/deploy" validate
kubectl -n ldap logs ldap-0 -c ldap
kubectl -n ldap logs ldap-0 -c backup
LDAPTLS_CACERT=/secure/path/ca.crt ldapsearch \
  -H ldaps://ldap.internal.example.com:636 -x \
  -D "cn=admin,$LDAP_BASE_DN" -W -b "$LDAP_BASE_DN"
```

Before production, drain a node/zone and confirm binds, reads, writes and deletes
on the surviving peers, then verify convergence after recovery. Keep AKS/Kubernetes,
OpenLDAP, container images and certificates maintained. PVCs and disks are retained
on workload deletion; Terraform destruction can still remove resources and backups.
Budget for three nodes, disks, load balancer, retained blobs and cross-region storage.

References: [OpenLDAP replication](https://www.openldap.org/doc/admin26/replication.html),
[OpenLDAP backup](https://www.openldap.org/doc/admin26/maintenance.html),
[Azure storage redundancy](https://learn.microsoft.com/azure/storage/common/storage-redundancy),
[AKS availability zones](https://learn.microsoft.com/azure/aks/reliability-availability-zones-concept),
[AKS workload identity](https://learn.microsoft.com/azure/aks/workload-identity-overview).
