# COA on AWS: Ubuntu laptop deployment runbook

Oct 9, 2026 · @Mazin Shaikh

## Before you start

This runbook deploys the Context Ontology Accelerator (COA) into one AWS account in **us-east-1**, environment **dev**, with the default **Cognito** sign-in, from an Ubuntu 22.04 or 24.04 laptop. Run every command in a normal user shell unless it starts with `sudo`.

Written against repo **v0.3.4** (mirror-sync commit `c84a304`) — the version this team is pinned to. `main` tracks ongoing development; stay on the tag unless you are deliberately upgrading, and re-check the storage-sizing steps in `COA-Redeployment-Guide.md` if you do.

| What | Expect |
| --- | --- |
| Your time | About half a day for a first deploy |
| CloudFormation time | About 1.5 h for the 16 stacks, plus the Databricks connector |
| Idle cost | About $930/month (Neptune, OpenSearch Serverless, NAT Gateway, Fargate) until you tear it down |
| Disk | Leave 30 GB+ free for toolchains, Docker images and build output |
| Permissions | AdministratorAccess (or equivalent) on the target account for the first deploy |

Keep the laptop plugged in and set to not sleep during the deploy, and run it inside `tmux` so a dropped terminal doesn't kill it.

## Part 1: Install dependencies on Ubuntu

You need AWS CLI v2, Docker with ARM64 emulation, Node.js 22 + pnpm 10, Python 3.12 + uv, Java 17 and Maven. Gradle comes with the repo (`models/gradlew`), so you don't install it.

**1.1 Base packages**

```bash
sudo apt-get update
sudo apt-get install -y build-essential git make curl unzip zip jq tmux \
  ca-certificates gnupg python3-pip python3-venv
```

**1.2 Docker Engine with buildx**

```bash
sudo install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg | sudo gpg --dearmor -o /etc/apt/keyrings/docker.gpg
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] \
  https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo $VERSION_CODENAME) stable" | \
  sudo tee /etc/apt/sources.list.d/docker.list > /dev/null
sudo apt-get update
sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin
sudo usermod -aG docker $USER
newgrp docker          # or log out and back in
docker run --rm hello-world
```

**1.3 ARM64 emulation (x86\_64 laptops only)**

The Serve, MCP and VKG images build for `linux/arm64`. Check `uname -m`: if it prints `x86_64`, register QEMU, or the build fails with `exec format error`.

```bash
sudo apt-get install -y qemu-user-static binfmt-support
# alternative: docker run --privileged --rm tonistiigi/binfmt --install arm64
docker buildx inspect --bootstrap | grep -i platforms   # should list linux/arm64
docker run --rm --platform linux/arm64 alpine uname -m  # must print aarch64
```

Not every image is arm64: the ontology-engine image and one sources task pin `linux/amd64` (`ontology-stack.ts` line 238, `packages/ontology-engine/Dockerfile`). On an x86_64 laptop those build natively and need nothing extra. If you ever build on an arm64 host (Apple Silicon), you need amd64 emulation instead — the pins exist because an unpinned build on an arm64 host produces an image the x86_64 task can't exec.

**1.4 AWS CLI v2**

```bash
cd /tmp
curl "https://awscli.amazonaws.com/awscli-exe-linux-$(uname -m).zip" -o awscliv2.zip
unzip -q awscliv2.zip && sudo ./aws/install --update
aws --version          # aws-cli/2.x
```

**1.5 Node.js 22, pnpm 10, AWS CDK CLI**

```bash
curl -fsSL https://deb.nodesource.com/setup_22.x | sudo -E bash -
sudo apt-get install -y nodejs
sudo npm install -g pnpm@10 aws-cdk
node -v; pnpm -v; cdk --version
```

Stay on pnpm 10: the repo pins pnpm 10.x, and pnpm 11 audits the lockfile differently.

**1.6 Python 3.12 via uv**

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
source $HOME/.local/bin/env      # or open a new terminal
uv python install 3.12
uv --version
```

Ubuntu 22.04 ships Python 3.10. That's fine, because uv reads the repo's `.python-version` and uses its own 3.12.

**1.7 Java 17 and Maven**

```bash
sudo apt-get install -y openjdk-17-jdk-headless maven
java -version          # 17.x
mvn -v
```

Maven builds the Databricks connector JAR, which `make deploy-dev` deploys after the platform stacks.

**1.8 Check everything**

```bash
aws --version; docker version --format '{{.Server.Version}}'
node -v; pnpm -v; cdk --version; uv --version
java -version; mvn -v | head -1; jq --version; pip3 --version
```

## Part 2: Prepare the AWS account

Most first-deploy failures start here, so do all five checks before you build anything.

**2.1 Credentials and region**

```bash
aws configure sso                  # or: aws configure (access keys)
export AWS_PROFILE=<your-profile>
export AWS_DEFAULT_REGION=us-east-1   # used by the CLI and preflight
export CDK_DEFAULT_REGION=us-east-1   # used by CDK; set both
aws sts get-caller-identity
```

Put the three `export` lines in `~/.bashrc` so every new terminal gets them. If you use SSO, make sure the session lasts at least 2–3 hours, or run `aws sso login` just before you deploy.

**2.2 Service quotas**

| Quota | Code | Default | What to do |
| --- | --- | --- | --- |
| Fargate On-Demand vCPU | L-3032A538 | 6 | Request an increase now. Preflight doesn't check it. |
| Lambda concurrent executions | L-B99A9384 | 1,000 (10 on new accounts) | If it's 10, deploy with `SCL_LAMBDA_RESERVED_CONCURRENCY=0` |
| VPCs per Region | L-F678F1CE | 5 | Need one free, or reuse a VPC with `SCL_VPC_ID` |
| OpenSearch Serverless OCU | L-50FA809B / L-4E98D4EB | 10 each | Usually fine for dev |

```bash
aws ec2 describe-vpcs --query 'length(Vpcs)'
aws lambda get-account-settings \
  --query 'AccountLimit.[ConcurrentExecutions,UnreservedConcurrentExecutions]'
aws service-quotas get-service-quota --service-code fargate --quota-code L-3032A538 \
  --query 'Quota.Value'
```

**2.3 Bedrock model access**

In the console, open **Amazon Bedrock → Model access** in us-east-1 and enable all four: Claude Sonnet 5, Claude Sonnet 4.6, Claude Haiku 4.5 and Cohere Embed v4.

On v0.3.4 the deploy's own defaults are three of those — `us.anthropic.claude-sonnet-5` (query and ontology induction), `us.anthropic.claude-haiku-4-5-20251001-v1:0` (chat) and `us.cohere.embed-v4:0` (embeddings), listed at `scripts/preflight-deploy.sh` lines 269–272. Sonnet 4.6 is the in-code fallback the ontology-engine inducer uses when no `llm_model_id` is passed, so enable it too. Preflight (check 9) verifies these IDs are invocable from the deploy region, but it cannot verify your account has been *granted* access — you'd find that out at the first query.

```bash
aws bedrock list-inference-profiles \
  --query "inferenceProfileSummaries[?contains(inferenceProfileId,'claude') || contains(inferenceProfileId,'cohere')].inferenceProfileId"
```

Also check that no SCP in your organization blocks Bedrock or Bedrock AgentCore.

**2.4 SageMaker Unified Studio admin principal**

The namespace stack's DataZone login role trusts an IAM role named `Admin` by default, and most accounts don't have one. Check it:

```bash
aws iam get-role --role-name Admin --query Role.Arn
```

If it doesn't exist (typical with IAM Identity Center), point the deploy at the role you sign in with:

```bash
aws iam list-roles --query "Roles[?starts_with(RoleName,'AWSReservedSSO_AdministratorAccess')].Arn" --output text
export SCL_SMUS_ADMIN_ARNS=<arn printed above>
```

Without this, preflight stops with `no IAM role named 'Admin'`.

**2.5 CloudTrail (optional, recommended)**

The Databricks parameter-integrity alarm needs a CloudTrail trail that logs management events. Check with `aws cloudtrail describe-trails`. Most organization accounts already have one.

## Part 3: Get the code and build

Clone the pinned tag, v0.3.4 — the same version as your Windows copy. Don't use `main`, which tracks ongoing development.

```bash
mkdir -p ~/work && cd ~/work
git clone --branch v0.3.4 https://github.com/aws/context-ontology-accelerator.git
cd context-ontology-accelerator

make setup        # Smithy codegen (gradlew), uv sync, pnpm install, pre-commit hooks
make test         # optional sanity check
```

The first `make setup` takes a while because it downloads the Gradle wrapper, openapi-generator and all Python and Node packages. A warning that Gradle isn't installed is harmless, since codegen uses the bundled `gradlew`. If `make setup` stops on pnpm, check that `pnpm -v` prints 10.x.

## Part 4: Configure and deploy

**4.1 Write the deploy config to SSM**

Set the first admin's email before the first deploy. Otherwise it's created as a placeholder (`nobody@amazon.com`), and the temporary password goes nowhere.

```bash
aws ssm put-parameter --name /coa/config --type String --overwrite \
  --value '{"initialAdminEmail":"<you@yourcompany.com>"}'
```

`/coa` is the default resource prefix. If you set `SCL_PREFIX`, the path becomes `/<prefix>/config`, and you must use the same prefix for every later deploy and destroy.

**4.2 Bootstrap CDK (once per account and region)**

```bash
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
cd infra && npx cdk bootstrap aws://$ACCOUNT_ID/us-east-1 && cd ..
```

**4.3 Deploy**

```bash
tmux new -s coa

# only if they apply to you (see Part 2):
# export SCL_SMUS_ADMIN_ARNS=arn:aws:iam::<acct>:role/...
# export SCL_LAMBDA_RESERVED_CONCURRENCY=0
# export SCL_VPC_ID=vpc-0abc123

make deploy-dev 2>&1 | tee deploy.log
```

`make deploy-dev` runs preflight first — ten checks on v0.3.4 (`scripts/preflight-deploy.sh`): toolchain versions, Python pip, container engine, Smithy-generated OpenAPI specs, ECR Public auth, the VPC limit, Lambda reserved-concurrency headroom, CDK bootstrap in every region the deploy touches, Bedrock model IDs, and a stale `cdk.context.json`. Then it builds, synthesizes and deploys the 16 `coa-dev-*` stacks, and deploys the Databricks connector last. Expect about 1.5 hours. If the terminal drops, reattach with `tmux attach -t coa`.

Before the first deploy, `deploy.sh` also runs `scripts/check-smus-admin-principal.sh`, which is what fails if no `Admin` role and no `SCL_SMUS_ADMIN_ARNS` exist (step 2.4).

At the end, the connector step prints an execution-role ARN. Keep it, because Databricks source owners need it in their IAM trust policy. It's also stored at `/coa/dev/connectors/databricks/deployment/role-arn` in SSM.

## Part 5: After the deploy

**5.1 Find the URLs and IDs**

```bash
aws cloudformation describe-stacks --stack-name coa-dev-web \
  --query "Stacks[0].Outputs[?OutputKey=='WebAppUrl'].OutputValue" --output text
aws cloudformation describe-stacks --stack-name coa-dev-auth \
  --query "Stacks[0].Outputs" --output table    # UserPoolId, client IDs, hosted UI
```

**5.2 Sign in**

1. Open the `WebAppUrl` in a browser.
2. Sign in with your `initialAdminEmail` and the temporary password Cognito emailed you, then set a new password.
3. You're in the Cognito `Admin` group, which maps to `platform-admin` automatically.

**5.3 Add teammates (optional)**

```bash
aws cognito-idp admin-create-user --user-pool-id <UserPoolId> \
  --username alice@company.com --user-attributes Name=email,Value=alice@company.com
aws cognito-idp admin-add-user-to-group --user-pool-id <UserPoolId> \
  --username alice@company.com --group-name Admin
```

Only `Admin` is pre-mapped to a role. Any other group needs an explicit grant on the web app's Permissions page.

**5.4 First namespace and data source**

1. In the web app, go to **Administration → Namespaces → Create namespace**. This creates a DataZone project and an Athena workgroup.
2. Tag the Glue database you want to onboard with the namespace ID, as in the command below.
3. Register the source (Glue, JDBC, Databricks or documents), then wait for the scan and AI enrichment.
4. Review and approve the enriched tables and columns.
5. Run **Ontology → Induction**, then query from the web app or an MCP client.

```bash
aws glue tag-resource \
  --resource-arn arn:aws:glue:us-east-1:$ACCOUNT_ID:database/<db-name> \
  --tags-to-add '{"coa:namespace":"<namespaceId>"}'
```

A Glue database must already be queryable in Athena when you register it.

## Troubleshooting and teardown

| Symptom | Cause | Fix |
| --- | --- | --- |
| `exec format error` while building images | No ARM64 emulation | Redo step 1.3, confirm `aarch64` |
| `permission denied` on `/var/run/docker.sock` | Your user isn't in the docker group yet | `newgrp docker`, or log out and back in |
| `No SMUS admin principal configured, and IAM confirmed that account … has no role named 'Admin'` | No SMUS admin fallback | Set `SCL_SMUS_ADMIN_ARNS` (step 2.4) |
| Lambda reserved concurrency rejected | Account quota is 10 | `SCL_LAMBDA_RESERVED_CONCURRENCY=0` |
| Namespace stack: `Domain name already exists` (409) | An earlier rollback left a DataZone domain behind | `aws datazone delete-domain --identifier <id> --skip-deletion-check`, delete the failed stack, redeploy |
| `ExpiredToken` midway | SSO session ran out | `aws sso login`, then rerun `make deploy-dev` (it resumes incrementally) |
| First query fails with model invalid / access denied | Bedrock model access missing | Enable all four models (step 2.3) |

For anything else, check the failing stack's events with `aws cloudformation describe-stack-events --stack-name coa-dev-<stack>`.

**Update later:** regenerate before you deploy, because `deploy.sh` doesn't do it for you — it runs preflight and `nx run-many -t build`, and `smithy-generated/` is not committed:

```bash
git fetch --tags && git checkout <new-tag>
make generate && pnpm install       # stale codegen is NOT refreshed by make deploy-dev
make deploy-dev
```

Only changed stacks redeploy. Preflight check 4 fails if the generated specs are missing altogether, but it does not detect specs left over from the previous tag.

**Tear down** when you're done testing, to stop the idle cost:

```bash
make destroy-dev                      # same SCL_PREFIX and region as the deploy
# non-interactive: SCL_DESTROY_YES=1 make destroy-dev
```

Teardown deletes Neptune and OpenSearch data permanently. AgentCore network interfaces can hold the `mcp` and `serve` security groups for hours or even days, so `destroy.sh` stops at step 2 of 7 (its ENI wait, budget `SCL_ENI_WAIT_MAX_SECONDS`, default 600 s) rather than walking into a `DependencyViolation`. That stop is expected. Wait and rerun `make destroy-dev` — all of its pre-`cdk destroy` steps are safe to repeat. See `COA-Redeployment-Guide.md` section 7 for the full step order.

Sources, all at v0.3.4 (`c84a304`): the repo's `external-docs/content/deploying.md`, `authentication-setup.md`, `getting-started.md` and `sources.md`; `scripts/preflight-deploy.sh`, `scripts/deploy.sh`, `scripts/destroy.sh`; and the sibling `infra-docs/AWS-SERVICES.md`.
