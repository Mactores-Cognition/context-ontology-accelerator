// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import * as fs from "fs";
import * as cdk from "aws-cdk-lib";
import * as cr from "aws-cdk-lib/custom-resources";
import * as iam from "aws-cdk-lib/aws-iam";
import * as route53 from "aws-cdk-lib/aws-route53";
import * as ssm from "aws-cdk-lib/aws-ssm";
import { Construct } from "constructs";
import * as ec2 from "aws-cdk-lib/aws-ec2";
import { WebAlbConstruct } from "../../constructs/web-alb-construct";
import { RuntimeConfig } from "@coa/shared";
import { SCLStack } from "../../constructs/scl-stack";
import { resolveContext } from "../../context";
import { fromRoot } from "../../paths";
import { CustomDomainConfig } from "../../types";

/**
 * Read the monorepo version from the repo-root VERSION file (the single source
 * of truth kept in sync across all manifests by `scripts/sync_version.py`).
 * Surfaced to the frontend via runtime-config.json so the UI shows the deployed
 * version without a rebuild. Returns undefined if the file is missing so a
 * deploy is never blocked on it.
 */
export function readRepoVersion(
  versionFile: string = fromRoot("VERSION"),
): string | undefined {
  try {
    return fs.readFileSync(versionFile, "utf-8").trim() || undefined;
  } catch {
    return undefined;
  }
}

/**
 * Assemble the {@link RuntimeConfig} written to `runtime-config.json`. Optional
 * fields are omitted (not emitted as `undefined`) so a missing apiEndpoint,
 * serveRuntimeArn, or version leaves no key behind — the frontend treats an
 * absent key and an absent value identically. Pure and exported so the
 * include/omit behavior is unit-testable without a synth.
 */
export function buildRuntimeConfig(args: {
  region: string;
  stage: string;
  authority: string;
  clientId: string;
  apiEndpoint?: string;
  serveRuntimeArn?: string;
  version?: string;
}): RuntimeConfig {
  const {
    region,
    stage,
    authority,
    clientId,
    apiEndpoint,
    serveRuntimeArn,
    version,
  } = args;
  return {
    region,
    stage,
    authority,
    clientId,
    ...(apiEndpoint && { apiEndpoint }),
    ...(serveRuntimeArn && { serveRuntimeArn }),
    ...(version && { version }),
  };
}

export interface WebStackProps extends cdk.StackProps {
  /**
   * Whether the auth stack provisioned a Cognito User Pool (COGNITO/SAML
   * idpType) — gates the Cognito-specific callback-URL patch below, which
   * has no equivalent for a direct external-OIDC deployment (no user pool
   * to patch). Read from SSM instead of the {@link WebStackProps}'s
   * userPoolId directly to avoid a cross-stack export on the auth stack's
   * UserPool/UserPoolClient (see idp-authentication-stack.ts).
   */
  readonly isCognitoMode: boolean;
  /** Backend API endpoint URL. */
  readonly apiEndpoint?: string;
  /** API Gateway REST API ID (used to update CORS once the UI origin is known). */
  readonly apiRestApiId?: string;
  /** API Gateway stage name (used to trigger redeployment after CORS update). */
  readonly apiStageName?: string;
  /** VPC hosting the UI load balancer and nginx tasks. */
  readonly vpc: ec2.IVpc;
  /** Custom domain configuration for the UI and/or API. */
  readonly customDomain?: CustomDomainConfig;
  /** REGIONAL WebACL ARN for the UI ALB. One is created when omitted. */
  readonly webAclArn?: string;
  /** AgentCore Runtime ARN for SSE streaming queries. */
  readonly serveRuntimeArn?: string;
  /**
   * Fully custom Content-Security-Policy header value, replacing the derived
   * one. Wired from the `content_security_policy` CDK context key in
   * `bin/app.ts` (issue #130).
   */
  readonly contentSecurityPolicy?: string;
}

/**
 * Web application hosting stack for the Context Ontology Accelerator.
 *
 * Serves the React SPA from an nginx container on ECS Fargate behind an
 * internet-facing ALB. TLS terminates on the ALB with an operator-supplied ACM
 * certificate; without one the ALB serves plain HTTP, which is dev-only (see
 * {@link WebAlbConstruct}).
 *
 * When no UI custom domain is configured the origin is only known at deploy
 * time, so this stack patches:
 * 1. **Cognito callback URLs** — but only to an https origin; Cognito rejects
 *    non-localhost http callbacks, so the HTTP path registers localhost only.
 * 2. **API Gateway CORS** — gateway response headers plus an API redeployment.
 *
 * `runtime-config.json` is handed to the container as RUNTIME_CONFIG_JSON and
 * written to the webroot at start-up, so the SPA discovers its backends without
 * a rebuild.
 */
export class WebStack extends SCLStack {
  constructor(scope: Construct, id: string, props: WebStackProps) {
    super(scope, id, props);

    const { ssmPrefix } = resolveContext(this.node);

    // IdP issuer/client ID via SSM (avoids a cross-stack export on the auth
    // stack's UserPool/UserPoolClient — see idp-authentication-stack.ts for
    // the writer, populated on both the Cognito/SAML and direct-OIDC paths).
    const authority = ssm.StringParameter.valueForStringParameter(
      this,
      `${ssmPrefix}/issuer`,
    );
    const clientId = ssm.StringParameter.valueForStringParameter(
      this,
      `${ssmPrefix}/userpool-client-id`,
    );
    // Only populated on the Cognito/SAML path (idp-authentication-stack.ts's
    // OIDC branch never writes this parameter) — gated by isCognitoMode so a
    // direct-OIDC deployment never attempts to resolve a nonexistent param.
    const userPoolId = props.isCognitoMode
      ? ssm.StringParameter.valueForStringParameter(
          this,
          `${ssmPrefix}/userpool-id`,
        )
      : "";
    // Hosted-UI domain prefix, for the CSP. Same Cognito-only gate as above:
    // the OIDC branch of the auth stack provisions no hosted UI, so there is no
    // parameter to read and nothing to allowlist (a direct-OIDC deployment's
    // token endpoint is on its own authority, already covered).
    const cognitoDomainPrefix = props.isCognitoMode
      ? ssm.StringParameter.valueForStringParameter(
          this,
          `${ssmPrefix}/cognito-domain-prefix`,
        )
      : undefined;

    const uiDomainName = props.customDomain?.uiDomainName;
    const uiCertificateArn = props.customDomain?.uiCertificateArn;
    const apiDomainName = props.customDomain?.apiDomainName;

    // Prefer the custom API domain when configured; otherwise the direct
    // API endpoint. Undefined when neither is set — buildRuntimeConfig omits it.
    const apiEndpoint = apiDomainName
      ? `https://${apiDomainName}`
      : props.apiEndpoint;

    const runtimeConfig = buildRuntimeConfig({
      region: this.region,
      stage: this.envName,
      authority,
      clientId,
      apiEndpoint,
      serveRuntimeArn: props.serveRuntimeArn,
      version: readRepoVersion(),
    });

    const web = new WebAlbConstruct(this, "WebAlb", {
      vpc: props.vpc,
      runtimeConfig,
      ...(uiDomainName && { uiDomainName }),
      ...(uiCertificateArn && { uiCertificateArn }),
      ...(cognitoDomainPrefix && { cognitoDomainPrefix }),
      ...(props.webAclArn && { webAclArn: props.webAclArn }),
      ...(props.contentSecurityPolicy && {
        contentSecurityPolicy: props.contentSecurityPolicy,
      }),
    });

    const siteUrl = web.siteUrl;

    // Optional Route53 alias → the ALB. L1 CfnRecordSet so only the zone id is
    // needed. A only: the ALB is IPv4, so an AAAA record would resolve empty.
    // When omitted, DNS is managed externally.
    const hostedZoneId = props.customDomain?.hostedZoneId;
    if (hostedZoneId && uiDomainName) {
      new route53.CfnRecordSet(this, "UiAliasA", {
        hostedZoneId,
        name: `${uiDomainName}.`,
        type: "A",
        aliasTarget: {
          dnsName: web.loadBalancer.loadBalancerDnsName,
          hostedZoneId: web.loadBalancer.loadBalancerCanonicalHostedZoneId,
          evaluateTargetHealth: false,
        },
      });
    }

    // ── Patch Cognito callback URLs ────────────────────────────────
    // Uses raw AWS API values (not CDK enums) because AwsCustomResource
    // calls the Cognito API directly. updateUserPoolClient is a full
    // replacement — all parameters must match the auth stack's addClient()
    // config or they will be reset to defaults.
    // Mapping: authFlows.userSrp → ALLOW_USER_SRP_AUTH,
    //          (implicit)        → ALLOW_REFRESH_TOKEN_AUTH,
    //          OAuthScope.OPENID → "openid", etc.
    //
    // Cognito rejects non-localhost http callback URLs, so on the plain-HTTP
    // path the ALB origin cannot be registered at all: in dev we register only
    // localhost (keeping `pnpm dev` sign-in working), and otherwise we skip the
    // patch entirely rather than fail the deploy or send an empty CallbackURLs,
    // which Cognito also rejects. prod never reaches here — bin/app.ts requires
    // a UI custom domain there.
    const isDev = this.envName === "dev";
    const callbackUrls = [
      ...(web.insecureTransport ? [] : [`${siteUrl}/authenticate/`]),
      ...(isDev ? ["http://localhost:5173/authenticate/"] : []),
    ];
    const logoutUrls = web.insecureTransport
      ? isDev
        ? ["http://localhost:5173/"]
        : []
      : [`${siteUrl}/`];

    if (props.isCognitoMode && callbackUrls.length > 0) {
      new cr.AwsCustomResource(this, "UpdateCognitoCallbacks", {
        installLatestAwsSdk: false,
        onUpdate: {
          service: "CognitoIdentityServiceProvider",
          action: "updateUserPoolClient",
          parameters: {
            UserPoolId: userPoolId,
            ClientId: clientId,
            CallbackURLs: callbackUrls,
            LogoutURLs: logoutUrls,
            ExplicitAuthFlows: [
              "ALLOW_USER_SRP_AUTH",
              "ALLOW_REFRESH_TOKEN_AUTH",
              // Must mirror idp-authentication-stack.ts addClient() authFlows.
              // updateUserPoolClient is a full replacement, so omitting this
              // in dev silently strips ALLOW_USER_PASSWORD_AUTH (added there
              // for integ-test/CLI sign-in) after this custom resource runs.
              ...(isDev ? ["ALLOW_USER_PASSWORD_AUTH"] : []),
            ],
            AllowedOAuthFlows: ["code"],
            AllowedOAuthScopes: ["email", "openid", "profile"],
            SupportedIdentityProviders: ["COGNITO"],
            AllowedOAuthFlowsUserPoolClient: true,
            // Must mirror idp-authentication-stack.ts addClient(). This is a
            // full replacement, so omitting it would reset the client to the
            // API default (LEGACY), re-enabling user-existence error leakage.
            PreventUserExistenceErrors: "ENABLED",
          },
          physicalResourceId: cr.PhysicalResourceId.of(
            `cognito-callbacks-${clientId}`,
          ),
        },
        policy: cr.AwsCustomResourcePolicy.fromStatements([
          new iam.PolicyStatement({
            actions: ["cognito-idp:UpdateUserPoolClient"],
            resources: [
              `arn:aws:cognito-idp:${this.region}:${this.account}:userpool/${userPoolId}`,
            ],
          }),
        ]),
      });
    }

    // ── Patch API Gateway CORS origin ──────────────────────────────
    if (props.apiRestApiId && props.apiStageName) {
      const corsHeaders = {
        "gatewayresponse.header.Access-Control-Allow-Origin": `'${siteUrl}'`,
        "gatewayresponse.header.Strict-Transport-Security":
          "'max-age=63072000; includeSubDomains; preload'",
        "gatewayresponse.header.X-Content-Type-Options": "'nosniff'",
      };

      const apiArn = `arn:aws:apigateway:${this.region}::/restapis/${props.apiRestApiId}`;

      // Update 4XX gateway response
      new cr.AwsCustomResource(this, "UpdateApiCors4XX", {
        installLatestAwsSdk: false,
        onUpdate: {
          service: "APIGateway",
          action: "putGatewayResponse",
          parameters: {
            restApiId: props.apiRestApiId,
            responseType: "DEFAULT_4XX",
            responseParameters: corsHeaders,
          },
          physicalResourceId: cr.PhysicalResourceId.of(
            `api-cors-4xx-${props.apiRestApiId}`,
          ),
        },
        policy: cr.AwsCustomResourcePolicy.fromStatements([
          new iam.PolicyStatement({
            actions: ["apigateway:PUT", "apigateway:PATCH", "apigateway:POST"],
            resources: [`${apiArn}/*`],
          }),
        ]),
      });

      // Update 5XX gateway response
      new cr.AwsCustomResource(this, "UpdateApiCors5XX", {
        installLatestAwsSdk: false,
        onUpdate: {
          service: "APIGateway",
          action: "putGatewayResponse",
          parameters: {
            restApiId: props.apiRestApiId,
            responseType: "DEFAULT_5XX",
            responseParameters: corsHeaders,
          },
          physicalResourceId: cr.PhysicalResourceId.of(
            `api-cors-5xx-${props.apiRestApiId}`,
          ),
        },
        policy: cr.AwsCustomResourcePolicy.fromStatements([
          new iam.PolicyStatement({
            actions: ["apigateway:PUT", "apigateway:PATCH", "apigateway:POST"],
            resources: [`${apiArn}/*`],
          }),
        ]),
      });

      // Trigger API redeployment so the CORS changes take effect
      new cr.AwsCustomResource(this, "RedeployApi", {
        installLatestAwsSdk: false,
        onUpdate: {
          service: "APIGateway",
          action: "createDeployment",
          parameters: {
            restApiId: props.apiRestApiId,
            stageName: props.apiStageName,
            description: "Auto-redeploy after CORS origin update",
          },
          physicalResourceId: cr.PhysicalResourceId.of(
            `api-redeploy-${Date.now()}`,
          ),
        },
        policy: cr.AwsCustomResourcePolicy.fromStatements([
          new iam.PolicyStatement({
            actions: ["apigateway:POST"],
            resources: [`${apiArn}/*`],
          }),
        ]),
      });
    }
  }
}
