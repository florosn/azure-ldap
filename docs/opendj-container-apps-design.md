# OpenDJ on Azure Container Apps: deployment design

## Status and scope

This is a **proof-of-concept design**, not a production-ready deployment. It
proposes a private, writable LDAP service using OpenIdentityPlatform OpenDJ,
Azure Container Apps and PostgreSQL. It is an alternative to, not a replacement
for, the repository's existing OpenLDAP/AKS architecture.

The design assumes a single OpenDJ instance, LDAPS-only client access, and
private connectivity from Azure and on-premises. A single instance does not
provide LDAP high availability. Azure PostgreSQL availability does not by
itself make the LDAP application highly available.

## Architecture

```text
On-premises clients                     Azure client VNets
      | VPN / ExpressRoute                  | peering
      +------------------+------------------+
                         | private DNS; LDAPS TCP 636
                 +-------v----------------------------------+
                 | OpenDJ workload VNet                     |
                 |                                          |
                 |  Container Apps infrastructure subnet    |
                 |  +------------------------------------+  |
                 |  | Internal Container Apps environment|  |
                 |  | internal load balancer             |  |
                 |  |   -> OpenDJ app (TCP 1636)         |  |
                 |  +-----------------+------------------+  |
                 |                    | PostgreSQL TLS 5432 |
                 |  Private endpoints subnet                |
                 |  +-----------------v------------------+  |
                 |  | PostgreSQL private endpoint        |  |
                 |  | Optional ACR / Key Vault endpoints |  |
                 |  +------------------------------------+  |
                 +------------------------------------------+
                          | private endpoint
                  Azure Database for PostgreSQL
```

PostgreSQL is the proposed durable store for LDAP entries via OpenDJ's JDBC
backend. The upstream [JDBC example](https://github.com/OpenIdentityPlatform/OpenDJ/blob/master/opendj-doc-generated-ref/src/main/asciidoc/how-to/jdbc-backend.adoc)
demonstrates PostgreSQL, but does not establish support for this Azure topology.
Prove compatibility with the chosen OpenDJ release, PostgreSQL version and
JDBC driver before production. Do not assume the stock container bootstraps
this backend automatically.

## Network and VNet design

Use a dedicated workload VNet, optionally connected to an existing hub VNet
for DNS, firewall and hybrid connectivity. The CIDRs below are examples only;
allocate non-overlapping ranges with the network team, including any peered,
on-premises and Container Apps reserved address ranges.

| Network | Example | Purpose |
| --- | --- | --- |
| Workload VNet | `10.80.0.0/16` | Hosts the Container Apps environment and private endpoints. |
| `snet-container-apps` | `10.80.0.0/23` | Dedicated to a workload profiles Container Apps environment; delegate to `Microsoft.App/environments`. |
| `snet-private-endpoints` | `10.80.2.0/27` | PostgreSQL private endpoint; optionally endpoints for ACR and Key Vault. |
| Hub VNet | Existing, non-overlapping | Optional shared DNS resolver, firewall and VPN/ExpressRoute gateway. |
| Client VNets | Existing, non-overlapping | Application clients, connected through direct or hub-and-spoke peering. |

The Container Apps subnet is exclusive to the environment. The workload
profiles environment requires at least `/27`; `/23` above reserves growth and
revision overlap capacity. Size for the selected profile, infrastructure
reservations and planned scale before creation; the subnet cannot be resized
after the environment is created. Do not reuse these delegation rules for a
legacy Consumption-only environment.

Create an **internal** Container Apps environment in the workload subnet.
Configure the OpenDJ app with **external TCP ingress relative to the
environment**, exposed port `636` and container target port `1636`. This
publishes it on the environment's internal load balancer, not the public
internet. Limit access to approved client networks with supported network
controls, verified effective routes and application authentication. Never
expose the OpenDJ admin or replication ports to clients.

Use PostgreSQL Flexible Server with **public access disabled** and a private
endpoint in `snet-private-endpoints`; select a server networking mode that
supports this option. This is separate from the alternative VNet-injected
PostgreSQL mode, which requires its own delegated subnet and DNS setup.
Configure NSGs, private endpoint policies and routing as supported by Azure;
verify effective behavior rather than assuming an NSG on the endpoint subnet
alone blocks every path.

Hybrid connectivity requires working routes in both directions. Enable gateway
transit/use of remote gateways where applicable; VNet peering is not
transitive. Allow LDAPS TCP 636 from approved client ranges, PostgreSQL TLS TCP
5432 from the workload, and DNS to the chosen resolvers. Permit required Azure
Container Apps infrastructure and approved outbound dependencies, including
registry image pulls, identity and monitoring. An internal environment
prevents public ingress; it does **not** eliminate outbound internet access.
Use private endpoints where supported or controlled firewall/NAT egress.

## DNS and TLS

- Create the Container Apps environment's private DNS zone for its generated
  default domain and point the wildcard record to the internal environment
  static IP. Link it to the workload VNet and client VNets that use Azure DNS.
  Forward the relevant zone through Azure DNS Private Resolver or equivalent
  from on-premises and custom DNS networks.
- Use the app's ingress FQDN as the initial LDAP endpoint. Provision an OpenDJ
  server certificate whose SAN contains the exact client-facing name, and have
  clients validate its chain and hostname. TCP ingress passes TLS to OpenDJ;
  OpenDJ terminates LDAPS. Validate any desired custom LDAP hostname and TCP
  routing in a proof of concept rather than assuming HTTP custom-domain
  behavior applies.
- Link the appropriate PostgreSQL private endpoint DNS zone to the workload
  VNet and configure conditional forwarding from custom DNS as needed. Use
  the normal PostgreSQL server hostname in the JDBC connection, verify it
  resolves to the private endpoint, and require TLS with certificate and
  hostname verification. Do not use an IP address to bypass verification.
- If ACR or Key Vault use private endpoints, configure their respective private
  DNS zones and forwarding separately. Test name resolution from the workload
  and all intended client networks.

## OpenDJ deployment and data

1. Select and pin an OpenIdentityPlatform OpenDJ release and image digest;
   verify its JDBC driver and backend capabilities against the upstream
   documentation and the chosen PostgreSQL version. Build a controlled image
   or bootstrap process if needed. Keep credentials and keys out of images,
   source control and deployment logs.
2. Create PostgreSQL privately, enable appropriate backups and retention, and
   create a least-privilege database identity. Provision a JDBC backend for the
   directory suffix; seed or import data using OpenDJ-supported tools.
   Preserve and review schemas, ACLs and password-hash compatibility when
   migrating from OpenLDAP.
3. Configure OpenDJ for LDAPS, a trusted certificate and no plaintext LDAP
   exposure. Store sensitive configuration in Key Vault or Container Apps
   secrets; restrict access and plan certificate and credential rotation.
   The upstream JDBC example stores credentials in the backend configuration:
   protect that configuration at rest and in backups.
4. The upstream container expects an instance directory at `/opt/opendj/data`,
   including configuration and other state. Establish which files must persist
   and a tested secure recreation or persistence mechanism before deployment.
   PostgreSQL holds directory entries, not necessarily every piece of server
   state. Do not assume a live embedded database is safe on Azure Files, or
   assume ephemeral storage survives replica replacement.
5. Set minimum and maximum replicas to one and disable scale-to-zero. This
   controls ordinary scaling, **not** writer exclusivity: Container Apps can
   overlap old and new revisions even in single-revision mode. Before
   deployment, establish and test a fencing or stop-before-start procedure
   that prevents simultaneous access to state unless the exact backend and
   bootstrap combination is proven safe. Accept downtime during replacement.
6. Configure startup, readiness and liveness probes suited to the chosen image.
   Readiness must establish that bootstrap is complete and the intended suffix
   and database are usable, not just that a TCP port is open. Do not expose
   privileged probe credentials or log directory data.

## Security, operations and recovery

- Use managed identities for supported Azure service access, including private
  registry pulls and Key Vault references; use a dedicated least-privilege
  PostgreSQL identity for JDBC. Restrict administrative access and audit it.
- Send application/platform logs and metrics to Azure Monitor. Alert on LDAP
  bind/search/write failures, unready replicas, database connectivity,
  certificate expiry and failed or stale backups. Avoid sensitive attributes,
  credentials and password hashes in logs.
- Use PostgreSQL automated backups and point-in-time recovery where supported;
  independently protect OpenDJ configuration, schema, certificates and secrets.
  Test a consistent restore of both database and server configuration into an
  isolated environment. Define measured RPO/RTO rather than assuming zero loss.
- For regional recovery, fence the old endpoint, restore database and OpenDJ
  configuration in a prepared region, validate data and TLS, then switch private
  DNS. Account for backup/replication lag and DNS/client reconnect time.
- Budget for Container Apps environment and compute, PostgreSQL compute/storage
  and backup, private endpoints, networking, DNS, registry, secrets and logs.

## Deployment and acceptance sequence

Use a separate Bicep deployment with parameterized names, region, CIDRs,
private DNS, image digest and resource sizes. First obtain subscription quota,
network address allocation, CA certificates and permissions. Then provision:

1. VNet, dedicated subnets, connectivity, routes, DNS and required endpoints.
2. Private PostgreSQL, registry, Key Vault and monitoring resources.
3. Internal Container Apps environment and the OpenDJ app with TCP ingress.
4. JDBC backend and protected server configuration; seed the directory once.
5. Private DNS records, application access controls, alerts and backup policy.

Before calling the design production-ready, demonstrate:

- Approved VNet and on-premises clients resolve the intended private IP and
  successfully perform certificate-verified LDAPS binds, searches, adds,
  modifications and deletes. Unapproved networks and the public internet
  cannot connect; plaintext LDAP is unavailable.
- PostgreSQL resolves privately and validates its certificate; loss and
  recovery of database connectivity produces clear failure and recovery.
- Acknowledged writes survive a clean restart and an unplanned replica
  replacement, with configuration and credentials recovered correctly.
- Revision changes cannot create unsafe concurrent writers. Exercise an
  interrupted deployment and prove the fencing procedure.
- Restore database and server configuration from backups in isolation, verify
  directory entries and password binds, and measure recovery time and data loss.
- Validate schema, ACLs, password policy, supported LDAP controls and expected
  performance against actual client workloads and the selected OpenDJ release.

## References

- [Container Apps VNet integration and subnet sizing](https://learn.microsoft.com/en-us/azure/container-apps/custom-virtual-networks)
- [Container Apps ingress](https://learn.microsoft.com/en-us/azure/container-apps/ingress-overview)
- [Private DNS for Container Apps](https://learn.microsoft.com/en-us/azure/container-apps/private-endpoints-with-dns)
- [Container Apps storage mounts](https://learn.microsoft.com/en-us/azure/container-apps/storage-mounts)
- [Container Apps revision behavior](https://learn.microsoft.com/en-us/azure/container-apps/revisions)
- [OpenDJ container and persistence](https://github.com/OpenIdentityPlatform/OpenDJ/blob/master/opendj-packages/opendj-docker/README.md)
- [OpenDJ JDBC backend example](https://github.com/OpenIdentityPlatform/OpenDJ/blob/master/opendj-doc-generated-ref/src/main/asciidoc/how-to/jdbc-backend.adoc)
