# COA Redeployment Guide (Ubuntu, us-east-1, dev)

How to redeploy the Context Ontology Accelerator into an AWS account it has run in before, and how to set the Neptune and OpenSearch Serverless sizes for a low-cost dev environment.

Written against repo **v0.3.4** (mirror-sync commit `c84a304`) — the version this team is pinned to. Line numbers below refer to that version.

> **Pinned on purpose.** `main` tracks ongoing development. Everything in section 4 is version-sensitive: the Neptune sizing knob changed after v0.3.4, so if you move to a newer tag, re-check section 4 before trusting it.

---

## 1. What you can skip on a redeploy

| Step from the first deploy | Redo it? |
| --- | --- |
| Install toolchain (AWS CLI, Docker, Node, pnpm, uv, Java, Maven) | No |
| Docker group / ARM64 emulation | No (re-run the `setfacl` workaround after a reboot if you used it) |
| Bedrock model access, service quotas | No |
| Clone the repo | No. Stay on the pinned tag unless you're deliberately upgrading (section 3). |
| `cdk bootstrap` | Only if the `CDKToolkit` stack was deleted (section 2) |
| SSM deploy config `/coa/config` | Only if it was deleted (section 2) |

---

## 2. Check the environment

Environment variables don't survive a reboot or a new terminal, so set them again:

```bash
cd ~/Projects/Boeing-COA/context-ontology-accelerator

export AWS_PROFILE=<iam-user-profile>
export AWS_DEFAULT_REGION=us-east-1
export CDK_DEFAULT_REGION=us-east-1
export SCL_SMUS_ADMIN_ARNS=<sso-role-arn>      # arn:aws:iam::<acct>:role/aws-reserved/sso.amazonaws.com/...

aws sts get-caller-identity                   # right account?
docker ps                                     # Docker reachable without sudo?
```

On v0.3.4 there is no `SCL_NEPTUNE_INSTANCE_CLASS` variable — `scripts/deploy.sh` doesn't read one (see 4.1), so there's nothing to unset. The variables `deploy.sh` does translate into CDK context are listed at `scripts/deploy.sh` lines 33–46.

Check what's still in the account:

```bash
aws cloudformation describe-stacks --stack-name CDKToolkit --query "Stacks[0].StackStatus"
aws ssm get-parameter --name /coa/config --query Parameter.Value
aws cloudformation list-stacks \
  --query "StackSummaries[?starts_with(StackName,'coa-') && StackStatus!='DELETE_COMPLETE'].[StackName,StackStatus]" \
  --output table
```

**If `CDKToolkit` is missing**, bootstrap again. First delete any leftover `cdk-hnb659fds-assets-<account>-us-east-1` S3 bucket, or the bootstrap fails because the bucket already exists.

```bash
cd infra && npx cdk bootstrap aws://$(aws sts get-caller-identity --query Account --output text)/us-east-1 && cd ..
```

**If `/coa/config` is missing**, write it again:

```bash
aws ssm put-parameter --name /coa/config --type String --overwrite \
  --value '{"initialAdminEmail":"<you@yourcompany.com>"}'
```

**If any `coa-*` stack still exists**, either destroy it first (section 7) or let the deploy update it.

---

## 3. Update the code

```bash
git fetch --tags
git checkout v0.3.4                 # the pinned version; see the note in section 4.2 before changing tags
git checkout -b dev-small-storage   # the Neptune change in 4.2 is a code edit, so work on a branch

make generate                       # Smithy codegen
bash ./scripts/setup-dev.sh         # use bash: the script's #!/usr/bin/env sh breaks on Ubuntu's dash
```

If `pnpm -v` no longer prints 10.x afterwards, reinstall it with `sudo npm install -g pnpm@10`.

---

## 4. Change the Neptune and OpenSearch size

**On v0.3.4 the two knobs work differently, and this is the most important thing in this guide:**

| | How you set it on v0.3.4 |
| --- | --- |
| OpenSearch Serverless OCU | CDK context — `infra/cdk.json` (4.3). No code change. |
| Neptune instance class | **Code edit** — `storage-stack.ts` line 118 (4.2). There is no context override. |

Putting `neptune_instance_class` in `cdk.json` on v0.3.4 does **nothing**: no code reads that key, CDK does not warn about unused context, and the deploy quietly creates a `db.r8g.large`. That is the expensive failure mode this section exists to prevent — you would believe you had shrunk Neptune and still be billed for `db.r8g.large`.

### 4.1 Where it lives in the code

Both live in **`infra/lib/stacks/foundation/storage-stack.ts`**.

Neptune instance, lines 113–121 — the class is a literal, with no `tryGetContext` call:

```ts
const neptuneInstance = new neptune.CfnDBInstance(
  this,
  "NeptunePrimaryInstance",
  {
    dbInstanceIdentifier: this.prefixed("neptune-primary"),
    dbInstanceClass: "db.r8g.large",          // line 118 — hardcoded on v0.3.4
    dbClusterIdentifier: neptuneCluster.dbClusterIdentifier!,
  },
);
```

OpenSearch Serverless capacity, lines 222–224 — both read from context:

```ts
const maxOcu = Number(this.node.tryGetContext("aoss_max_ocu") ?? 96);   // default max 96 OCU
// NEXTGEN valid min OCU values: 0, 2, 4, 8, 16, or multiples of 16.
const minOcu = Number(this.node.tryGetContext("aoss_min_ocu") ?? 2);    // default min 2 OCU
```

`scripts/deploy.sh` (lines 33–46) turns `SCL_*` environment variables into `--context` flags. There is **no** entry for Neptune or for the OpenSearch settings; the nearest neighbour is `SCL_LAMBDA_RESERVED_CONCURRENCY` → `lambda_reserved_concurrency` at line 43.

### 4.2 Neptune: edit the file (one-line change)

```bash
# infra/lib/stacks/foundation/storage-stack.ts, line 118
-        dbInstanceClass: "db.r8g.large",
+        dbInstanceClass: "db.t4g.medium",
```

Two things to know before you commit it:

- **One unit test fails.** `infra/test/foundation/storage-stack.test.ts` line 66 is `it("creates a Neptune instance with r8g.large")` and asserts `DBInstanceClass: "db.r8g.large"` at line 69. Either update that expectation in the same commit, or expect `pnpm nx run infra:test` (and so `make test`) to fail on exactly that test. It does not block the deploy.
- **Keep it on a branch** (`dev-small-storage` from section 3), so pulling a future tag shows it as a conflict you must re-decide rather than silently losing it.

If you'd rather not carry a code change, the alternative is to backport the newer context lookup — but that is a larger edit to a pinned branch, and this one line is the smaller risk.

### 4.3 OpenSearch: set the values in `infra/cdk.json`

Values in `cdk.json` apply to every `cdk` command, so `make deploy-dev` and `make destroy-dev` both see them.

```json
{
  "app": "npx tsx bin/app.ts",
  "context": {
    "env": "dev",
    "//": "Emit templates without indentation. ... (leave this text as it is)",
    "@aws-cdk/core:suppressTemplateIndentation": true,
    "aoss_min_ocu": 0,
    "aoss_max_ocu": 4
  }
}
```

Put a comma after each entry except the last. Leave the existing `env`, `//` and `@aws-cdk/core:suppressTemplateIndentation` entries alone — the indentation setting is what keeps the API stack's template under CloudFormation's 1,000,000-byte limit. **Do not add `neptune_instance_class` here** (see the top of section 4).

### 4.4 Choosing the values

| Setting | Default | Low-cost dev | Notes |
| --- | --- | --- | --- |
| Neptune class (code edit, 4.2) | `db.r8g.large` | `db.t4g.medium` | Smallest Neptune class (burstable, 4 GB). Fine for small dev graphs. CPU above the baseline is billed as burst credits. If it's rejected, try `db.t3.medium`. Heavy concurrent SPARQL can run out of memory even on `db.r8g.large`. |
| `aoss_min_ocu` | `2` | `0` | Valid: 0, 2, 4, 8, 16, or a multiple of 16, up to 1696. With `0` the collection scales to zero when idle. The first query after idle takes about 10 s, and bulk ingestion can be throttled (HTTP 429), which fails the ingest loudly rather than silently half-writing. Use `2` for ingest-heavy work. |
| `aoss_max_ocu` | `96` | `4` | Cost cap. Same valid set as min, except it cannot be `0`, and must be ≥ min. Raise to `8` if ingestion keeps getting throttled. |

Both OCU values are validated at **synth time** (`storage-stack.ts` lines 245–268), so a bad number — `3`, `6`, negative, or min above max — fails immediately with a readable error instead of after a slow CloudFormation update. `aoss_min_ocu: 0` additionally emits a synth warning (`AOSS min OCU is 0: indexing capacity can scale to zero…`); that warning is expected and is not an error.

You can't turn off OpenSearch standby replicas: AWS requires `standbyReplicas: ENABLED` for NEXTGEN collections, and the stack hardcodes it.

### 4.5 Verify before deploying

`cdk synth <stack>` prints YAML, so check the JSON copy it writes to `cdk.out`:

```bash
pnpm --filter coa-infra exec cdk synth coa-dev-storage \
  --context smus_admin_principal_arns=$SCL_SMUS_ADMIN_ARNS > /dev/null 2>&1

grep -o '"DBInstanceClass":"[^"]*"\|"MinIndexingCapacityInOcu":[0-9]*\|"MaxIndexingCapacityInOcu":[0-9]*' \
  infra/cdk.out/coa-dev-storage.template.json
```

Expected, with the 4.2 edit applied and the 4.3 context set:

```text
"DBInstanceClass":"db.t4g.medium"
"MinIndexingCapacityInOcu":0
"MaxIndexingCapacityInOcu":4
```

**If `DBInstanceClass` still says `db.r8g.large`**, the 4.2 file edit isn't in place — check that you didn't instead add `neptune_instance_class` to `cdk.json`, which has no effect on v0.3.4.

The deprecated-API warnings are harmless. `grep` works on one line because templates are emitted unindented (4.3).

---

## 5. Deploy

```bash
tmux new -s coa
make deploy-dev 2>&1 | tee deploy.log
```

- **Leave it running:** detach with **Ctrl+b**, then **d**. Reattach with `tmux attach -t coa`. Don't type `exit`, and don't press Ctrl+d or Ctrl+c.
- **Follow progress from another terminal:** `tail -f deploy.log`
- **What happens:** preflight, build and synth run first. Then all images and Lambda bundles are built and uploaded (unchanged ones are skipped if `CDKToolkit` survived). Then the 16 stacks deploy **one at a time**, in dependency order, and the Databricks connector deploys last.
- **How long:** about 1.5 h of CloudFormation time. A smaller Neptune doesn't change that much.
- **If it stops partway** (reboot, expired token): set the variables again and rerun `make deploy-dev`. Finished stacks are skipped.

---

## 6. After the deploy

```bash
aws cloudformation describe-stacks --stack-name coa-dev-web \
  --query "Stacks[0].Outputs[?OutputKey=='WebAppUrl'].OutputValue" --output text
aws cloudformation describe-stacks --stack-name coa-dev-auth --query "Stacks[0].Outputs" --output table

# confirm the sizes actually deployed
aws neptune describe-db-instances --query "DBInstances[?starts_with(DBInstanceIdentifier,'coa-dev')].[DBInstanceIdentifier,DBInstanceClass]" --output table
```

Run that last command every time. It is the only check that proves which Neptune class you are paying for.

Sign in at `WebAppUrl` with `initialAdminEmail` and the temporary password Cognito emails you. That user is in the `Admin` group, which maps to `platform-admin`.

---

## 7. Tear down

```bash
make destroy-dev                     # same variables and cdk.json as the deploy
# non-interactive: SCL_DESTROY_YES=1 make destroy-dev
```

`scripts/destroy.sh` runs seven steps, in this order: delete the AgentCore Runtimes → **wait for their ENIs to detach** → delete the VKG ECS services → delete the Cloud Map services → force-delete the DataZone domain → delete the connector stacks → `cdk destroy --all`. Steps 1–6 clear resources CloudFormation doesn't fully own; they are idempotent, so re-running after an early exit picks up where it left off.

- **It can stop at step 2 by design.** AgentCore releases its network interfaces outside CloudFormation. If they haven't detached within `SCL_ENI_WAIT_MAX_SECONDS` (default 600), the script stops there rather than walking into a `DependencyViolation` on `DeleteSecurityGroup`. That is the expected outcome, not a hang — wait and rerun `make destroy-dev`. In practice this can take hours.
- **`DependencyViolation` on the `mcp` or `serve` stacks:** same cause. Wait, then rerun.
- **Not removed:** the `CDKToolkit` stack and `/coa/config`. Keep both if you'll redeploy. That saves the bootstrap and lets unchanged images be skipped.
- Teardown deletes Neptune and OpenSearch data permanently.

---

## 8. Quick troubleshooting

| Symptom | Fix |
| --- | --- |
| `permission denied ... docker.sock` | `newgrp docker`, or `sudo setfacl -m user:$(whoami):rw /var/run/docker.sock` (resets on reboot) |
| `make setup` fails with `EACCES` on `npm install -g pnpm` | Run `bash ./scripts/setup-dev.sh` instead of `sh` |
| Neptune deployed as `db.r8g.large` after you set `cdk.json` | Expected on v0.3.4 — `neptune_instance_class` isn't read. Apply the code edit in 4.2. |
| Synth: `aoss_min_ocu=<n> is invalid` / `cannot exceed aoss_max_ocu` | Out-of-range OCU value. Use 0, 2, 4, 8, 16, or a multiple of 16 (max ≥ 2 and ≥ min). See 4.4. |
| `No SMUS admin principal configured, and IAM confirmed that account … has no role named 'Admin'` | Export `SCL_SMUS_ADMIN_ARNS`. Raised by `scripts/check-smus-admin-principal.sh`, which `deploy.sh` runs before CDK. |
| `ExpiredToken` partway through | Refresh credentials, rerun `make deploy-dev` |
| Namespace stack: `Domain name already exists` (409) | `aws datazone delete-domain --identifier <id> --skip-deletion-check`, delete the failed stack, redeploy |
| Lambda reserved concurrency rejected | `export SCL_LAMBDA_RESERVED_CONCURRENCY=0` |

Sources, all at v0.3.4 (`c84a304`): `infra/lib/stacks/foundation/storage-stack.ts`, `infra/test/foundation/storage-stack.test.ts`, `infra/cdk.json`, `scripts/deploy.sh`, `scripts/destroy.sh`, `scripts/check-smus-admin-principal.sh`, plus repo `external-docs/content/deploying.md` and `getting-started.md`; [Amazon Neptune pricing](https://aws.amazon.com/neptune/pricing/).
