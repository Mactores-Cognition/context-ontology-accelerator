// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import * as cdk from "aws-cdk-lib";
import { RuntimeConfig } from "@coa/shared";

/**
 * Build the Content-Security-Policy for the SPA.
 *
 * `script-src 'self'` is the primary control — it blocks inline and
 * cross-origin script execution. `connect-src`/`frame-src` are scoped to the
 * app's real endpoints (API, OIDC authority, Cognito, AgentCore, S3 presign).
 * `style-src` keeps `'unsafe-inline'` because Cloudscape/emotion inject styles
 * at runtime; this does not weaken script protection.
 *
 * @param runtimeConfig Deployed frontend config; endpoints are derived from it.
 * @param override Fully custom policy, replacing the derived one.
 * @param cognitoDomainPrefix Cognito hosted-UI prefix, when a user pool exists.
 *   Required for sign-in — see the `cognitoHostedUiOrigin` note below.
 * @param insecureTransport Serving over plain HTTP; omits
 *   `upgrade-insecure-requests`, which would otherwise rewrite every asset
 *   request to https:// against a listener with no TLS.
 */
export function buildContentSecurityPolicy(
  runtimeConfig: RuntimeConfig,
  override?: string,
  cognitoDomainPrefix?: string,
  insecureTransport = false,
): string {
  if (override) return override;

  const originOf = (url?: string): string | undefined => {
    if (!url) return undefined;
    // Cross-stack values (e.g. the API Gateway URL) are unresolved tokens at
    // synth, so `new URL()` throws. Build the origin in CloudFormation instead:
    // `https://<host>/<stage>/` split on "/" puts the host at index 2.
    if (cdk.Token.isUnresolved(url)) {
      return `https://${cdk.Fn.select(2, cdk.Fn.split("/", url))}`;
    }
    try {
      return new URL(url).origin;
    } catch {
      return undefined;
    }
  };

  const apiOrigin = originOf(runtimeConfig.apiEndpoint);
  const authOrigin = originOf(runtimeConfig.authority);

  // AgentCore origin for SSE streaming (Playground fetches directly).
  const agentCoreOrigin = runtimeConfig.serveRuntimeArn
    ? `https://bedrock-agentcore.${runtimeConfig.region}.amazonaws.com`
    : undefined;

  // Cognito user-pool IdP endpoint — the browser OIDC flow always calls this
  // directly (discovery, JWKS, token). Derived from `region` rather than
  // `authority`, which can be empty/unresolved at synth and would silently
  // drop the origin and block sign-in.
  const cognitoOrigin = runtimeConfig.region
    ? `https://cognito-idp.${runtimeConfig.region}.amazonaws.com`
    : undefined;

  // Cognito HOSTED-UI origin — a DIFFERENT host from `cognitoOrigin`, and where
  // the browser actually exchanges the authorization code. Needed in BOTH
  // directives: connect-src for the code→token POST and logout-time refresh
  // revocation, frame-src for automaticSilentRenew's hidden iframe.
  // Interpolated, not parsed: the prefix is a CFN dynamic reference, so
  // `new URL()` would throw and drop the origin (issue #130).
  const cognitoHostedUiOrigin =
    cognitoDomainPrefix && runtimeConfig.region
      ? `https://${cognitoDomainPrefix}.auth.${runtimeConfig.region}.amazoncognito.com`
      : undefined;

  // S3 origins for the browser→S3 presigned PUT/GET used by OSI import/export.
  // Region-scoped like cognitoOrigin: the OSI bucket lives in a later service
  // stack, so scope to the region rather than couple this to it (issue #103).
  const s3Origins = runtimeConfig.region
    ? [
        `https://*.s3.${runtimeConfig.region}.amazonaws.com`,
        `https://s3.${runtimeConfig.region}.amazonaws.com`,
        // us-east-1 (and some botocore versions) presign against the legacy
        // global host, which has no region segment (issue #211).
        `https://*.s3.amazonaws.com`,
        `https://s3.amazonaws.com`,
      ]
    : [];

  const connect = new Set<string>(["'self'"]);
  const frame = new Set<string>(["'self'"]);
  for (const o of [
    apiOrigin,
    authOrigin,
    agentCoreOrigin,
    cognitoOrigin,
    cognitoHostedUiOrigin,
    ...s3Origins,
  ]) {
    if (o) connect.add(o);
  }
  if (authOrigin) frame.add(authOrigin); // OIDC silent-renew iframe
  if (cognitoOrigin) frame.add(cognitoOrigin); // OIDC iframe against the IdP
  if (cognitoHostedUiOrigin) frame.add(cognitoHostedUiOrigin); // silent-renew iframe

  // No API endpoint configured at all → permit scheme-level so the app still
  // works. A cross-stack (token) endpoint is resolved above and never lands here.
  if (!apiOrigin) {
    connect.add("https:");
  }

  // `default-src 'none'`: every fetch directive the SPA uses is declared
  // explicitly, so the fallback only catches types it never loads.
  // `form-action 'none'`: the SPA never performs a native form submission.
  // `base-uri 'none'`: index.html has no <base> element.
  return [
    "default-src 'none'",
    "script-src 'self'",
    "style-src 'self' 'unsafe-inline'",
    "img-src 'self' data:",
    "font-src 'self' data:",
    `connect-src ${[...connect].join(" ")}`,
    `frame-src ${[...frame].join(" ")}`,
    "object-src 'none'",
    "base-uri 'none'",
    "frame-ancestors 'none'",
    "form-action 'none'",
    ...(insecureTransport ? [] : ["upgrade-insecure-requests"]),
  ].join("; ");
}
