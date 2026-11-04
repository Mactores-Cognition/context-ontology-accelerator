// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

/**
 * Bedrock guardrails on/off switch for the whole deployment.
 *
 * On unless the deploy command passes `-c guardrails_enabled=false`. When off,
 * the stacks stop handing the guardrail ids to the code that uses them, and each
 * of those already runs unguarded without an id:
 *   - sources: database enrichment (GUARDRAIL_SSM_PARAM) and document screening
 *     (RETRIEVAL_GUARDRAIL_ID / RETRIEVAL_GUARDRAIL_VERSION);
 *   - ontology engine: SHACL rule generation (GUARDRAIL_SSM_PARAM);
 *   - serve: SERVE_GUARDRAILS_DISABLED=true (serve then ignores both ids and logs
 *     guardrails_disabled_by_configuration at ERROR on every start).
 * The guardrail stack itself is still deployed, so switching back on needs no
 * new resources. Refused in prod: the guardrail is the prompt-attack boundary.
 */

import { IConstruct } from "constructs";

export const GUARDRAILS_CONTEXT_KEY = "guardrails_enabled";

const ON = ["true", "1", "on", "yes"];
const OFF = ["false", "0", "off", "no"];

/**
 * Whether Bedrock guardrails are on for this deployment.
 *
 * @param scope any construct in the app (reads the CDK context).
 * @param envName the deployment environment (e.g. "dev", "prod").
 * @returns true unless `guardrails_enabled` is set to a false value.
 * @throws if the value is not a recognised true/false spelling, or if it turns
 *   the guardrails off in prod.
 */
export function guardrailsEnabled(scope: IConstruct, envName: string): boolean {
  const raw = scope.node.tryGetContext(GUARDRAILS_CONTEXT_KEY);
  if (raw === undefined) return true;
  const value = String(raw).trim().toLowerCase();
  if (ON.includes(value)) return true;
  if (!OFF.includes(value)) {
    throw new Error(
      `${GUARDRAILS_CONTEXT_KEY} must be true or false (got "${String(raw)}")`,
    );
  }
  if (envName === "prod") {
    throw new Error(
      `Bedrock guardrails cannot be turned off in prod (${GUARDRAILS_CONTEXT_KEY}=false)`,
    );
  }
  return false;
}
