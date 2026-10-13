# COA Redeployment Guide (Ubuntu, us-east-1, dev)

How to redeploy the Context Ontology Accelerator into an AWS account it has run in before, and how to set the Neptune and OpenSearch Serverless sizes for a low-cost dev environment.

Written against repo **v0.3.5** (commit `052b5a2`). Line numbers below refer to that version.

---

## 1. What you can skip on a redeploy

| Step from the first deploy | Redo it? |
| --- | --- |
| Install toolchain (AWS CLI, Docker, Node, pnpm, uv, Java, Maven) | No |
| Docker group / ARM64 emulation | No (re-run the `setfacl` workaround after a reboot if you used it) |
| Bedrock model access, service quotas | No |
| Clone the repo | No. Pull or check out the release you want instead (section 3). |
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
unset SCL_NEPTUNE_INSTANCE_CLASS              # let cdk.json decide (see 4.4)

aws sts get-caller-identity                   # right account?
docker ps                                     # Docker reachable without sudo?
```

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
git checkout v0.3.5                 # or the release you want; work on a branch if you change code
git checkout -b dev-small-storage

make generate                       # Smithy codegen
bash ./scripts/setup-dev.sh         # use bash: the script's #!/usr/bin/env sh breaks on Ubuntu's dash
```

If `pnpm -v` no longer prints 10.x afterwards, reinstall it with `sudo npm install -g pnpm@10`.

---

## 4. Change the Neptune and OpenSearch size

### 4.1 Where it lives in the code

Both are in **`infra/lib/stacks/foundation/storage-stack.ts`**. In v0.3.5 both already read their size from CDK context, so you **don't need to edit this file**.

Neptune instance, around lines 113–130:

```ts
const neptuneInstance = new neptune.CfnDBInstance(this, "NeptunePrimaryInstance", {
  dbInstanceIdentifier: this.prefixed("neptune-primary"),
  // Overridable via `--context neptune_instance_class=...` (local: `SCL_NEPTUNE_INSTANCE_CLASS`)
  dbInstanceClass:
    (this.node.tryGetContext("neptune_instance_class") as string) ??
    "db.r8g.large",                                   // default when nothing is set
  dbClusterIdentifier: neptuneCluster.dbClusterIdentifier!,
});
```

OpenSearch Serverless capacity, around lines 232–234:

```ts
const maxOcu = Number(this.node.tryGetContext("aoss_max_ocu") ?? 96);   // default max 96 OCU
// NEXTGEN valid min OCU values: 0, 2, 4, 8, 16, or multiples of 16.
const minOcu = Number(this.node.tryGetContext("aoss_min_ocu") ?? 2);    // default min 2 OCU
```

`scripts/deploy.sh` (line 45) also turns the `SCL_NEPTUNE_INSTANCE_CLASS` environment variable into `--context neptune_instance_class=...`. There's no matching variable for the OpenSearch settings.

### 4.2 Set the values in `infra/cdk.json` (recommended)

Values in `cdk.json` apply to every `cdk` command, so `make deploy-dev` and `make destroy-dev` both see them.

```json
{
  "app": "npx tsx bin/app.ts",
  "context": {
    "env": "dev",
    "//": "Emit templates without indentation. ... (leave this text as it is)",
    "@aws-cdk/core:suppressTemplateIndentation": true,
    "neptune_instance_class": "db.t4g.medium",
    "aoss_min_ocu": 0,
    "aoss_max_ocu": 4
  }
}
```

Put a comma after each entry except the last.

### 4.3 Choosing the values

| Setting | Default | Low-cost dev | Notes |
| --- | --- | --- | --- |
| `neptune_instance_class` | `db.r8g.large` | `db.t4g.medium` | Smallest Neptune class (burstable, 4 GB). Fine for small dev graphs. CPU use above the baseline is billed as burst credits. If it's rejected, try `db.t3.medium`. Heavy concurrent SPARQL can run out of memory even on `db.r8g.large`. |
| `aoss_min_ocu` | `2` | `0` | Valid: 0, 2, 4, 8, 16, or multiples of 16. With `0` the collection scales to zero when idle. The first query after idle takes about 10 s, and bulk ingestion can be throttled (HTTP 429), which fails the ingest loudly. Use `2` for ingest-heavy work. |
| `aoss_max_ocu` | `96` | `4` | Cost cap. Valid: 2, 4, 8, 16, or multiples of 16. Must be ≥ min. Raise to `8` if ingestion keeps getting throttled. |

You can't turn off OpenSearch standby replicas: AWS requires them for NEXTGEN collections.

### 4.4 Avoid the empty-value trap

A `--context` flag on the command line overrides `cdk.json`. If you pass `--context neptune_instance_class=$SCL_NEPTUNE_INSTANCE_CLASS` while the variable is unset, the value becomes **empty** and synth warns `DBInstanceClass: '' is not valid`.

- **Using `cdk.json`:** keep `SCL_NEPTUNE_INSTANCE_CLASS` unset and don't pass that flag.
- **Using the variable instead:** set it to a real value before running `make deploy-dev` (`export SCL_NEPTUNE_INSTANCE_CLASS=db.t4g.medium`). `deploy.sh` only adds the flag when the variable isn't empty.

### 4.5 Verify before deploying

`cdk synth <stack>` prints YAML, so check the JSON copy it writes to `cdk.out`:

```bash
pnpm --filter coa-infra exec cdk synth coa-dev-storage \
  --context smus_admin_principal_arns=$SCL_SMUS_ADMIN_ARNS > /dev/null 2>&1

grep -o '"DBInstanceClass":"[^"]*"\|"MinIndexingCapacityInOcu":[0-9]*\|"MaxIndexingCapacityInOcu":[0-9]*' \
  infra/cdk.out/coa-dev-storage.template.json
```

Expected:

```text
"DBInstanceClass":"db.t4g.medium"
"MinIndexingCapacityInOcu":0
"MaxIndexingCapacityInOcu":4
```

The warning `AOSS min OCU is 0: indexing capacity can scale to zero…` is expected with `aoss_min_ocu: 0`. The deprecated-API warnings are harmless.

`make test` may fail the storage-stack unit test that expects the default size. That doesn't block the deploy.

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

Sign in at `WebAppUrl` with `initialAdminEmail` and the temporary password Cognito emails you. That user is in the `Admin` group, which maps to `platform-admin`.

---

## 7. Tear down

```bash
make destroy-dev                     # same variables and cdk.json as the deploy
# non-interactive: SCL_DESTROY_YES=1 make destroy-dev
```

Order: cleanup API calls first (AgentCore runtimes, VKG ECS services, Cloud Map services, DataZone domain, connector stack), then `cdk destroy --all` in reverse dependency order.

- **`DependencyViolation` on the `mcp` or `serve` stacks:** AgentCore is still releasing its network interfaces. Wait a few hours and rerun `make destroy-dev`.
- **Not removed:** the `CDKToolkit` stack and `/coa/config`. Keep both if you'll redeploy. That saves the bootstrap and lets unchanged images be skipped.

---

## 8. Quick troubleshooting

| Symptom | Fix |
| --- | --- |
| `permission denied ... docker.sock` | `newgrp docker`, or `sudo setfacl -m user:$(whoami):rw /var/run/docker.sock` (resets on reboot) |
| `make setup` fails with `EACCES` on `npm install -g pnpm` | Run `bash ./scripts/setup-dev.sh` instead of `sh` |
| Synth: `DBInstanceClass: '' is not valid` | Empty context value. See 4.4. |
| Preflight: `no IAM role named 'Admin'` | Export `SCL_SMUS_ADMIN_ARNS` |
| `ExpiredToken` partway through | Refresh credentials, rerun `make deploy-dev` |
| Namespace stack: `Domain name already exists` (409) | `aws datazone delete-domain --identifier <id> --skip-deletion-check`, delete the failed stack, redeploy |
| Lambda reserved concurrency rejected | `export SCL_LAMBDA_RESERVED_CONCURRENCY=0` |

Sources: repo `external-docs/content/deploying.md`, `getting-started.md`, `infra/lib/stacks/foundation/storage-stack.ts`, `scripts/deploy.sh`, `scripts/destroy.sh` (v0.3.5); [Amazon Neptune pricing](https://aws.amazon.com/neptune/pricing/).
