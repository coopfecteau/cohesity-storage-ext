# Domain glossary - Cohesity storage extension

Cohesity renamed several concepts between releases and uses vendor words where the industry uses
different ones. This file pins the terms so sessions do not drift between names. Glossary only - no
implementation detail, no decisions. Decisions live in `.scratch/cohesity-storage-ext/`.

## Cohesity side

**Cluster** - A Cohesity DataPlatform appliance cluster. The namespace root for every other object: all
Cohesity object ids are int64s scoped *to a cluster*, so the same id means different things on two
clusters. Identified by `clusterId`.

**Helios** - Cohesity's multi-cluster control plane. A separate API surface *and a separate RBAC layer*
from the cluster API. Not used in v1; we authenticate directly against a cluster.

**Storage Domain** - A named storage location on a cluster that sets deduplication, compression, erasure
coding and replication policy, and contains Views. **Formerly called a "View Box"**, and the old name
still appears in v1 API paths and in older docs (`GetViewBoxStats`). Always write "Storage Domain";
expect "View Box" when reading Cohesity's own material.

**View** - A file-system share inside a Storage Domain, exposed over NFS, SMB or S3. Views can number in
the thousands, so v1 polls only the top 20 by activity and carries them as *dimensions*, not entities.

**Protection Group** - A backup job: a set of sources, a schedule, and a policy, writing into one Storage
Domain. Cohesity's word for what almost everyone else calls a "job". Use "Protection Group" in code and
config; "backup job" is acceptable in prose aimed at customers.

**Protection Run** - One execution of a Protection Group. Has a terminal status, a start and end time,
and bytes transferred. Runs are **events with instants**, not states with durations - which is why run
outcomes are modelled as a counter plus an age gauge rather than a status gauge.

**Protection Policy** - The retention and schedule template a Protection Group references. Not an entity
in v1.

**Protection Source** - A thing being protected: a VM, database, physical host, NAS share. Tens of
thousands of these can exist on one cluster, which is why they are not entities. Where one corresponds to
something Dynatrace already monitors, the plan is to draw an edge to the *existing* Dynatrace entity
rather than create a Cohesity copy.

**Node** - A physical or virtual member of a Cluster. Not an entity in v1 - no metrics are collected for
it yet.

## Dynatrace side

**Smartscape node** - The current-state entity model. Custom node types must be prefixed `CUSTOM_` or
`EXT_`; this extension uses `EXT_COHESITY_*`. Distinct from the older *classic entity* model
(`dt.entity.*`), which is a lookback view over a query window and is in deprecation.

**Feature set** - An extension-level switch controlling which metrics the EEC ingests. Distinct from the
per-cluster collection toggles in the activation schema, which control which REST calls the extension
*makes*. Two different switches; both must be on for a metric to appear.

**EEC** - Extension Execution Controller. The component that runs the extension, shipped with every
OneAgent and ActiveGate. It supplies the Python interpreter; an extension cannot bring its own.

**Remote activation** - Running an extension on an ActiveGate that reaches out over the network, as
opposed to local activation on a OneAgent host. Cohesity is an appliance with no OneAgent, so this
extension is remote-only.
