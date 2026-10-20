# Cloud Map removal — migration plan

**Status:** proposed, not implemented.
**Driver:** GovCloud deployment + org policy denies `servicediscovery:*`.
**Decision:** replace DNS-based discovery with ECS-API task-IP resolution and
client-side load balancing. No new AWS services.

Rejected alternatives and why:

| Option | Why not |
|---|---|
| ECS Service Connect | Built *on* Cloud Map, so it removes nothing. Its DNS also only resolves from inside another ECS task (Envoy sidecar), which breaks every caller here — all three are Lambdas or AgentCore. `ontology-stack.ts:498` already documents this rejection. |
| Internal ALB | Works in GovCloud, but needs ELB approval and does **not** fix teardown: it swaps runtime-created Cloud Map services for runtime-created target groups + listener rules, the same class of orphaned out-of-CFN resource. Also ~$20/mo + LCU and a ~100-namespace ceiling (default 100 rules/listener, 100 TGs/ALB). |
| Self-managed Route 53 private hosted zone | Reimplements Cloud Map without auto-deregistration — every task roll leaves a stale A record unless an ECS task-state-change reconciler is added. |
| VPC Lattice | Billed per service-hour (~$18/mo *per namespace*). Cost scales with tenants; wrong shape for a per-namespace fleet. |

Enabling fact: an ECS interface VPC endpoint already exists
(`infra/lib/stacks/foundation/network-stack.ts:204`), so no new networking is
needed. `ListTasks`/`DescribeTasks` are already in use by
`packages/control-plane/src/coa_control_plane/namespace/vkg_health.py:67,71`.

---

## 1. Current state

One Cloud Map private DNS namespace, `{prefix}-{env}-services.local`, created at
`network-stack.ts:384` (CFN-owned), used two ways:

**(a) Static — `ontology-engine`.** Registered by CFN via `cloudMapOptions`
(`ontology-stack.ts:511`). Resolved as
`http://ontology-engine.<nsname>:8001` by **three** Lambda consumers:

| Consumer | Env wired at | Code reading it |
|---|---|---|
| api-proxy Lambda | `ontology-stack.ts:1068` | `packages/ontology-engine/src/coa_ontology/api_proxy_handler.py:24` (module-level `os.environ[...]`) |
| `namespaceApiFn` | `namespace-stack.ts:688` | `packages/control-plane/.../namespace/cleanup.py:372` |
| DeleteOntology step | `namespace-deletion-pipeline.ts:230` | `packages/control-plane/.../namespace/delete_handler.py:272` |

**(b) Dynamic — per-namespace VKG fleet.** Created **at runtime, outside CFN** by
`infra/lambda/vkg-reload/index.py:479` (`_ensure_cloud_map`), one Cloud Map
service `vkg-{ns}` per COA namespace, attached via `serviceRegistries` at
`index.py:422`. Resolved by AgentCore through a string rewrite at
`vkg_translator.py:139` (`://vkg.` → `://vkg-{ns}.`).

(b) is what makes teardown fragile: CFN owns the namespace but not the services
inside it, so `DeleteNamespace` fails and network-stack lands in
`DELETE_FAILED`. Three separate code paths exist purely to compensate —
`scripts/destroy.sh:329-390` (step 4), `cleanup.py:268-301`, and the
`servicediscovery:*` grants at `vkg-stack.ts:319` and
`namespace-deletion-pipeline.ts:309-312`.

---

## 2. New component: `coa_common.ecs_discovery`

New file `libs/common/src/coa_common/ecs_discovery.py` (per CLAUDE.md's
"reuse `libs/common`" rule — all four callers need it).

```python
def resolve_service_endpoint(
    cluster: str,
    service_name: str,
    port: int,
    *,
    scheme: str = "http",
    ttl_seconds: int = 60,
    ecs_client: Any | None = None,
) -> str: ...

def invalidate(cluster: str, service_name: str) -> None: ...
```

Behaviour:

1. Module-level cache keyed `(cluster, service_name)` → `(url, expires_at)`,
   TTL 60s by default. Mirrors the old Cloud Map A-record TTL of 10s but
   trades freshness for fewer API calls; the retry path in §3 covers staleness.
2. `list_tasks(cluster=..., serviceName=..., desiredStatus="RUNNING")`, then
   `describe_tasks(cluster=..., tasks=arns)`.
3. Candidate selection — **this is the client-side load balancing** that
   replaces Cloud Map's `MULTIVALUE` round-robin:
   - keep tasks with `lastStatus == "RUNNING"`;
   - prefer those whose `healthStatus == "HEALTHY"`; if none report HEALTHY,
     fall back to any RUNNING task. This deliberately mirrors the existing
     fallback ladder in `vkg_health.py:158-188` so health semantics stay
     consistent across the repo;
   - `random.choice` among the survivors. VKG autoscales 1→3
     (`vkg-reload/index.py:458`), so this matters.
4. IP extraction: `task["attachments"]` where `type == "ElasticNetworkInterface"`
   → `details[]` where `name == "privateIPv4Address"`. This comes back directly
   from `DescribeTasks`, so **no `ec2:DescribeNetworkInterfaces` grant and no
   EC2 call are needed**.
5. No running task → raise a typed error from `coa_common.exceptions`
   (`ServiceUnavailableError`), not a bare `RuntimeError`.
6. Synchronous (boto3). The async caller in §3 must wrap it in
   `asyncio.to_thread` — calling boto3 directly on the event loop would block
   it. Note `coa_common.async_boto_config()` is only a botocore `Config`, not an
   async client; it should still be passed for timeout/retry consistency with
   `cleanup.py:252`.

Unit tests: `libs/common/tests/unit/test_ecs_discovery.py` — stubbed ECS client,
covering healthy-preference, unhealthy fallback, no-task error, cache hit/expiry,
`invalidate`, and malformed-attachment handling. Marker `unit`; 80% floor applies.

---

## 3. Per-file changes

### Infra — remove the namespace

**`infra/lib/stacks/foundation/network-stack.ts`**
- Delete the `servicediscovery` import (line 6), the `serviceNamespace`
  declaration (lines 71-72) and the `PrivateDnsNamespace` block (lines 383-391).

**`infra/lib/stacks/services/vkg-stack.ts`**
- Delete import (14), the `serviceNamespace` prop (39-40), `namespace` /
  `cloudMapNamespaceName` locals (109-110).
- Drop `CLOUD_MAP_NAMESPACE_ID` from the reload Lambda env (252).
- Delete the `servicediscovery:CreateService|ListServices` policy (316-324).
- Replace the endpoint SSM param (452-457) and `CfnOutput` (467-470): publish
  `/vkg/port` instead of a URL. `/vkg/cluster-arn` (446-450) already exists and
  becomes the primary discovery input.

**`infra/lib/stacks/services/ontology-stack.ts`**
- Delete import (15), prop (55-56), `namespace` local (143).
- Remove `cloudMapOptions` from the Fargate service (511-516) and rewrite the
  comment block at 495-502.
- Delete `SsmServiceNamespaceArn` (1016-1021). **Note the comment at 1010-1015:**
  that param was kept to avoid orphaning a cross-stack export during the
  Service-Connect→`cloudMapOptions` migration. Removing it and
  `network-stack`'s namespace in the *same* change is safe because the producer
  is going away too — but this must land as one deploy, not two.
- Replace the `SsmOntologyEndpoint` URL (1023-1026) and `CfnOutput` (1103) with
  cluster-ARN + port + service-name params.
- Replace `ONTOLOGY_ENGINE_ENDPOINT` on api-proxy (1063-1073) with
  `ONTOLOGY_ENGINE_CLUSTER_ARN` / `ONTOLOGY_ENGINE_SERVICE_NAME` /
  `ONTOLOGY_ENGINE_PORT`; delete the now-wrong comment at 1064-1067.
- Add to `apiProxyFn` role: `ecs:ListTasks` + `ecs:DescribeTasks`.
  `DescribeTasks` supports `ecs:cluster` as a condition key; `ListTasks` does
  **not** support resource-level scoping, so it needs `resources: ["*"]` —
  call this out in a comment so a future reviewer doesn't "tighten" it into a
  broken policy.

**`infra/lib/stacks/services/namespace-stack.ts`**
- Replace `ontologyEngineEndpoint` prop (41-47) with the cluster/service/port
  trio; update env wiring at 688 and grant `namespaceApiFn` the two ECS actions.

**`infra/lib/stacks/services/namespace-deletion-pipeline.ts`**
- Delete `cloudMapNamespaceId` prop (98), its env block (264-266) and the
  `servicediscovery:*` policy (305-317).
- Swap `ontologyEngineEndpoint` (100, 229-231) for the trio; grant the ECS
  actions to `deleteOntologyFn`.

**`infra/bin/app.ts`**
- Drop `serviceNamespace` (244, 495) and `cloudMapNamespaceId` (256).
- Replace the hardcoded `.local` URLs at 258 and 408 — these are the two places
  that string-build Cloud Map FQDNs from the prefix.

**`infra/lambda/vkg-reload/index.py`**
- Delete the `sd` client (18), `_ensure_cloud_map` (479-496), the
  `cloud_map_ns_id` read and its presence check (372, 377-379), the
  `registry_arn` call and its error block (388-406), and `serviceRegistries`
  from `create_service` (422).
- Simplification: with no Cloud Map entry created before the task def, the
  "orphaned registry" error path at 389-406 disappears entirely — the
  `_register_task_definition` call can move back inline.

### Application code

**`packages/context-manager/src/coa_serve/config.py`**
- Line 318: replace `VKG_ENDPOINT` with `vkg_cluster_arn` + `vkg_port`
  (`ServiceConfig` fields at 77). Also note `clients/vkg.py:81` has a second
  env fallback, `VKG_SERVICE_URL` — leave it (it is the injectable
  base-URL path the translator now supplies explicitly) but drop it from any
  deployed env.
- Update the `vkg_endpoint=` log field at `main.py:437` and the constructor call
  at `main.py:261-262`.

**`packages/context-manager/src/coa_serve/tier2/ontop/vkg_translator.py`** — the
core change.
- `__init__` (99-125): `vkg_endpoint: str` → `vkg_cluster_arn: str`,
  `vkg_port: int`. Cache becomes keyed by namespace still, but must now also
  hold the resolved URL so a stale entry can be detected.
- `_resolve_client` (129-149) becomes `async`, replacing the `://vkg.` rewrite:
  resolve `f"{prefix}-vkg-{ns}"` via
  `await asyncio.to_thread(resolve_service_endpoint, ...)`. Keep the existing
  64-entry bound (127) and **`await client.close()` on eviction** — the current
  code at 146-147 drops the client without closing its pooled
  `httpx.AsyncClient` (`clients/vkg.py:86-96`), leaking a connection pool. Worth
  fixing while here.
- Single call site, `line 184`, is already inside an async function → add `await`.
- **Stale-IP retry.** `VKGClient.translate` already catches
  `httpx.ConnectError` at `clients/vkg.py:146`. Wrap the 184-185 call: on a
  connect failure, `invalidate()`, close and drop the cached client, re-resolve,
  retry **once**. Without this, every VKG deployment roll surfaces as a failed
  query until the 60s TTL lapses — this is the single most important behavioural
  difference from Cloud Map's 10s TTL and must not be skipped.

**`packages/ontology-engine/src/coa_ontology/api_proxy_handler.py`**
- Line 24 is a module-level `os.environ["ONTOLOGY_ENGINE_ENDPOINT"]` evaluated at
  import. It must become a per-invocation resolver call; the module-level
  `ENDPOINT` constant goes away. Call sites include `line 184`
  (`{ENDPOINT}/readiness`) plus the main proxy path. Update the module docstring
  (4-12), which still says "Service Connect".

**`packages/control-plane/src/coa_control_plane/namespace/cleanup.py`**
- Delete the Cloud Map block (268-301) and the `cloud_map_ns_id` read (247).
  Update the `Steps:` docstring (237-243) — step 2 no longer exists.
- Line 372: resolve the ontology-engine endpoint via the helper.

**`packages/control-plane/src/coa_control_plane/namespace/delete_handler.py`**
- Line 272: same endpoint resolution change.

**`packages/control-plane/src/coa_control_plane/namespace/vkg_health.py`**
- No change needed. Already ECS-API based; it becomes the precedent the new
  helper is modelled on.

### Scripts

**`scripts/destroy.sh`**
- Delete step 4 entirely: the header comment (20-28), `CLOUDMAP_WAIT_MAX_SECONDS`
  (97) and the whole block at 329-390. Renumber the remaining steps (the file
  documents "seven steps"; it becomes six).
- Step 3 (delete VKG ECS services) stays — those are still created outside CFN.

**`Makefile`** — drop `SCL_CLOUDMAP_WAIT_MAX_SECONDS` from the env list (127).

### Tests

| File | Change |
|---|---|
| `infra/test/foundation/network-stack.test.ts:319` | Delete the `*-services.local` namespace assertion; add a negative assertion that no `AWS::ServiceDiscovery::*` resource is synthesized. |
| `infra/test/services/vkg-stack.test.ts:35,56,73,201` | Drop `serviceNamespace` from all four `VkgStack` constructions; assert the `servicediscovery` policy is gone. |
| `infra/test/services/ontology-stack.test.ts` | Drop the prop; assert no `cloudMapOptions` on the service and that the ECS grants exist. |
| `infra/test/services/namespace-deletion-pipeline.test.ts` | Drop `cloudMapNamespaceId`; assert the `servicediscovery:*` statement is absent. |
| `infra/test/services/serve-stack.test.ts:57` | `vkgEndpoint: "http://vkg...local:8080"` → cluster ARN + port. |
| `infra/test/lambdas/test_vkg_reload_handler.py` | Remove `servicediscovery` stubbing and `_ensure_cloud_map` cases; assert `create_service` is called **without** `serviceRegistries`. |
| `packages/control-plane/tests/unit/test_vkg_lifecycle.py` | Drop Cloud Map deletion assertions. |
| `packages/control-plane/tests/unit/test_namespace_cleanup.py:219,234,248` | Endpoint env → cluster/service/port. |
| `packages/control-plane/tests/unit/test_delete_namespace.py:40,387` | Same. |
| `packages/ontology-engine/tests/unit/test_api_proxy_handler.py:18,124,312,325,404` | Endpoint env → resolver; keep the 312/325 assertions that the internal hostname never leaks into an error body — **now an IP, so the leak check matters more, not less**. |
| `packages/context-manager/tests/unit/` (vkg translator suite) | New: healthy-task resolution, client-side pick, the stale-IP invalidate-and-retry path, eviction closes the client. Delete the "endpoint does not contain `://vkg.`" `ValueError` test (141-144). |
| new `libs/common/tests/unit/test_ecs_discovery.py` | See §2. |

### Docs

- `external-docs/content/deploying.md:1268` — remove the Cloud Map row from the
  AWS-services table.
- `infra-docs/AWS-SERVICES.md:53` — same.
- `infra-docs/COA-Redeployment-Guide.md:234` — "seven steps" → six; drop
  "delete the Cloud Map services".
- `infra-docs/diagrams/destroy-explorer.html` — two references in the teardown
  diagram.
- `infra/README.md` — teardown-ordering section, if it enumerates step 4.

`tests/unit/test_doc_accuracy.py` at the repo root may assert on these tables;
run `uv run pytest tests/unit -q` explicitly (it is not an Nx project).

---

## 4. Risks and sequencing

1. **One-deploy constraint.** Removing `network-stack`'s namespace and
   `ontology-stack`'s `SsmServiceNamespaceArn` must land together (see the
   comment at `ontology-stack.ts:1010-1015`). Splitting them across deploys
   risks a CFN export still being imported.
2. **Existing deployed stacks** have live Cloud Map services created by the old
   reload Lambda. On the upgrade deploy, CFN will try to delete the namespace
   while those remain. **Run the current `destroy.sh` step 4 logic once, as a
   one-off cleanup, before deploying this change** — or deploy to a fresh stack.
   This is the main operational gotcha.
3. **AgentCore SG egress.** `serve-stack.ts:187-191` sets
   `allowAllOutbound: false`. Verify 443 egress to the ECS interface endpoint SG
   exists; it is likely already covered by the existing AWS-API rules, but it is
   a hard failure if not.
4. **Throttling.** `DescribeTasks` at high namespace counts. The 60s TTL cache
   plus the 64-entry client bound keeps this to roughly one call pair per
   namespace per minute. Revisit if namespace count exceeds a few hundred.
5. **Loss of DNS indirection.** Callers are now coupled to ECS. If VKG ever
   moves off Fargate, the resolver breaks. Accepted trade-off; the alternative
   (ALB) was rejected above.

## 5. Verification

```bash
make lint                      # version drift + ruff + mypy + tsc + prettier
make test                      # nx run-many -t test
uv run pytest tests/unit -q    # root cross-package checks, not in the Nx graph
cd infra && npx jest           # stack snapshots
```

Then grep for residue — this should return nothing outside this plan document:

```bash
grep -rni "cloudmap\|servicediscovery\|services\.local" \
  --include='*.ts' --include='*.py' --include='*.sh' .
```

Deployed verification needs a dev stack: `make deploy-dev`, create a namespace,
publish an ontology, confirm the VKG service comes up and a Tier-2 SPARQL query
resolves; then `make destroy-dev` and confirm network-stack deletes cleanly
without the step-4 workaround.
