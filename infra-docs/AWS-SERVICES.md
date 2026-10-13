# AWS Services Required — Context Ontology Accelerator

Inventory of every AWS service this application needs, grouped by whether the
deploy creates it, calls it at runtime, or expects it to already exist.

**Provenance:** verified against the source rather than transcribed from the
docs — derived from the `aws-cdk-lib/aws-*` module imports under `infra/lib`
and `infra/bin`, and the boto3 clients instantiated under `packages/` and
`libs/`.

| | |
|---|---|
| Repo version | `0.3.4` |
| Commit | `c84a304` (mirror sync) |
| Reviewed | 2026-10-08 |
| Deploy stacks | 16 |

---

## 1. Provisioned by the deploy (CDK creates these)

### Compute

| Service | Used for |
|---|---|
| AWS Lambda | API handlers, workers, custom resources, ontology reload |
| Amazon ECS on Fargate | Long-running containers: source enrichment, document ingestion, Ontop VKG (one service per namespace) |

### Data stores

| Service | Used for |
|---|---|
| Amazon Neptune | Knowledge-graph store (RDF/SPARQL) — one `db.r8g.large` instance |
| Amazon OpenSearch Serverless | Vector search for retrieval and grounding (NEXTGEN collection) |
| Amazon DynamoDB | Job/proposal state, roles, grants, Cedar policies, sessions |
| Amazon S3 | Ontology artifacts, staged documents, web assets, access logs |

### AI / ML

| Service | Used for |
|---|---|
| Amazon Bedrock | All LLM and embedding inference |
| Bedrock Guardrails | Content and PII filtering on guarded calls |
| Bedrock AgentCore Runtime | Hosts the Serve (query) and MCP server runtimes |

### Networking & edge

| Service | Used for |
|---|---|
| Amazon VPC (EC2) | Private networking — all Lambdas and tasks are VPC-bound |
| Amazon CloudFront | Web app CDN and TLS termination |
| AWS WAF (WAFv2) | Per-IP rate limiting at the edge and the API stage |
| AWS Cloud Map (Service Discovery) | Per-namespace VKG service discovery |
| Elastic Load Balancing v2 | Referenced by the monitoring construct |
| Amazon Route 53 | DNS records — **optional**, custom domains only |
| AWS Certificate Manager | TLS certificates — **optional**, custom domains only |

### Identity & access

| Service | Used for |
|---|---|
| Amazon Cognito | Default identity provider (skipped entirely in OIDC mode) |
| AWS IAM | Task and function roles, least-privilege grants |

### Catalog & governance

| Service | Used for |
|---|---|
| Amazon DataZone / SageMaker Unified Studio | Namespace domain, projects, data-asset catalog |
| Amazon Athena | Workgroup and per-source data catalogs |

### API & orchestration

| Service | Used for |
|---|---|
| Amazon API Gateway | REST API surface, authorizer, per-route throttles |
| AWS Step Functions | Source-scan orchestration, namespace-deletion pipeline |
| Amazon EventBridge | Scan/ingest event routing, parameter-integrity audit rules |
| Amazon EventBridge Scheduler | Scheduled source operations |
| Amazon SQS | Review queue, bulk-review worker continuations, async deletes |

### Operations

| Service | Used for |
|---|---|
| Amazon CloudWatch + CloudWatch Logs | Metrics, log groups, dashboards, alarms |
| AWS Systems Manager (Parameter Store) | ~107 cross-stack parameters and deploy config — every stack writes here |
| Amazon ECR | Container images for Fargate tasks and AgentCore |

---

## 2. Called at runtime but *not* provisioned

Invoked by application code via the AWS SDK. These either already exist in the
account, are created per-source when data is onboarded, or must be set up
separately.

> **This is the group most often missed** when scoping IAM permissions or
> checking regional availability. A gap here fails at feature-use time, not at
> deploy time.

| Service | Used for |
|---|---|
| AWS STS | Role assumption and caller identity — the second most-used client in the codebase |
| Amazon Athena | SQL over mapped data sources, table sampling, namespace cleanup |
| AWS Glue | Data Catalog reads, JDBC connection provisioning for scans |
| AWS Lake Formation | Permission grants when provisioning Glue federated catalogs |
| AWS Secrets Manager | Data-source credentials (JDBC, Databricks tokens) |
| Amazon Neptune Data API | Direct graph queries against the cluster |
| Amazon Redshift Data API | Only for Redshift-backed sources |
| Amazon Textract | OCR — only for scanned/PDF document ingestion |

---

## 3. Prerequisites the deploy does *not* create

| Prerequisite | Why it matters |
|---|---|
| **Bedrock model access** | Must be requested in the console for Claude Sonnet 4.6, Claude Haiku 4.5, Claude Sonnet 5, and Cohere Embed v4. The deploy cannot grant this, and `cdk synth` cannot detect whether you have it. |
| **CloudTrail trail logging management events** | Required for the Databricks connector parameter-integrity alarm to observe anything. The deploy creates no trail by design — that belongs to the account baseline. Most enterprise accounts already have one; without it the alarm is **blind**, not quiet. |
| **Amazon ECR Public** | Build-time base-image pulls. `us-east-1` only, regardless of deploy region. Handled automatically by the deploy's preflight checks. |

---

## 4. Services that constrain region selection

Most services above are available in effectively all commercial regions. These
six are the narrow ones — verify **all** of them before choosing a region other
than `us-east-1`:

- **Amazon Bedrock AgentCore Runtime** (need *Runtime* specifically, not just any AgentCore feature)
- **Amazon DataZone** / SageMaker Unified Studio
- **Amazon Neptune**
- **Amazon OpenSearch Serverless** (available in fewer regions than managed OpenSearch)
- **Amazon Bedrock** + Guardrails
- **Amazon Athena** (including federated query)

AgentCore Runtime and DataZone are the tightest constraints in practice.

**Additional constraints:**

- `us-east-1` is the default and the only region AWS has deployed and validated
  this in.
- `us-east-1` is **always involved** even for deployments elsewhere: the
  CloudFront-scope WAF web ACL and the UI ACM certificate can only exist there,
  and ECR Public is `us-east-1`-only.
- Availability zones matter, not just regions — AgentCore Runtime supports only
  **3 of 6 AZs** in `us-east-1`, and the OpenSearch Serverless VPC endpoint
  covers a different subset. The CDK app resolves the intersection
  automatically.
- GovCloud and China partitions are **not supported**.
- Default Bedrock model IDs are US cross-region inference profiles (`us.…`) that
  **cannot be invoked from a non-US region** — a non-US deployment must override
  every model ID.

---

## 5. Cost drivers

Four services account for nearly all idle spend (**~$930/month** per the
project's own estimate):

| Service | Why it costs when idle |
|---|---|
| Amazon Neptune | One always-on provisioned instance — not Serverless |
| Amazon OpenSearch Serverless | Bills a minimum OCU allocation whether queried or not; standby replicas are mandatory for NEXTGEN collections, roughly doubling consumption |
| NAT Gateway | One, always on |
| ECS Fargate | One VKG service **per namespace** — scales with namespace count |

Lambda, API Gateway, DynamoDB, S3, and Bedrock are usage-based and near zero
when idle.

> **For a PoC:** tear down between evaluation sessions with `make destroy-dev`.
> Budget extra time for teardown — AgentCore releases its VPC network interfaces
> outside CloudFormation and can hold them for hours to days.

---

## 6. Service quotas worth checking first

| Service | Quota (code) | AWS default | Notes |
|---|---|---|---|
| ECS / Fargate | On-Demand vCPU resource count (`L-3032A538`) | **6** | **Tightest fit.** Tasks are 2 vCPU / 8 GB and 4 vCPU / 16 GB, and VKG runs one service per namespace. Not covered by preflight. |
| Lambda | Concurrent executions (`L-B99A9384`) | 1,000 (10 on some new accounts) | Preflight-checked. On a reduced-quota account, deploy with `SCL_LAMBDA_RESERVED_CONCURRENCY=0`. |
| VPC | VPCs per Region (`L-F678F1CE`) | 5 | Preflight-checked. Deploy creates one unless `SCL_VPC_ID` is set. |
| OpenSearch Serverless | Indexing / search max OCU (`L-50FA809B`, `L-4E98D4EB`) | 10 each | Standby replicas roughly double consumption. |
| Amazon Bedrock | Per-model TPM / RPM, per inference profile | Varies by model | Induction throughput is usually bounded by model tokens-per-minute, not compute. |
| Amazon Athena | Active DML queries (`L-FC5F6546`) | 200 | Concurrent serve-time queries share this pool. |
| Amazon Textract | `DetectDocumentText` TPS (`L-75788A8B`) | 25 | Only relevant when ingesting scanned documents at volume. |

---

## Notes on the official documentation

This inventory differs slightly from Appendix A of
`external-docs/content/deploying.md`, which that document anticipates — it
directs readers to verify against `infra/lib/stacks/` since stacks get
refactored. Differences found:

- Appendix A lists **Athena** as runtime-only, but `sources-stack.ts` imports
  `aws-cdk-lib/aws-athena` and does provision Athena resources. It belongs in
  both sections.
- **EventBridge Scheduler** (`aws-cdk-lib/aws-scheduler`, `sources-stack.ts`) is
  not listed.
- **Elastic Load Balancing v2** (`constructs/monitoring.ts`) is not listed.
- **Neptune Data API** (`neptunedata` boto3 client) is not listed among the
  runtime-only services.
