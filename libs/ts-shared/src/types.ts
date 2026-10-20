// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

/**
 * Runtime configuration delivered to the frontend at deploy time.
 *
 * Passed to the UI container as the `RUNTIME_CONFIG_JSON` environment variable
 * and written to the webroot as `runtime-config.json` at start-up. The web-app
 * fetches it at mount; CDK supplies the value during deployment.
 *
 * Never put secrets here: it is served to the browser and is readable via
 * `ecs:DescribeTaskDefinition`.
 */
export interface RuntimeConfig {
  readonly region: string;
  readonly stage: string;
  /** OIDC / Cognito authority URL. */
  readonly authority: string;
  /** OIDC / Cognito client ID. */
  readonly clientId: string;
  /** Backend API endpoint (set once API stack is wired). */
  readonly apiEndpoint?: string;
  /** AgentCore Runtime ARN for SSE streaming queries. Frontend constructs the invocations URL. */
  readonly serveRuntimeArn?: string;
  /** Monorepo version (repo-root VERSION file) surfaced in the UI. E.g. "0.2.2". */
  readonly version?: string;
}
