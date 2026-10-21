// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import * as cdk from "aws-cdk-lib";
import * as cognito from "aws-cdk-lib/aws-cognito";

/** Supported identity provider types — set via cdk.json context `idpType`. */
export enum IdpType {
  /** Standalone Cognito User Pool (default). */
  COGNITO = "COGNITO",
  /** Cognito User Pool federated with a SAML 2.0 enterprise IdP. */
  SAML = "SAML",
  /** Direct integration with an external OIDC IdP — no Cognito. */
  OIDC = "OIDC",
}

/** Configuration for direct OIDC IdP integration (no Cognito). */
export interface OidcSettings {
  /** OIDC issuer URL (e.g. https://dev-123.okta.com/oauth2/default). */
  readonly issuerUrl: string;
  /** OIDC client ID registered with the external IdP. */
  readonly clientId: string;
  /** JWKS URI for token signature verification. */
  readonly jwksUri?: string;
  /** JWT claim name that carries group membership (e.g. "groups"). */
  readonly groupClaim?: string;
}

/** Maps an IdP group claim value to one or more application roles. */
export interface ClaimMapping {
  readonly groupValue: string;
  readonly mappedRoles: string[];
}

/** Initial Cognito user attributes for the admin user. */
export interface InitialCognitoUserAttributes {
  username: string;
  group: string;
  email: string;
}

export interface IdpAuthenticationStackProps extends cdk.StackProps {
  /** Override the IdP type. Defaults to context `idpType` → COGNITO. */
  readonly idpType?: IdpType;
  /** Required when idpType is OIDC. */
  readonly oidcSettings?: OidcSettings;
  /** Seed group→role claim mappings. */
  readonly claimsMappings?: ClaimMapping[];
  /** Initial Cognito admin user details (Cognito/SAML path only). */
  readonly initialUserDetails?: InitialCognitoUserAttributes;
  /** Custom attributes to add to the Cognito User Pool (COGNITO/SAML). */
  readonly cognitoCustomAttributes?: Record<string, cognito.ICustomAttribute>;
  /** Federated IdP attribute → Cognito attribute mappings (COGNITO/SAML). */
  readonly attributeMapping?: Record<string, cognito.ProviderAttribute>;
  /** SAML identity providers to register with the Cognito User Pool. */
  readonly samlProviders?: cognito.UserPoolIdentityProviderSamlProps[];
  /** OIDC identity providers to register with the Cognito User Pool. */
  readonly oidcProviders?: cognito.UserPoolIdentityProviderOidcProps[];
  /** Refresh token validity duration. @default cdk.Duration.hours(12) */
  readonly refreshTokenValidity?: cdk.Duration;
}

// ── Custom domains ─────────────────────────────────────────────────

/**
 * Custom-domain configuration for the public web surface.
 *
 * Three independent parts, each optional: the UI pair, the API pair, and the
 * hosted zone. Within a pair both fields are required or neither (validated in
 * `bin/app.ts`). The UI pair is what gives the ALB an HTTPS listener — without
 * it the UI is served over plain HTTP, which is dev-only. Omitting the API pair
 * leaves the API on its default `execute-api` endpoint.
 *
 * Both certificates MUST be in the **deploy region**: the UI cert terminates
 * TLS on the ALB and the API cert on API Gateway, neither of which is global.
 */
export interface CustomDomainConfig {
  /** Web UI custom domain, e.g. "coa.amazon.com". */
  readonly uiDomainName?: string;
  /** ARN of the ACM certificate for {@link uiDomainName} (deploy region). */
  readonly uiCertificateArn?: string;
  /** REST API (API Gateway) custom domain, e.g. "api.coa.amazon.com". */
  readonly apiDomainName?: string;
  /** ARN of the ACM certificate for {@link apiDomainName} (deploy region). */
  readonly apiCertificateArn?: string;
  /** Route53 hosted zone id for the domains; alias records are created here. */
  readonly hostedZoneId?: string;
}

// ── SSM Config (JSON-serializable) ─────────────────────────────────

/**
 * Deployment configuration loaded from SSM Parameter Store.
 *
 * This is the JSON-serializable counterpart of IdpAuthenticationStackProps.
 * `buildAuthProps()` in app.ts converts these plain values into CDK types.
 */
export interface SsmConfig {
  readonly idpType?: IdpType;
  readonly oidcSettings?: OidcSettings;
  readonly claimsMappings?: ClaimMapping[];
  /** Initial admin email — receives a temporary password on first deploy. */
  readonly initialAdminEmail?: string;
  /** SAML providers — metadataUrl or metadataContent (not both). */
  readonly samlProviders?: Array<{
    readonly name: string;
    readonly metadataUrl?: string;
    readonly metadataContent?: string;
  }>;
  /** OIDC providers to federate through Cognito. */
  readonly oidcProviders?: Array<{
    readonly name: string;
    readonly clientId: string;
    readonly clientSecret: string;
    readonly issuerUrl: string;
    readonly scopes?: string[];
  }>;
  /** Custom Cognito attributes (key = name, value = "string" | "number"). */
  readonly cognitoCustomAttributes?: Record<string, "string" | "number">;
  /** Federated attribute mapping (key = Cognito attr, value = IdP attr name). */
  readonly attributeMapping?: Record<string, string>;
  /** Refresh token validity in hours. @default 12 */
  readonly refreshTokenValidityHours?: number;
  /** Custom domain for the web app. */
  readonly customDomain?: string;

  /** Bedrock LLM model ID for query resolution (NL-to-SPARQL, synthesis).
   *  Defaults to DEFAULT_BEDROCK_LLM_MODEL_ID when omitted. */
  readonly bedrockLlmModelId?: string;

  /** Bedrock embedding model ID used by every embedding producer and consumer
   *  (induction, doc-kg-build, metric matching, serve retrieval). All must use
   *  the SAME model — vectors from different models are not comparable — so
   *  this single key feeds them all. Changing it after data exists is a data
   *  migration; set it at initial deploy. Accepts geo inference profiles
   *  (us./eu.) and bare in-region IDs. Defaults to
   *  amazon.nova-2-multimodal-embeddings-v1:0 (DEFAULT_BEDROCK_MODEL_ID). */
  readonly bedrockEmbedModelId?: string;

  /** Embedding vector dimension for the model above. Baked into the OpenSearch
   *  index at creation (not changeable afterwards), so it must be settable at
   *  initial deploy alongside the model ID. Defaults to 1024. */
  readonly bedrockEmbedDimensions?: number;

  /** Bedrock LLM model ID for ontology induction and description
   *  generation (ontology-engine LLM_MODEL_ID /
   *  DESCRIPTION_LLM_MODEL_ID). Defaults to us.amazon.nova-pro-v1:0
   *  (DEFAULT_BEDROCK_INDUCTION_MODEL_ID). */
  readonly bedrockInductionLlmModelId?: string;

  /** Bedrock chat/completion model ID for source enrichment, constraint
   *  inference, grounding rerank, and document KG extraction (the shared
   *  BedrockClient default and the doc-ingestion inference-profile ARN).
   *  Defaults to us.amazon.nova-pro-v1:0 (DEFAULT_BEDROCK_CHAT_MODEL_ID). */
  readonly bedrockChatModelId?: string;

  /** Model IDs allowed for per-request override via options.modelId.
   *  When omitted or empty, any valid model ID is accepted. */
  readonly allowedOverrideModels?: string[];
}
