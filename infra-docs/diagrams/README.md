# Deploy / destroy flow diagrams

Same two flows in two formats — Mermaid for pasting into Miro, and interactive
HTML for exploring.

| File                    | Covers                                                            |
| ----------------------- | ----------------------------------------------------------------- |
| `deploy-flow.mmd`       | `make deploy-dev` — 4 phases, 16 stacks, dependency graph by level |
| `destroy-flow.mmd`      | `make destroy-dev` — 7 steps, gates, reverse-order teardown       |
| `deploy-explorer.html`  | the deploy flow, interactive                                      |
| `destroy-explorer.html` | the teardown, interactive                                         |

## The interactive explorers

Open either `.html` file in any browser. Self-contained — no build, no install,
no network. Light and dark themes, with a toggle that overrides the OS setting.

Both carry the same four interactions:

- **Click any node** for a side panel: what it creates or deletes, what it waits
  on and *why* each edge exists, and what it blocks. Dependencies in the panel
  are clickable, so you can walk the graph.
- **Step through** with Prev / Play / Next. The deploy explorer walks the 16
  stacks in order; the destroy explorer walks all 24 beats — gates, the 6
  out-of-CFN steps, every stack inside step 7, then verification — switching
  views automatically as it crosses from steps into stacks.
- **Highlight dependency paths.** Selecting a stack dims everything outside its
  closure: upstream in blue, downstream in orange. In the destroy explorer the
  framing inverts to what must be destroyed *first*.
- **Toggle sequential vs parallel.** `Levels` groups stacks by dependency level
  (what could run together); `Deploy order` / `Sequence` shows the one-at-a-time
  order CDK actually uses.

Plus a tab each for the parallelism question (deploy) and the wait budgets
(destroy), and a **Table** tab — the WCAG-clean twin of each graph, so nothing is
reachable only by colour or hover.

Each dependency is tagged `prop` or `explicit`. `prop` means CloudFormation
infers the edge from a cross-stack resource reference; `explicit` means an
`addDependency()` call was needed because the value travels through an SSM
dynamic reference CDK cannot trace. That distinction is the thing most likely to
bite when adding a stack — miss an `explicit` edge and a fresh-account deploy
bakes a dummy value into a Lambda env var and 502s until redeploy.

> Edges out of `network`, `auth` and `authnz` are drawn faded. They feed almost
> every stack, and at full strength they bury the structural chain. Selecting a
> node restores them.

## Getting the Mermaid into Miro

1. Open the board → **Miro AI / Diagramming** → *Diagram from code* (Mermaid).
2. Paste the file contents (drop the leading `%%` comment block if the importer
   complains — those are source notes, not rendered output).
3. Preview first at <https://mermaid.live> to confirm the render before importing.

If Miro's Mermaid version rejects `direction LR` inside a nested `subgraph`
(supported from Mermaid 9.1), delete those `direction` lines — layout changes,
content doesn't.

## Source of truth

Built from the code, not from prose docs:

- `Makefile` — `deploy-dev`, `deploy-serve`, `destroy-dev` targets
- `scripts/deploy.sh`, `scripts/preflight-deploy.sh`
- `scripts/destroy.sh`
- `infra/bin/app.ts` — every `addDependency()` and prop-derived stack reference

The graph in the HTML files is a **hand-authored snapshot**, not generated. When
`infra/bin/app.ts` changes, update the `STACKS` array near the top of each
`<script>` block. The level numbers are checkable: every `level` must equal
`1 + max(level of its deps)`, which is how the current data was verified.

## Key facts the diagrams encode

**Deployment is sequential, not parallel.** The `addDependency()` graph has 10
topological levels and allows parallelism — 5 stacks at L0, 2 each at L6 and L7.
`cdk deploy --all` in `deploy.sh` passes no `--concurrency`, so CDK's default of
1 applies: one stack at a time. Raising it is a one-flag change, but L1→L5 is a
hard serial chain (`storage → vkg → namespace → serve → sources`), so the
realistic saving is small and the two long poles (`storage` ~15 min, `serve`
~10 min) are both inside it.

**The destroy script's first 6 steps exist because CloudFormation doesn't own
those resources.** AgentCore Runtimes and their ENIs, VKG's per-namespace ECS
services, Cloud Map services inside a CFN-owned namespace, the DataZone domain's
retained children, and the connector stacks in separate CDK apps. Only step 7 is
CFN's job, and it runs in exact reverse of the deploy graph.

**Two hard stops in teardown.** Step 2 (AgentCore ENIs still attached after the
budget) and step 3 (`delete-service` API failure) exit rather than continue —
proceeding would just fail inside `cdk destroy` with `DependencyViolation` or
`ClusterContainsServicesException`. Steps 1–6 are idempotent, so re-running
resumes.

**Cross-region.** `*-edge-waf` always deploys to `us-east-1` (CloudFront-scope
WebACL), so a non-`us-east-1` deploy needs both regions CDK-bootstrapped.

## Known doc drift

`infra/README.md` is stale on both counts and was not used as a source:

- Its teardown section lists **5** destroy steps; `destroy.sh` has **7** (Cloud
  Map services and connector stacks were added since). It also says the step-2
  ENI wait is "currently disabled in `destroy.sh` (commented out)" — it is
  active, at `scripts/destroy.sh:181-239`.
- Its "Deployment Architecture" stack table lists `coa-dev-structured`, which no
  longer exists, and omits `metric-service`, `ontology`, `data-layer`, `mcp` and
  `edge-waf`. The `~Deploy Time` column is the only place those estimates exist,
  so the two quoted above are indicative, not measured.
