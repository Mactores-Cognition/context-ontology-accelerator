# Remove CloudFront — serve the web UI from ECS Fargate behind an ALB

## Context

The console is served today by CloudFront + a private S3 bucket
(`infra/lib/constructs/public-ui-construct.ts`), with a CLOUDFRONT-scope WAF
WebACL provisioned by a dedicated us-east-1 stack
(`infra/lib/stacks/foundation/edge-waf-stack.ts`). The deploying organisation
does not permit CloudFront, so the UI is undeliverable as built.

This change removes CloudFront entirely — distribution, OAC S3 origin, the
response-headers policy, the UI and CloudFront log buckets, the edge WAF stack
and its cross-region SSM hand-off — and replaces it with an **nginx container on
ECS Fargate behind an internet-facing Application Load Balancer**, terminating
TLS with an operator-supplied ACM certificate in the deploy region. There is
**one** hosting mode afterwards, not a toggle.

Decisions already taken (do not re-open): internet-facing ALB in the existing
public subnets; operator-supplied cert + domain in the **deploy** region; SPA
baked into the image with `runtime-config.json` written at container start; WAF
becomes a REGIONAL WebACL on the ALB and `EdgeWafStack` is deleted.

### Two supported modes: HTTPS (cert supplied) and HTTP (dev only)

CloudFront supplied a free `*.cloudfront.net` TLS hostname; **an ALB has no
AWS-provided certificate**. Rather than make a cert mandatory, the web stack
supports two modes:

| | `ui_domain` + `ui_cert_arn` supplied | Neither supplied |
|---|---|---|
| Listener | HTTPS:443 + HTTP:80 → 443 redirect | HTTP:80 only |
| `siteUrl` | `https://<ui-domain>` | `http://<alb-dns-name>` |
| CSP | includes `upgrade-insecure-requests` | omits it |
| Cognito callbacks | patched to the UI domain | **not patched** (see below) |
| Intended for | any real environment | local/dev smoke-testing only |

**What works on the HTTP path:** the UI loads, SPA deep-link routing works,
`runtime-config.json` resolves, security headers are emitted, the ALB + target
group + WAF + ECS plumbing is all exercised end-to-end. This is enough to
validate the whole CloudFront replacement.

**What does not:** **Cognito sign-in at the ALB origin.** Cognito requires
callback URLs to be `https://`, with `http://localhost` the single exception, so
`http://<alb-dns>/authenticate/` cannot be registered at all — passing it to
`UpdateUserPoolClient` fails the deploy with `InvalidParameterException`. This is
an AWS constraint, not something the stack can work around. Two ways to get a
working auth flow without buying a domain:

1. **`pnpm dev` at `localhost:5173`** against the deployed API — already
   supported, and `web-stack.ts:290-292` already registers
   `http://localhost:5173/authenticate/` in dev.
2. **Any cert + hostname**, including a self-signed cert imported into ACM.
   Cognito only checks the `https` scheme, so a browser click-through on the
   untrusted-CA warning is enough to exercise the full flow.

`prod` is unaffected by any of this: `bin/app.ts:193-199` already throws without
a UI custom domain, so the HTTP path cannot reach production.

## Verified facts this plan depends on

Worth stating, because two natural assumptions about this repo are wrong:

- **There are no `{{resolve:ssm:...}}` dynamic references in this path.**
  `ssm.StringParameter.valueForStringParameter` renders as a CFN template
  `Parameter` of type `AWS::SSM::Parameter::Value<String>` whose `Default` is the
  path; consumers hold a plain `Ref`. The repo's own test asserts exactly this
  (`infra/test/foundation/web-stack.test.ts:332-343`). Feeding such values into
  an ECS container env var is already proven here —
  `ontology-stack.ts:265,267,292-296` does it for the OpenSearch endpoint and
  three table/role names.
- **`CDK_BUNDLING_STACKS="[]"` does not disable Docker image assets.**
  `aws:cdk:bundling-stacks` gates `AssetStaging` bundling (the Python Lambda
  bundling in `infra/lib/utils/python-bundling.ts`). `ContainerImage.fromAsset`
  → `DockerImageAsset` does no Docker work at synth; it hashes the context and
  emits asset metadata, and `docker build` runs at **asset-publish time during
  `cdk deploy`**. So: a missing `packages/web-app/dist` fails minutes into a real
  deploy with an opaque `COPY failed`, *after* preflight passed — and CI never
  catches it. Correspondingly, the stack tests need **no**
  `"aws:cdk:bundling-stacks": []` context.
- **`libs/ts-shared/src/types.ts:8` already specifies the contract**:
  *"**ECS mode**: passed as the `RUNTIME_CONFIG_JSON` environment variable."*
  Use that exact variable name.
- **`/ws` is gone from the frontend.** `use-playground-stream.ts` /
  `use-playground-chat-sse.ts` replaced the WebSocket hooks; there is no
  `new WebSocket` in `packages/web-app/src`. nginx needs no WebSocket proxy and
  no `/api` reverse proxy — it is pure static. The ALB described in
  `infra/README.md:287-295` **does not exist** (`elasticloadbalancingv2` appears
  nowhere under `infra/lib` except `monitoring.ts:16`); that section is stale.
- **Foundation→foundation deps are already normal** — `StorageStack` takes
  `network` (`bin/app.ts:235-238`), so WebStack gaining a VPC is unremarkable.

## What is reused rather than rewritten

- `buildContentSecurityPolicy()` — `public-ui-construct.ts:38-164`. Pure,
  token-aware, covered by 20+ assertions in
  `infra/test/constructs/public-ui-csp.test.ts`. Moves **byte-identical**.
- `readRepoVersion()` / `buildRuntimeConfig()` — `web-stack.ts:25-69`. Unchanged.
- `WafWebAcl` — already supports `scope: "REGIONAL"` (its doc comment at line 11
  names ALB). Note `EdgeWafStack` is its only production caller today
  (`api-stack.ts:664-767` hand-rolls its own WebACL for the
  `SizeRestrictions_QUERYSTRING` exemption), so using it REGIONAL here is what
  keeps the construct and its test alive.
- `AccessLogsBucket` — one instance for ALB access logs.
- `SclMonitoring.monitorAlb()` (`monitoring.ts:242`, written speculatively and
  **currently unused**) and `monitorFargateService()` (line 125).
- The four `AwsCustomResource`s in `web-stack.ts` (Cognito callbacks, API CORS
  4XX/5XX, API redeploy) — they key off `siteUrl`, still a deploy-time token.
- Build ordering — already correct. `infra/project.json` declares
  `implicitDependencies: ["coa-web-app"]` and `synth` → `dependsOn: ["build"]`,
  and `scripts/deploy.sh:124` runs `pnpm nx run-many -t build` first. No new
  build step.

## Implementation

Ten steps, each independently reviewable and leaving the repo synth-able.

### Step 1 — Extract the CSP (pure refactor, zero behaviour change)

- **New** `infra/lib/constructs/content-security-policy.ts` —
  `buildContentSecurityPolicy` moved verbatim from
  `public-ui-construct.ts:16-164`, retargeting its two doc references to
  `PublicUIConstructProps` (lines 32, 178).
- `public-ui-construct.ts` imports it temporarily; export from the constructs barrel.
- **Move** `infra/test/constructs/public-ui-csp.test.ts` →
  `content-security-policy.test.ts`, **import path only**. All assertions
  survive untouched — the lowest-risk way to prove the CSP was not weakened.
- ✅ `pnpm nx run infra:test` green with no assertion edits.

Then one behaviour change, added as a **fourth optional parameter** so the
existing 20+ assertions keep passing unmodified:
`buildContentSecurityPolicy(runtimeConfig, override?, cognitoDomainPrefix?, insecureTransport = false)`.
When `insecureTransport` is true, omit `upgrade-insecure-requests` (line 162) —
on an `http://` document that directive rewrites every asset request to
`https://` against a listener with no TLS, so the page loads nothing. Everything
else in the policy is unchanged; `'self'`-based directives are scheme-relative
and work identically over HTTP. Add one new test for the omission and one
asserting the directive is still present by default.

### Step 2 — The container (`packages/web-app/`, no CDK yet)

This is the step CI will **never** validate, so verify it by hand.

- **`Dockerfile`** — SPDX header; digest-pinned public-ECR base matching the
  house convention (`packages/vkg/Dockerfile:7`,
  `packages/context-manager/Dockerfile:6`):
  `FROM --platform=linux/arm64 public.ecr.aws/docker/library/nginx:<tag>@sha256:<digest>`
  with the Renovate comment block mirroring `packages/vkg/Dockerfile:2-6`.
  **Resolve the real digest at implementation time** (`docker buildx imagetools
  inspect`) — do not paste an invented one. No Docker Hub; that is a deliberate
  repo-wide constraint, which is why `nginxinc/nginx-unprivileged` is out and the
  non-root work is done by hand. ARM64 matches `vkg`, which already builds under
  buildx emulation on this Windows dev flow; fall back to AMD64 only if that
  proves painful.
  Body: `COPY nginx/ /etc/nginx/`, `COPY docker-entrypoint.sh`,
  `RUN chmod +x … && sed -i 's/\r$//' …` (CRLF guard — this repo is developed on
  Windows, see `packages/vkg/Dockerfile:44`),
  `COPY --chown=nginx dist/ /usr/share/nginx/html/` **and chown the webroot** so
  the non-root entrypoint can write `runtime-config.json` into it, `EXPOSE 8080`,
  in-image `HEALTHCHECK` on `/health`, `USER nginx`, `ENTRYPOINT`.
- **`nginx/nginx.conf`** — `pid /tmp/nginx.pid;` and `*_temp_path` under `/tmp`
  (non-root cannot write the defaults); `listen 8080` (non-root cannot bind
  <1024); gzip; `location = /health { access_log off; return 200 "ok\n"; }`
  (the ALB probes every 15s from 2+ nodes — without `access_log off` they
  dominate the log group); `no-store` on `/index.html` and
  `/runtime-config.json`; `immutable, max-age=31536000` on `/assets/` (Vite emits
  content-hashed filenames); `location / { try_files $uri /index.html; }` (SPA
  fallback, replacing CloudFront's 403/404 → `/index.html`).
- **`nginx/snippets/security-headers.conf`** — the transport-independent
  headers, all with `always`: `X-Content-Type-Options: nosniff`,
  `X-Frame-Options: DENY`,
  `Referrer-Policy: strict-origin-when-cross-origin`. Together with the
  entrypoint-generated snippet below this mirrors
  `public-ui-construct.ts:229-254` exactly. HSTS and the CSP are **not** here —
  both depend on whether the deploy terminates TLS, so the entrypoint writes
  them (see `docker-entrypoint.sh`).

  > **The single most likely way to ship a security regression here.** nginx
  > inherits `add_header` from a parent context **only if the current context
  > declares none of its own**. A `location` that adds `Cache-Control` silently
  > drops every inherited security header. So: keep every security header in the
  > two snippets (`security-headers.conf` + the generated
  > `transport-headers.conf`) and `include` **both** in **every** `location`
  > block that declares any `add_header` of its own. No CDK template assertion
  > can catch this — the curl matrix in the verification section is the only
  > guard.
- **`docker-entrypoint.sh`** — `#!/usr/bin/env bash`, SPDX, `set -euo pipefail`,
  required env via `${VAR:?msg}` (the `packages/vkg/entrypoint.sh:13-17` idiom).
  Writes `$RUNTIME_CONFIG_JSON` verbatim to
  `/usr/share/nginx/html/runtime-config.json`, then generates
  `/etc/nginx/snippets/transport-headers.conf` containing the CSP
  (`add_header Content-Security-Policy "$CSP_HEADER" always;`) and — only when
  `INSECURE_TRANSPORT` is unset — the HSTS header
  (`max-age=63072000; includeSubDomains; preload`, matching
  `public-ui-construct.ts:231-235`). Then `exec nginx -g 'daemon off;'`.
  `INSECURE_TRANSPORT` is set by the task definition on the no-cert path, so one
  image serves both modes and the header set always matches the listener.
  **Do not use `envsubst`.** The official image's
  `20-envsubst-on-templates.sh` rewrites `$VAR`/`${VAR}` in templates, and an
  nginx config is full of `$uri`/`$host`/`$request_uri` — it survives today only
  because those are not in the environment, and is one `export host=…` from
  breaking.
- **`.dockerignore`** — build context is `packages/web-app` itself: ignore `*`,
  un-ignore only `dist/`, `nginx/`, `docker-entrypoint.sh`. `DockerImageAsset`
  honours this for hashing (`IgnoreMode.DOCKER`), so without it the asset hash
  churns on every `pnpm install` and staging is slow.

`packages/web-app/package.json` already carries the repo VERSION (gated by
`tests/unit/test_sync_version.py`); no new manifest.

### Step 3 — New construct `infra/lib/constructs/web-alb-construct.ts`

Extends `SCLConstruct`. Takes **`vpc: ec2.IVpc`**, deliberately *not*
`network: NetworkStack` — the shared `ecsSecurityGroup` already has port 8080
open to the whole VPC CIDR (`vkg-stack.ts:216-220`), so handing the construct
the whole network object is an invitation to reuse it and expose nginx
VPC-wide. This matches `NamespaceStack`/`ServeStack`/`McpStack`/`DataLayerStack`
(`bin/app.ts:250,399,528,439`).

- **Public-subnet guard**, before creating the ALB: throw if
  `vpc.selectSubnets({subnetType: PUBLIC}).subnetIds.length < 2`, naming
  `-c vpc_id`. Synth-safe both ways — `Vpc.fromLookup` returns a dummy VPC with
  2 public subnets during credentialless synth, and the created path has 2 by
  construction (`network-stack.ts:106-117`). CDK's native error
  ("no 'Public' subnet groups in this VPC") is unreadable, and the 2-AZ
  requirement otherwise fails only at `CreateLoadBalancer`.
- **Security groups**: `albSecurityGroup` (443, plus 80 for the redirect, from
  `Peer.anyIpv4()` — the first internet-facing resource in this VPC) and
  `serviceSecurityGroup` (`allowAllOutbound: false`, egress 443 for ECR/logs,
  ingress 8080 **from `albSecurityGroup` only**).
- **ALB**: `internetFacing: true`, `vpcSubnets: { subnetType: PUBLIC }`,
  `dropInvalidHeaderFields: true`, `ipAddressType: DUAL_STACK` (keeps the
  existing AAAA alias record honest — an IPv4-only ALB with an AAAA record
  resolves to an empty answer and breaks IPv6-only clients),
  and **no S3 access logging** — see deviation 10 below; `logAccessLogs()`
  cannot work in an environment-agnostic stack.
- **Listeners**: cert present → HTTPS:443 with the cert **and
  `sslPolicy: elbv2.SslPolicy.RECOMMENDED_TLS13`** (CDK otherwise defaults to
  `ELBSecurityPolicy-2016-08`, which permits TLS 1.0/1.1 — a silent downgrade
  from CloudFront's `TLS_V1_2_2021` at `public-ui-construct.ts:303`, and the
  easiest thing in this change to miss), plus HTTP:80 → 443 permanent redirect.
  Cert absent → **HTTP:80 forwarding to the target group** (no redirect listener)
  plus `cdk.Annotations.of(this).addWarning(...)` stating that the UI is served
  over plain HTTP, that Cognito sign-in is unavailable at this origin, and naming
  `ui_domain` / `ui_cert_arn` as the fix. Deliberately **not** a throw: this is
  the supported dev path, and every existing `WebStack` test
  (`web-stack.test.ts:58,75,99,262,306,329,362,383`) plus CI's `cdk synth` runs
  without custom-domain context.
  Also emit HSTS only on the HTTPS path — a `Strict-Transport-Security` header on
  a plain-HTTP response is ignored by browsers, but emitting it would pin the
  host to HTTPS the moment a cert is later added mid-test, which is confusing.
  Thread the same `insecureTransport` flag into `buildContentSecurityPolicy` and
  into the container's `CSP_HEADER`.
- **Target group**: port 8080, HTTP, `targetType: ip`, health check `/health`
  (interval 15s, timeout 5s, healthyThresholdCount 2), and
  **`deregistrationDelay: Duration.seconds(10)`** — the 300s default adds five
  minutes to every deploy of a stateless static server.
- **Cluster / task / service**: dedicated `ecs.Cluster` `prefixed("web-cluster")`;
  dedicated `logs.LogGroup` `/ecs/${prefixed("web")}` (ONE_MONTH, DESTROY) as in
  `ontology-stack.ts:222-226`; 256 CPU / 512 MiB is ample for nginx;
  `FargateService` in `PRIVATE_WITH_EGRESS` with `assignPublicIp: false`,
  `circuitBreaker: { rollback: true }`, `minHealthyPercent: 100`,
  `maxHealthyPercent: 200`, **`healthCheckGracePeriod`** (required with a target
  group attached, or ECS kills tasks before registration completes),
  `desiredCount` 2 in prod / 1 in dev. **No `cloudMapOptions`** — nothing
  discovers the UI by DNS.
- **Image**: the repo-wide dual path (`vkg-stack.ts:137-162`,
  `ontology-stack.ts:229-255`) — `web_app_image_uri` + `ecr_repository_arn` +
  `ecr_repository_name` → `fromEcrRepository`, else
  `ContainerImage.fromAsset(Paths.webApp, { platform: LINUX_ARM64 })`. Add
  `webApp: fromRoot("packages/web-app")` to `infra/lib/paths.ts`; do **not**
  copy `vkg-stack.ts:159`'s relative `"../packages/vkg"`, which violates that
  file's own documented invariant.
- **Container env** (plain `environment:`, never `secrets:`):
  `RUNTIME_CONFIG_JSON` = `cdk.Stack.of(this).toJsonString(runtimeConfig)` and
  `CSP_HEADER` = `buildContentSecurityPolicy(...)`. `toJsonString` routes through
  `CloudFormationLang.toJSON`, placing the quotes and JSON-escaping the literal
  segments around each `Ref`. Two notes worth a code comment: it cannot escape a
  token's *deploy-time* value, so a `"` or `\` in an SSM value would yield
  malformed JSON (impossible for an issuer URL, client id, domain prefix, API URL,
  ARN or semver — and `s3deploy.Source.jsonData` had the identical property); and
  these vars are readable via `ecs:DescribeTaskDefinition`, so **no secrets may
  ever enter `RuntimeConfig`**. Sizes (~400 B and ~800 B) are nowhere near
  ECS's 64 KiB task-def quota.
- **WAF**: `WafWebAcl({ scope: "REGIONAL", namePrefix: "ui" })` +
  `CfnWebACLAssociation` on `alb.loadBalancerArn` (mirroring
  `api-stack.ts:764-767`), honouring a caller-supplied ARN instead.
- **Monitoring**: `new SclMonitoring(...).monitorAlb(alb, "ui").monitorFargateService(service)`.
  Also fix the stale "Serve ALB / AgentCore behind the ALB" doc comment at
  `monitoring.ts:238-241`.
- **Outputs**: `LoadBalancerDnsName` + `WebsiteURL`, replacing
  `DistributionDomainName`/`DistributionId`/`WebsiteURL`
  (`public-ui-construct.ts:348-361`). Safe — none carry an `exportName`.

### Step 4 — Rewire `WebStack`

The ALB and service go **inside `WebStack`**, not a new stack: `siteUrl` and
`RUNTIME_CONFIG_JSON` must live in the same stack as the task definition.
`siteUrl` (`web-stack.ts:247-249`) feeds four custom resources and, with no
custom domain, *is* `alb.loadBalancerDnsName`; a separate stack would mean a CFN
`Export` on it and an export lock that prevents ever replacing the ALB — exactly
what `web-stack.ts:76-79` and `bin/app.ts:459-463` document the team routing
around via SSM.

- Add `vpc: ec2.IVpc`; swap `PublicUIConstruct` → `WebAlbConstruct`.
- **Delete** the dead `enablePrivateEndpoints` prop and its throw (lines 91-92,
  192-196) — there is one mode now; `websiteContentPath` (87-88, 233); the
  `autoWebAclParam` prop (95-100) and the `ReadCloudFrontWebAcl` cross-region
  `AwsCustomResource` (198-230). Rewrite the "two deployment modes" class doc
  (113-134).
- `siteUrl` = `https://${uiDomainName}` when set, else `http://${alb.loadBalancerDnsName}`.
- **Gate `UpdateCognitoCallbacks` (279-326) on a HTTPS `siteUrl`.** Cognito
  rejects any non-`localhost` `http://` callback URL, so passing the ALB's HTTP
  name to `UpdateUserPoolClient` fails the deploy with
  `InvalidParameterException` — the one thing that would stop an HTTP dev deploy
  from completing. Behaviour by mode:
  - **cert supplied** → unchanged; patch `https://<ui-domain>/authenticate/`
    (plus the dev localhost entry).
  - **no cert, `dev`** → still create the custom resource, but with
    **only** `http://localhost:5173/authenticate/` in `CallbackURLs` and
    `http://localhost:5173/` in `LogoutURLs`. Cognito accepts localhost over
    HTTP, so the deploy succeeds and `pnpm dev` sign-in keeps working — which is
    the documented way to exercise auth without a domain.
  - **no cert, non-`dev`** → skip the custom resource entirely, leaving the auth
    stack's `DEFAULT_CALLBACK_URLS` (`constants.ts:51`) in place. Do not emit an
    empty `CallbackURLs`; Cognito rejects that when `AllowedOAuthFlows` is set.
    (`prod` never reaches here — `bin/app.ts:193-199` throws first.)
  Keep the `ExplicitAuthFlows` parity logic identical in every branch; it is a
  full replacement and the regression the existing tests guard.
- The API CORS patch (328-407) needs **no** gating — `http://<alb-dns>` is a
  perfectly valid `Access-Control-Allow-Origin` value.
- Route53 alias (256-269): keep the L1 `CfnRecordSet` A/AAAA pair; swap the
  hardcoded CloudFront zone `Z2FDTNDATAQYW2` for
  `alb.loadBalancerCanonicalHostedZoneId` and the dnsName for
  `alb.loadBalancerDnsName`.
- Delete `infra/lib/constructs/public-ui-construct.ts` and
  `infra/lib/stacks/foundation/edge-waf-stack.ts`; prune both barrels
  (`constructs/index.ts:17,20`, `foundation/index.ts:5`) and the dangling
  `{@link EdgeWafStack}` reference in `waf-web-acl.ts:13-16`.

### Step 5 — `infra/bin/app.ts`

- **Relax the custom-domain contract** (lines 151-174) from five all-or-nothing
  keys into two independent pairs plus an independent `hosted_zone_id`. The
  bundling existed only because CloudFront needed us-east-1 while API Gateway
  needed the deploy region; both certs now live in the deploy region and could
  even be one wildcard. The two surfaces are also independently optional — a UI
  cert is what upgrades the ALB to HTTPS, while the API is perfectly usable on
  its default `execute-api` endpoint — so forcing an API custom domain just to
  get UI TLS is a tax with no justification.
  - UI pair `ui_domain` + `ui_cert_arn` — both or neither; optional (omitting
    both selects the HTTP dev mode).
  - API pair `api_domain` + `api_cert_arn` — both or neither; optional.
  - `hosted_zone_id` — optional (DNS may be external, and there is no alias
    record to create at all in HTTP mode).
- **Per-field cert-region guards** (176-189): `uiCertificateArn` must now be in
  `deployRegion`, not us-east-1. Keep the fail-fast shape and "got region X"
  message style.
- **Fix `allowedOrigin`** (191-199) to key on `customDomain?.uiDomainName`
  rather than `customDomain` truthiness. After relaxation, an API-only config
  makes `customDomain` truthy with `uiDomainName === undefined`, yielding
  `"https://undefined"` and silently breaking StorageStack's S3 CORS and every
  service stack's CORS. Leave the dev-`"*"` / prod-throw logic otherwise intact —
  `"*"` in dev is what lets `pnpm dev` on `localhost:5173` call the deployed API,
  matching the localhost callback `web-stack.ts:290-292` already adds.
- **No throw for a missing UI domain.** A certless deploy is the supported HTTP
  dev mode; the construct's `addWarning` is the whole signal. `prod` is already
  protected by the existing `allowedOrigin` throw (193-199), so no new guard is
  needed — and adding one would defeat the dev path.
- **Rename `cloudfront_web_acl_id` → `ui_web_acl_id`** (209) and **throw** if the
  old key is set: its value is a CLOUDFRONT-scope ARN, which is invalid for a
  `CfnWebACLAssociation` and would surface as `WAFInvalidParameterException` at
  deploy. Note the rename also moves the WebACL name and CloudWatch metric from
  `coa-dev-cloudfront-waf-*` to `coa-dev-ui-waf-*`
  (`waf-web-acl.ts:51,70,95,109`), a string documented at `deploying.md:662-677`.
- **Delete** `cfWebAclParamName` (217), the `EdgeWafStack` block (446-456), the
  `autoWebAclParam` wiring (468-472), `web.addDependency(edgeWaf)` (485), and the
  import (29-37).
- **Replace the silent `fs.existsSync(webAppDist)` guard (480) with a
  precondition** that throws if `packages/web-app/dist/index.html` is missing,
  naming `pnpm nx run coa-web-app:build`. Check `index.html`, not the directory —
  a stale empty `dist/` from an aborted build passes a directory check and ships
  a blank page. Skip the check when the prebuilt-ECR image path is taken. This
  never fires in CI or `make deploy-dev` (the Nx `implicitDependencies` /
  `dependsOn` chain builds the SPA first) and never fires in unit tests (they
  construct `WebStack` directly), only for someone running `cdk synth` by hand.
- Pass `vpc: network.vpc` to `WebStack`.

### Step 6 — `infra/lib/types.ts`

`CustomDomainConfig` (63-89): make `apiDomainName`, `apiCertificateArn` and
`hostedZoneId` optional; drop the us-east-1 requirement from the doc block
(74-76). **No `ApiStack` change needed** — `api-stack.ts:501` and `:634` already
guard each field individually.

Also `libs/ts-shared/src/types.ts:7-8`: "CloudFront mode" is gone;
`RUNTIME_CONFIG_JSON` is the only mode.

### Step 7 — Tests

`infra/jest.config.js` enforces a **global 80% line threshold** over `lib/**` +
`bin/**`. This change deletes ~160 well-covered lines and adds ~250, so the new
construct test must be thorough or the suite fails on coverage, not assertions.

- **Delete** `infra/test/foundation/edge-waf-stack.test.ts`,
  `infra/test/constructs/public-ui-custom-domain.test.ts`, and the
  `describe("WebStack CloudFront WAF WebACL")` block
  (`web-stack.test.ts:124-176`) — both its tests assert
  `AWS::CloudFront::Distribution` or the us-east-1 SSM reader.
- **Rewrite** `cspOf()` (`web-stack.test.ts:286-295`) to read the `CSP_HEADER`
  entry of `AWS::ECS::TaskDefinition` → `ContainerDefinitions[0].Environment`
  instead of the `ResponseHeadersPolicy`. The four assertions below it
  (297-389) survive nearly verbatim, including the high-value `Parameters`
  defaults check at 332-343.
- **Rewrite** `describe("WebStack runtime-config.json artifact")` (244-281):
  there is no deploy asset any more. Assert the `RUNTIME_CONFIG_JSON`
  `Fn::Join` literals contain `"version":"<VERSION>"`, keeping the existing
  independent VERSION read (247-251) so nothing is hardcoded. Note in the
  comment that the "real artifact on disk" property is lost — the env var is the
  artifact now.
- The three Cognito-parity tests (53-121) and the `readRepoVersion` /
  `buildRuntimeConfig` pure tests (178-241) need **no change** beyond passing a
  `vpc` from a sibling `NetworkStack` in the same app (the
  `vkg-stack.test.ts:45-77` pattern).
- **New** `infra/test/constructs/web-alb-construct.test.ts`: internet-facing +
  public subnets; HTTPS listener carries the cert ARN **and `SslPolicy`**;
  HTTP→HTTPS redirect exists only with a cert; target-group port / health path /
  deregistration delay; service SG takes 8080 from the ALB SG only and the
  shared `ecsSecurityGroup` is untouched; REGIONAL `WebACLAssociation` on the ALB
  ARN; a provided `ui_web_acl_id` creates no `CfnWebACL`; the <2-public-subnet
  guard throws; ALB access-logging attributes set; `AWS::CloudFront::*` count
  is 0.
- **New — the HTTP dev mode**, since it is now a supported path rather than a
  degraded accident:
  - no cert → exactly one listener, `Port: 80`, `Protocol: HTTP`, action
    `forward` (not `redirect`), and no `Certificates`;
  - no cert → a warning annotation is attached (assert via
    `Annotations.fromStack(stack).hasWarning("*", Match.stringLikeRegexp(...))`);
  - no cert → the container's `CSP_HEADER` omits `upgrade-insecure-requests` and
    `INSECURE_TRANSPORT` is present in the task-definition environment; with a
    cert, the inverse of both;
  - **no cert + `dev` → the `UpdateCognitoCallbacks` custom resource contains
    only the localhost callback and no `http://` ALB URL**; no cert + a non-`dev`
    env → no `UpdateCognitoCallbacks` custom resource at all. This is the guard
    against reintroducing the `InvalidParameterException` deploy failure, so give
    it a comment saying so.
- `infra/test/constructs/waf-web-acl.test.ts:67` — flip the `CLOUDFRONT` scope
  case to `REGIONAL`.
- `infra/test/foundation/storage-stack.test.ts:172,179` — cosmetic sample origin
  string.

### Step 8 — Scripts

- `scripts/preflight-deploy.sh` §8 (226-249) — drop us-east-1 from
  `_bootstrap_regions` and delete the "us-east-1 is always required — the
  `*-edge-waf` stack deploys there" error. A single-region deploy now needs a
  single bootstrap. (No leftover-`edge-waf` warning: see the `destroy.sh` note
  below — greenfield means there is nothing to warn about.) The ECR-Public login
  at 152-155 **stays** and is now load-bearing for the nginx base image.
- `scripts/destroy.sh` — **no change.** The orphaned-`edge-waf` sweep this
  originally called for is migration-only machinery: it matters solely to an
  environment that was deployed by a *previous* version of this code and still
  has a `{prefix}-{env}-edge-waf` stack in us-east-1. This deployment is
  greenfield, so no such stack ever exists and `cdk destroy --all` is already
  complete. Documented in Step 9 instead, so a future upgrader knows the one
  command to run; add the sweep back (a tolerant, idempotent `delete-stack` +
  wait before `:579`, marked TRANSITIONAL in the `vkg-stack.ts:368-374` idiom)
  only if an environment turns up that needs it.
- `scripts/deploy.sh` — `SCL_UI_CERT_ARN` comment at 15-17 and 39-40:
  "(us-east-1)" → "(deploy region)", and replace the "all five or none" note
  with the two-pair contract.

### Step 9 — Documentation

- `external-docs/content/deploying.md` — the heaviest: "`us-east-1` is always
  involved" (250-252); the **"Deploying outside us-east-1 needs TWO
  bootstraps"** warning (270-280); the stack table rows for `*-edge-waf` and
  `*-web` plus "all 16 stacks" → 15 (333-336); `SCL_UI_CERT_ARN` us-east-1
  (412-421); the edge-WAF rate-limit row (631); `cloudfront_web_acl_id` and the
  `…-cloudfront-waf-…` metric names (662-677); the `WebAppUrl` output (743, which
  matches no current output name either); the service table (1263-1264); and
  1402-1404. Plus a line in the `vpc_id` section: an imported VPC must provide
  ECR api/dkr + S3 endpoints or working NAT egress from its private subnets, or
  the UI task never reaches steady state (`network-stack.ts:124` creates
  endpoints only on the new-VPC path).
- **New "UI hosting modes" section** in `deploying.md`, carrying the mode table
  from this plan's Context section verbatim:
  1. **HTTPS (any real environment)** — supply `ui_domain` + `ui_cert_arn`
     (`SCL_UI_DOMAIN` / `SCL_UI_CERT_ARN`) with the cert in the **deploy
     region**. The ALB has no AWS-provided certificate, unlike CloudFront, so
     this is the one new operator input the removal introduces.
  2. **HTTP (dev smoke-testing only)** — omit both and the ALB serves the
     console on `http://<alb-dns>`. State plainly what works (UI, routing,
     runtime-config, headers, the full ALB/ECS/WAF path) and what does not
     (**Cognito sign-in at the ALB origin — Cognito requires `https` callback
     URLs except for `localhost`**), and point at `pnpm dev` on
     `localhost:5173` or an ACM-imported self-signed cert as the two ways to
     exercise auth without a domain.
  3. **Set your external IdP's redirect URI to the UI domain** —
     `https://<ui-domain>/authenticate/` and `https://<ui-domain>/`. Cognito's
     own callbacks are patched automatically by the full-replacement custom
     resource; an Okta/Azure AD app must be configured by hand.
- **Separate "Upgrading from a CloudFront deployment" subsection**, clearly
  marked as not applying to a fresh install. Keeps the greenfield path short
  while leaving the knowledge recorded:
  1. `aws cloudformation delete-stack --stack-name coa-<env>-edge-waf --region us-east-1`
     — once `EdgeWafStack` leaves the app, `cdk destroy --all` can no longer see
     it (`scripts/destroy.sh:585-589` enumerates only the deploy region).
  2. The `-web` stack update **deletes a CloudFront distribution**, which CFN
     does as disable-then-delete: expect **15-45 minutes** on that stack and a
     window where the old URL 403s.
  3. In **prod**, `AccessLogsBucket` is `RETAIN` with no auto-delete
     (`access-logs-bucket.ts:70-73`), so `coa-prod-ui-logs-<acct>` and
     `coa-prod-cf-logs-<acct>` are orphaned and must be removed by hand. Dev
     buckets auto-delete.
- `external-docs/content/authentication-setup.md` — 171-173 (runtime-config
  "injected into S3"), 274, 295, and a rewrite of the "Get the CloudFront
  domain" Okta walkthrough at 387-401.
- `infra/README.md` — 270, 291, 336, and **delete the stale "private ALB +
  CloudFront VPC Origin `/ws`" section (287-295)** outright; it describes
  nothing that exists. Add the real ALB to the stack table and
  `monitorAlb` to the monitoring inventory at 344-345.
- `packages/web-app/README.md` — 102 (`E2E_BASE_URL` example) and the Deployment
  section (328-333): document the container, not "serve behind CloudFront".
- `packages/context-manager/README.md` — 23, 27, 301.
- Code comments: `packages/web-app/src/api-hooks/use-example-questions.ts:24`,
  `use-metrics.ts:260`, `access-logs-bucket.ts:24`. And re-check whether
  `packages/web-app/tests/e2e/specs/auth/logout.spec.ts:12-15` can be
  un-skipped — its stated cause is the CloudFront origin.

Every added/modified `.ts`/`.sh`/`.smithy` file needs the
`SPDX-License-Identifier: Apache-2.0` header
(`scripts/ci/check-license-headers.sh`).

### Step 10 — Known trade-offs to state in the PR

- **Cost:** an ALB plus a continuously-running Fargate task replaces a
  pay-per-request CDN — roughly $25-35/month idle, small against the ~$930/month
  Neptune + OpenSearch floor the repo already documents.
- **No CDN:** assets served from one region. Fine for an internal console.
- **Caching is deliberately better, not worse.** CloudFront applied a *global*
  `Cache-Control: no-store, max-age=0` custom header
  (`public-ui-construct.ts:255-263`), so the UI is effectively uncached today.
  The nginx split (immutable year-long cache for `/assets/*`, `no-store` for
  `index.html` and `runtime-config.json`) is a real win — flag it so nobody
  "fixes" it back.
- **`/questions/{ns}.json` example prompts move from hot-swap to deploy-time.**
  `use-example-questions.ts:38` fetches per-namespace Playground sample prompts
  from the UI origin. Today an operator can `aws s3 cp` one into the UI assets
  bucket and it is live immediately; with an immutable image there is no bucket
  to copy into.
  **The capability is retained**, because Vite already copies
  `packages/web-app/public/` into `dist/` and `dist/` is baked into the image —
  so committing `public/questions/<ns>.json` works with no further change. Only
  the *change-without-redeploy* path is lost, and it is replaced by the ordinary
  deploy cycle.
  No functional regression either way: nothing in the repo creates these files
  (verified — no `questions/*.json` exists anywhere), the hook tolerates a 404 at
  line 41, and the Playground simply renders no chips, exactly as it does today.
  **Runtime drop-ins are retained** (decided in review): the web stack keeps a
  small private `ui-content` bucket and the entrypoint `aws s3 sync`s
  `questions/` into the webroot at container start, following
  `packages/vkg/entrypoint.sh:30-57` (retries, then degraded mode — a missing or
  unreadable bucket must never crash-loop the UI). An operator drops a file in
  the bucket and forces a new ECS deployment; no image rebuild. Updates are
  picked up on task start rather than instantly, which is the one difference from
  CloudFront. Baked-in `public/questions/*.json` still work and the synced copy
  wins.

## Verification

### Container (Step 2 — the only place nginx behaviour is ever checked)

```bash
pnpm nx run coa-web-app:build
docker build -t coa-web-app:local packages/web-app
docker run --rm -p 8080:8080 \
  -e RUNTIME_CONFIG_JSON='{"region":"us-west-2","stage":"dev","authority":"https://example.com","clientId":"abc"}' \
  -e CSP_HEADER="default-src 'none'; script-src 'self'" coa-web-app:local

# THE CURL MATRIX — for each of / , /index.html , /runtime-config.json ,
# /assets/<hashed>.js , /namespaces/foo : assert all five security headers.
# This is the only guard against the add_header inheritance trap.
curl -sSI localhost:8080/<path>

curl -s  localhost:8080/runtime-config.json      # exact JSON back, Cache-Control: no-store
curl -s  localhost:8080/namespaces/foo | head -1 # index.html (SPA fallback)
curl -sI localhost:8080/health                   # 200
docker run --rm coa-web-app:local id -u          # non-zero (non-root)
```

### Repo

```bash
pnpm nx run infra:test        # jest; watch the 80% global coverage gate
make lint                     # version-check + tsc + ruff + mypy + prettier
uv run pytest tests/unit -q   # version sync, mirror paths, doc accuracy
cd packages/web-app && pnpm test

pnpm nx run infra:synth       # must pass with NO custom-domain context (CI parity)

# HTTP dev mode: synths, warns, single port-80 forward listener, no cert
CDK_DEFAULT_ACCOUNT=123456789012 pnpm --filter coa-infra exec cdk synth coa-dev-web \
  | grep -E 'Port|Protocol|Certificates|INSECURE_TRANSPORT'
# HTTPS mode: cert + SslPolicy + the redirect listener appear
CDK_DEFAULT_ACCOUNT=123456789012 pnpm --filter coa-infra exec cdk synth coa-dev-web \
  -c ui_domain=app.example.com -c ui_cert_arn=arn:aws:acm:us-west-2:123456789012:certificate/x \
  | grep -E 'SslPolicy|internet-facing|redirect'

# nothing CloudFront survives:
grep -ri cloudfront infra/ packages/ libs/ scripts/ external-docs/ \
  --include='*.ts' --include='*.sh' --include='*.md' | grep -v node_modules
```

### After a certless `make deploy-dev` (the HTTP dev path — do this first)

No context keys needed; `make deploy-dev` as-is. This validates the entire
CloudFront replacement except Cognito sign-in.

1. The synth prints the "served over plain HTTP" warning, and the deploy
   **completes** — in particular `UpdateCognitoCallbacks` does not fail. That
   custom resource is the one thing that breaks if the gating in Step 4 is wrong.
2. `http://<alb-dns>/` loads the console shell.
3. `curl -s http://<alb-dns>/runtime-config.json` returns resolved values.
4. Deep link: `http://<alb-dns>/namespaces` returns the app, not a 404.
5. `curl -sI http://<alb-dns>/` → `X-Frame-Options`, `X-Content-Type-Options`,
   `Referrer-Policy` and `Content-Security-Policy` present; **no**
   `Strict-Transport-Security`; the CSP has **no** `upgrade-insecure-requests`.
   Repeat across the curl matrix.
6. Browser devtools: assets all load (this is what `upgrade-insecure-requests`
   would have broken) and there are no CSP violations on the shell.
7. Sign-in via `pnpm dev` on `localhost:5173` against the deployed API still
   works — the only auth path available in this mode.

### After a real `make preflight && make deploy-dev` (with a cert)

1. `make preflight` no longer demands a us-east-1 bootstrap, and the deploy
   creates nothing there: no `coa-dev-edge-waf` stack, no CLOUDFRONT WebACL, no
   `/{prefix}/edge/cloudfront-web-acl-arn` SSM parameter.
2. ECS service reaches steady state and the target group shows **healthy** within
   ~1 min. If not, suspect the SG ingress on 8080 or the `/health` location.
3. **Re-run the curl matrix against `https://<ui-domain>`** for `/`,
   `/index.html`, `/runtime-config.json`, `/assets/<hashed>.js` and a deep SPA
   route. Nothing upstream catches a dropped header.
4. `curl https://<ui-domain>/runtime-config.json` returns valid JSON with
   resolved `authority`, `clientId`, `apiEndpoint`, `serveRuntimeArn` and
   `version` — i.e. the `Fn::Join` really resolved. Then sign in in a browser and
   confirm **zero CSP violations** in devtools; the hosted-UI `connect-src` /
   `frame-src` entries (`public-ui-construct.ts:98-101`) are what issue #130 was
   about, and the CSP has moved transport from a CloudFront policy to an nginx
   header.
5. `curl -I http://<ui-domain>/` → 301 to HTTPS.
6. `aws wafv2 get-web-acl-for-resource --resource-arn <alb-arn>` returns
   `coa-dev-ui-waf`. For request logs, read the nginx access log in CloudWatch:
   `aws logs tail /ecs/coa-dev-web --follow`.
7. Cognito app client `CallbackURLs` contains `https://<ui-domain>/authenticate/`
   plus `http://localhost:5173/authenticate/` in dev.
8. `/system-health` and one Playground query succeed (the API CORS patch landed
   with the new origin), and a presigned-S3 OSI import works (exercises the
   `connect-src` S3 origins).
9. Deep-link refresh (`https://<ui-domain>/namespaces/<id>` → F5) returns the app.
10. `E2E_BASE_URL=https://<ui-domain> pnpm --filter coa-web-app test:e2e`.
11. `make destroy-dev`, then confirm no stacks remain. Single region only — a
    greenfield deploy never creates anything in us-east-1.

---

## Implementation notes (deviations from the plan above)

Written during implementation. Where this section disagrees with the plan, this
section is what the code does.

1. **The ALB is IPv4-only, and only an `A` alias record is created.** The plan
   called for `DUAL_STACK` to keep the existing A+AAAA pair honest, but a
   dual-stack ALB requires an IPv6 CIDR on the VPC and `NetworkStack` allocates
   none (`subnetConfiguration` is IPv4 `cidrMask` only). Dropping the AAAA
   record was the correct half of that trade.
2. **The ALB→task security-group rules are left to CDK.** Registering the
   service with the target group makes CDK create the ALB-SG egress and
   service-SG ingress itself; adding them by hand as well produced two
   CloudFormation rules with different descriptions, which fails at deploy with
   `InvalidPermission.Duplicate`. Only the internet ingress (443/80) and the
   service's 443 egress are declared explicitly.
3. **The subnet guard reads `vpc.publicSubnets`, not `selectSubnets`.**
   `selectSubnets({subnetType: PUBLIC})` throws its own opaque
   "no 'Public' subnet groups" error first, which would have made the friendly
   message unreachable.
4. **`packages/web-app/.dockerignore` must un-ignore `Dockerfile`.** CDK stages
   the build context using these rules and then runs `docker build` in the
   staged copy, unlike a plain `docker build`, which always sends the Dockerfile
   regardless. An `*`-then-allowlist file without `!Dockerfile` breaks the asset
   build.
5. **The entrypoint is `sh`, not `bash`** — the alpine base has no bash. It
   generates `snippets/transport-headers.conf` (CSP always, HSTS only when
   `INSECURE_TRANSPORT` is unset) rather than two separate files.
6. **No synth-time throw for a missing UI domain, and no `allow_insecure_ui`
   escape hatch.** Plain HTTP is a supported dev mode, so the only signal is the
   construct's warning annotation; `prod` stays protected by the pre-existing
   `allowedOrigin` throw.
7. **`CustomDomainConfig` fields are all optional** and the five-key contract
   became two independent pairs plus an independent `hosted_zone_id`.
   `ApiStack` needed no change — it already guarded each field.
8. **The operator `questions/` drop-in was kept**, per review: the construct
   creates a private `{prefix}-{env}-ui-content-{account}-{region}` bucket, the
   image installs `aws-cli`, and the entrypoint syncs `questions/` into the web
   root at task start with three attempts and a non-fatal fallback. A new
   deployment is needed to pick up changes; no image rebuild.
   `contentBucket.grantRead(role, "questions/*")` already covers the
   `ListBucket` that `aws s3 sync` performs, so no extra IAM statement.
9. **`infra/README.md`'s Serve-stack ALB / `/ws` WebSocket section was deleted,
   not ported.** It documented a private ALB, target group and
   `AlbDnsName` output that do not exist in `serve-stack.ts`, and a `/ws` path
   the frontend no longer calls. The replacement describes the real
   direct-to-AgentCore SSE path.

### Verification status

**None of this has been compiled or tested.** This checkout has no
`node_modules`, no `smithy-generated/`, no Java/Gradle, no `uv`, and Python 3.9
rather than 3.12 — and the Docker daemon was not running — so `tsc`, `jest`,
`pytest` and `docker build` could all not be run. What *was* checked:
`scripts/ci/check-license-headers.sh` passes, `bash -n` is clean on every
modified shell script, and a repo-wide grep shows no unintended CloudFront
references remain.

Run `make setup`, then the commands in the Verification section above. Treat the
first `docker build` and the first `cdk synth` as the real review of this change;
the `apk add aws-cli` line and the nginx config are the two places most likely to
need a correction.
10. **The UI ALB has no S3 access logging.** The plan called for an
   `AccessLogsBucket` wired via `logAccessLogs()`, but that resolves the
   regional ELB log-delivery account at synth time and **every stack in this app
   is environment-agnostic** — nothing passes `env`, in `bin/app.ts` or
   `SCLStack` — so `stack.region` is an `AWS::Region` token and CDK throws
   `RegionRequiredEnableBvAccess`. This failed 27 tests and would have failed
   `cdk synth` identically.
   Dropped rather than fixed by pinning the stack to a concrete region, which
   would cost the app its portable templates and risk agnostic-to-concrete
   cross-stack reference errors against `NetworkStack`. Request logging is
   retained: nginx writes one access-log line per request to stdout and the
   awslogs driver ships it to `/ecs/{prefix}-{env}-web`. What is lost is the
   ALB-level record — TLS version, target processing time, ELB status versus
   target status — and the S3 copy. Re-add by giving the web stack a concrete
   `env` and guarding for credentialless CI synth.
