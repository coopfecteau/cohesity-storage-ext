/**
 * Keeps the Cohesity host-link inlineLookup tables current.
 *
 * Cohesity knows which VM each backup job protects; Dynatrace knows which host is which VM.
 * Neither knows the other. The extension publishes the first half as a bridge metric
 * (cohesity.protectiongroup.protects, one line per protected VM carrying its BIOS UUID), the
 * HOST entities carry the second half as a serial, and this task joins them and writes the
 * result into the OpenPipeline pipeline shipped with the extension - after which a
 * protection group -> HOST 'protects' edge appears in Smartscape.
 *
 * WHY A LOOKUP TABLE AND NOT A DQL JOIN
 *   An edge needs the target entity's Smartscape id, and a HOST's id is Dynatrace-internal:
 *   it cannot be computed from a UUID. Our own protection-group id CAN be computed from its
 *   id components, so the computed side is ours and the looked-up side is theirs. That is why
 *   the direction here is the inverse of the NetApp precedent this is adapted from.
 *
 * PREREQUISITES
 *   1. The extension is installed and 'collectHostLink' is ON for at least one cluster. It is
 *      OFF by default: the bridge metric is one series per protected VM.
 *   2. A routing entry sends host metrics into the pipeline - see dynatrace/README.md. Without
 *      it the pipeline exists, this task fills it, and nothing ever reaches it.
 *   3. PIPELINE_OBJECT below is set to the settings object id of the shipped pipeline
 *      'extension:cohesity-host-link'. dynatrace/README.md says how to find it.
 *   4. Workflow actor scopes: settings:objects:read, settings:objects:write (to rewrite the
 *      tables), storage:metrics:read and storage:buckets:read (the bridge metric),
 *      storage:smartscape:read (smartscapeNodes HOST).
 *
 * No network path is needed. Unlike the NetApp original this reads everything from Grail and
 * never leaves the tenant, so no EdgeConnect and no credential vault entry are involved.
 *
 * COST AND CADENCE
 *   Which VM a job protects is configuration, not telemetry: it changes when somebody edits a
 *   backup job, which is weekly at most. Hourly is already far more often than the data moves.
 */
import { execution } from '@dynatrace-sdk/automation-utils';
import { queryExecutionClient } from '@dynatrace-sdk/client-query';
import { settingsObjectsClient } from '@dynatrace-sdk/client-classic-environment-v2';

// ---- configure this one ---------------------------------------------------
const PIPELINE_OBJECT = '<pipeline-settings-object-id>';
// ---------------------------------------------------------------------------

const GROUP_PROCESSOR = 'cohesity-host-link.protection-group-lookup';
const CLUSTER_PROCESSOR = 'cohesity-host-link.cluster-lookup';

// How far back to look for the bridge metric. Generous on purpose: the extension emits it on a
// rotation, so a cluster with more VMware groups than one poll's budget needs several polls to
// publish the whole mapping, and a short window would make the table flap between subsets.
const LOOKBACK = '-24h';

/**
 * A BIOS UUID as canonical lowercase 8-4-4-4-12, or '' if the value is not one.
 *
 * THE TWIN of cohesity_storage.domain.normalise_object_uuid - deliberately, because this side
 * and the extension side must reduce the same VM to the same string or the join silently
 * matches nothing. tests/test_host_link.py holds both to the same worked examples; change one
 * and that test fails.
 *
 * The two spellings being reconciled:
 *   Cohesity  00112233-4455-6677-8899-aabbccddeeff
 *   Dynatrace VMware-00 11 22 33 44 55 66 77-88 99 aa bb cc dd ee ff
 *
 * The vendor prefix CANNOT be removed by stripping non-hex characters: 'vmware' contains 'a'
 * and 'e', which are hex digits, so a blind strip leaves 34 of them and the value is rejected.
 * So a leading '<prefix>-' is dropped first, and only when that prefix contains a non-hex
 * character - which is what tells 'VMware-' apart from the first group of a UUID that happens
 * to be all letters, like 'abcdefab-1234-...'.
 *
 * The decimal guard is first for the same reason it is in the Python: 32 decimal digits are
 * also 32 valid hex digits, so a Cohesity-internal int64 id would otherwise normalise into a
 * well-formed UUID that matches no host anywhere.
 */
function normaliseUuid(value) {
  let text = String(value == null ? '' : value).trim().toLowerCase();
  if (!text || /^[0-9]+(:[0-9]+)*$/.test(text)) return '';
  const dash = text.indexOf('-');
  if (dash > 0 && /[^0-9a-f]/.test(text.slice(0, dash))) text = text.slice(dash + 1);
  const digits = text.replace(/[^0-9a-f]/g, '');
  if (digits.length !== 32) return '';
  return [
    digits.slice(0, 8), digits.slice(8, 12), digits.slice(12, 16),
    digits.slice(16, 20), digits.slice(20),
  ].join('-');
}

/**
 * {key: value} as OpenPipeline's [[[keys...],"value"],...] string.
 *
 * A list of groups, not a map: every key resolving to the same value shares one row, which is
 * what keeps the table small when many hosts belong to one protection group. Sorted at both
 * levels so the encoded string is stable - the caller compares it against what is already
 * stored to decide whether to write, and an unstable order would rewrite the settings object
 * on every run. '[]' for an empty input, which restores the inert state the pipeline ships in
 * rather than leaving a stale mapping asserting edges for hosts nothing protects any more.
 */
function lookupTable(pairs) {
  const grouped = new Map();
  for (const [key, value] of pairs) {
    if (!key || !value) continue;
    if (!grouped.has(value)) grouped.set(value, []);
    grouped.get(value).push(key);
  }
  const rows = [...grouped.entries()]
    .sort((a, b) => (a[0] < b[0] ? -1 : a[0] > b[0] ? 1 : 0))
    .map(([value, keys]) => [keys.sort(), value]);
  return JSON.stringify(rows);
}

async function runDql(query) {
  const start = await queryExecutionClient.queryExecute({
    body: { query, requestTimeoutMilliseconds: 60000 },
  });
  let r = start;
  while (r && (r.state === 'NOT_STARTED' || r.state === 'RUNNING')) {
    r = await queryExecutionClient.queryPoll({ requestToken: start.requestToken });
  }
  return (r && r.result && r.result.records) ? r.result.records : [];
}

export default async function ({ execution_id }) {
  await execution(execution_id);

  // 1. The mapping the extension published: one row per (VM uuid, protection group, cluster).
  //    timeseries rather than fetch because the bridge metric is a metric; the aggregation is
  //    meaningless and only the `by` dimensions are read.
  const links = await runDql(
    'timeseries n = avg(cohesity.protectiongroup.protects), ' +
    'by: { `cohesity.object.uuid`, `cohesity.protectiongroup.id`, `cohesity.cluster.id` }, ' +
    `from: ${LOOKBACK}`
  );
  if (!links.length) {
    // Not an error. Either no cluster has collectHostLink on, or it is on and the cluster's
    // objects carry no usable uuid - in which case the extension has already said so, loudly,
    // as an ERROR on the cohesity_storage.diagnostics log channel.
    return { skipped: 'no cohesity.protectiongroup.protects data in the window' };
  }

  // 2. The Dynatrace half. A VMware guest publishes its BIOS UUID as the system serial;
  //    physical hardware publishes a vendor service tag there, which normalises to '' and is
  //    dropped. Most of a mixed estate is not a VMware guest, and that is fine.
  const hosts = await runDql(
    'smartscapeNodes "HOST" | fields id, serial = host.additional_system_info[`system.serial`]'
  );
  const hostByUuid = new Map();
  for (const host of hosts) {
    const uuid = normaliseUuid(host.serial);
    if (host.id && uuid) hostByUuid.set(uuid, host.id);
  }

  // 3. Join. Both tables are keyed on the HOST id, because that is the inlineLookup's source
  //    field; the protection group's node id is then computed from the two values inside the
  //    pipeline, exactly as the extension's own rule computes it.
  const groupPairs = new Map();
  const clusterPairs = new Map();
  const unmatched = new Set();
  const multiplyProtected = new Set();
  for (const row of links) {
    const uuid = normaliseUuid(row['cohesity.object.uuid']);
    const group = row['cohesity.protectiongroup.id'];
    const cluster = row['cohesity.cluster.id'];
    if (!uuid || !group || !cluster) continue;
    const hostId = hostByUuid.get(uuid);
    if (!hostId) {
      // The unmatched majority, and the expected case: most protected VMs are not
      // OneAgent-monitored. No entity is minted and no edge drawn - ticket 16's "degrade
      // quietly" requirement is simply that this branch does nothing.
      unmatched.add(uuid);
      continue;
    }
    const existing = groupPairs.get(hostId);
    if (existing !== undefined && existing !== group) {
      // An inlineLookup holds one value per key, so one job has to win. Lowest group id wins,
      // which is arbitrary but stable; saying which hosts were affected is the honest way to
      // be arbitrary, and a host backed up by two jobs is common on a real estate.
      multiplyProtected.add(hostId);
      if (existing <= group) continue;
    }
    groupPairs.set(hostId, group);
    clusterPairs.set(hostId, cluster);
  }

  const groupTable = lookupTable(groupPairs);
  const clusterTable = lookupTable(clusterPairs);

  // 4. Rewrite ONLY the two tables, on the object as it stands right now. Read-modify-write
  //    rather than a blind put of a remembered shape: the pipeline also carries the node and
  //    edge processors, and overwriting those with a stale copy would silently retire the
  //    edge this whole thing exists to draw.
  const current = await settingsObjectsClient.getSettingsObjectByObjectId({ objectId: PIPELINE_OBJECT });
  const value = current.value;
  const processors = (value.processing && value.processing.processors) || [];
  const groupProc = processors.find((p) => p.id === GROUP_PROCESSOR);
  const clusterProc = processors.find((p) => p.id === CLUSTER_PROCESSOR);
  if (!groupProc || !clusterProc) {
    throw new Error(
      `${PIPELINE_OBJECT} is not the Cohesity host-link pipeline: ` +
      `it has no ${GROUP_PROCESSOR} / ${CLUSTER_PROCESSOR} processor`
    );
  }

  const summary = {
    hosts: hostByUuid.size,
    mapped: groupPairs.size,
    unmatched: unmatched.size,
    multiplyProtected: [...multiplyProtected].sort(),
  };
  if (
    groupProc.inlineLookup.inlineLookupTable === groupTable &&
    clusterProc.inlineLookup.inlineLookupTable === clusterTable
  ) {
    // The common case once the estate settles. Skipping the write keeps the settings object's
    // change history meaningful instead of one entry per hour forever.
    return { unchanged: true, ...summary };
  }

  groupProc.inlineLookup.inlineLookupTable = groupTable;
  clusterProc.inlineLookup.inlineLookupTable = clusterTable;
  await settingsObjectsClient.putSettingsObjectByObjectId({
    objectId: PIPELINE_OBJECT,
    body: { value },
  });

  return { updated: true, ...summary };
}
