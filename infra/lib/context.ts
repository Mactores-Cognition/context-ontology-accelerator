// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { Node } from "constructs";
import {
  CTX_RESOURCE_PREFIX,
  CTX_ENV,
  CTX_PROJECT_TAG,
  CTX_GRAPH_BASE_URI,
  CTX_EVENT_SOURCE_PREFIX,
  CTX_LAMBDA_RESERVED_CONCURRENCY,
  DEFAULT_RESOURCE_PREFIX,
  DEFAULT_ENV,
  DEFAULT_PROJECT_TAG,
  DEFAULT_GRAPH_BASE_URI,
  DEFAULT_URN_PREFIX,
  DEFAULT_VOCAB_PREFIX,
  DEFAULT_DZ_TYPE_PREFIX,
  DEFAULT_LAMBDA_RESERVED_CONCURRENCY,
} from "./constants";

/** Resolved context values available to any stack or construct. */
export interface ResolvedContext {
  readonly prefix: string;
  readonly envName: string;
  readonly projectTag: string;
  readonly ssmPrefix: string;
  /** Base authority for knowledge-graph IRIs (e.g. "http://coa.amazon.com"). */
  readonly graphBaseUri: string;
  /** URN scheme prefix (e.g. "coa" → urn:coa:...). */
  readonly urnPrefix: string;
  /** RDF vocab short prefix (e.g. "coa" → coa:isMapped). */
  readonly vocabPrefix: string;
  /**
   * EventBridge source prefix. Tracks `resource_prefix` so co-located
   * deployments don't cross-trigger on the shared account-global bus
   * (e.g. "scl" → scl.ontology). Override with `event_source_prefix`.
   */
  readonly eventSourcePrefix: string;
  /** DataZone type name prefix in PascalCase (e.g. "Coa" → CoaTableMetadata). */
  readonly dzTypePrefix: string;
}

/** Read resource_prefix and env from CDK context with defaults. */
export function resolveContext(node: Node): ResolvedContext {
  const prefix =
    (node.tryGetContext(CTX_RESOURCE_PREFIX) as string) ??
    DEFAULT_RESOURCE_PREFIX;
  const envName = (node.tryGetContext(CTX_ENV) as string) ?? DEFAULT_ENV;
  const projectTag =
    (node.tryGetContext(CTX_PROJECT_TAG) as string) ?? DEFAULT_PROJECT_TAG;
  const graphBaseUri =
    (node.tryGetContext(CTX_GRAPH_BASE_URI) as string) ??
    DEFAULT_GRAPH_BASE_URI;
  // EventBridge source DOES track `prefix` (unlike the other brand tokens
  // below). An event source is a ROUTING attribute on a transient message, not
  // a persisted identifier: nothing is written into a data store keyed by it, so
  // deriving it per-deployment orphans nothing. It has to be deployment-scoped
  // because the bus it lands on (`default`) is ACCOUNT-GLOBAL and shared — two
  // deployments in one account+region emitting the same source means each one's
  // rules match the OTHER's events. That is not hypothetical: it provisioned
  // duplicate per-namespace VKG services that only the originating deployment's
  // teardown could ever delete, leaking ECS services and failing
  // test_namespace_delete_deregisters_vkg. The explicit `event_source_prefix`
  // context override is retained for the deliberate cross-deployment-interop
  // case (one deployment consuming another's events).
  const eventSourcePrefix =
    (node.tryGetContext(CTX_EVENT_SOURCE_PREFIX) as string) ?? prefix;
  // The remaining data-plane identifiers are intentionally NOT derived from
  // `prefix`. They are written into Neptune RDF and DataZone metadata, so they
  // must stay stable across deployments and prefixes — a prefix change would
  // re-mint every IRI and orphan the graph data already stored. See the BRAND
  // block in libs/ts-shared/src/constants.ts. They are safe to keep static
  // because each lives inside a container that is ALREADY prefix-isolated (its
  // own Neptune cluster, its own DataZone domain), unlike the shared event bus.
  // `prefix` governs physical resource names via prefixed()/ssmPrefix.
  const urnPrefix = DEFAULT_URN_PREFIX;
  const vocabPrefix = DEFAULT_VOCAB_PREFIX;
  const dzTypePrefix = DEFAULT_DZ_TYPE_PREFIX;
  return {
    prefix,
    envName,
    projectTag,
    ssmPrefix: `/${prefix}`,
    graphBaseUri,
    urnPrefix,
    vocabPrefix,
    eventSourcePrefix,
    dzTypePrefix,
  };
}

/** CDK context key that selects where table metadata is stored. */
export const CTX_METADATA_STORE = "metadata_store";

/**
 * `metadata_store` value that stores table metadata in a DynamoDB table
 * (TABLE_NAMES.METADATA_ASSETS) instead of a DataZone domain. Any other value,
 * or none, keeps DataZone.
 */
export const METADATA_STORE_DYNAMODB = "dynamodb";

/**
 * Value published to `/{prefix}/smus/domain-id` when no DataZone domain is
 * deployed (DynamoDB metadata store). Stacks still read that parameter, and
 * scripts/destroy.sh skips domain deletion when it holds this value.
 */
export const NO_DATAZONE_DOMAIN = "NO_DATAZONE_DOMAIN";

/** True when the `metadata_store` context selects the DynamoDB metadata store. */
export function isDynamoDbMetadataStore(node: Node): boolean {
  return node.tryGetContext(CTX_METADATA_STORE) === METADATA_STORE_DYNAMODB;
}

/** CDK context key that selects how callers reach ontology-engine and VKG. */
export const CTX_SERVICE_DISCOVERY = "service_discovery";

/**
 * `service_discovery` value that puts ontology-engine and the per-namespace VKG
 * services behind one internal ALB instead of Cloud Map private DNS. Any other
 * value, or none, keeps Cloud Map.
 */
export const SERVICE_DISCOVERY_ALB = "alb";

/** True when the `service_discovery` context selects the internal ALB. */
export function isAlbServiceDiscovery(node: Node): boolean {
  return node.tryGetContext(CTX_SERVICE_DISCOVERY) === SERVICE_DISCOVERY_ALB;
}

/**
 * CDK context keys for HTTPS on the internal ALB (`service_discovery=alb`).
 * `service_cert` picks where the certificate comes from: `private-ca` (the
 * default; AWS Private CA, no domain needed) or `acm-public` (a public ACM
 * certificate for `service_domain`, DNS-validated in the Route 53 public zone
 * `public_zone_id`).
 */
export const CTX_SERVICE_CERT = "service_cert";
export const CTX_SERVICE_DOMAIN = "service_domain";
export const CTX_PUBLIC_ZONE_ID = "public_zone_id";
export const SERVICE_CERT_PRIVATE_CA = "private-ca";
export const SERVICE_CERT_ACM_PUBLIC = "acm-public";

/** Resolved HTTPS settings for the internal ALB. */
export interface ServiceTlsSettings {
  /** Certificate source: `private-ca` or `acm-public`. */
  readonly mode:
    | typeof SERVICE_CERT_PRIVATE_CA
    | typeof SERVICE_CERT_ACM_PUBLIC;
  /** Route 53 private zone name; the certificate covers `*.<domainName>`. */
  readonly domainName: string;
  /** Public hosted zone that validates the certificate (`acm-public` only). */
  readonly publicZoneId?: string;
}

/**
 * Resolve the HTTPS settings for the internal ALB. The private-CA default uses
 * the internal zone `<prefix>-<env>.internal`.
 *
 * @throws if `service_cert` is unknown, or `acm-public` lacks its domain or zone.
 */
export function resolveServiceTls(node: Node): ServiceTlsSettings {
  const mode =
    (node.tryGetContext(CTX_SERVICE_CERT) as string | undefined) ??
    SERVICE_CERT_PRIVATE_CA;
  const domain = node.tryGetContext(CTX_SERVICE_DOMAIN) as string | undefined;
  if (mode === SERVICE_CERT_PRIVATE_CA) {
    const { prefix, envName } = resolveContext(node);
    return { mode, domainName: domain ?? `${prefix}-${envName}.internal` };
  }
  if (mode === SERVICE_CERT_ACM_PUBLIC) {
    const publicZoneId = node.tryGetContext(CTX_PUBLIC_ZONE_ID) as
      | string
      | undefined;
    if (!domain || !publicZoneId) {
      throw new Error(
        `${CTX_SERVICE_CERT}=${SERVICE_CERT_ACM_PUBLIC} needs ${CTX_SERVICE_DOMAIN} and ${CTX_PUBLIC_ZONE_ID}`,
      );
    }
    return { mode, domainName: domain, publicZoneId };
  }
  throw new Error(
    `Unknown ${CTX_SERVICE_CERT}: ${mode} (expected ${SERVICE_CERT_PRIVATE_CA} or ${SERVICE_CERT_ACM_PUBLIC})`,
  );
}

/**
 * SSM parameter holding the private CA's root certificate, which callers of
 * ontology-engine and VKG add to their trust store. Undefined unless the
 * internal ALB uses AWS Private CA (Cloud Map mode, or a public certificate).
 */
export function serviceCaCertParamName(node: Node): string | undefined {
  if (!isAlbServiceDiscovery(node)) return undefined;
  if (resolveServiceTls(node).mode !== SERVICE_CERT_PRIVATE_CA)
    return undefined;
  return `${resolveContext(node).ssmPrefix}/services/ca-cert`;
}

/** Environment variable that carries {@link serviceCaCertParamName} to callers. */
export const SERVICE_CA_CERT_PARAM_ENV = "SERVICE_CA_CERT_PARAM";

/** Return `{prefix}-{env}-{name}`. */
export function prefixed(node: Node, name: string): string {
  const { prefix, envName } = resolveContext(node);
  return `${prefix}-${envName}-${name}`;
}

/**
 * Resource-tag key that binds a resource the platform reads, but does not own,
 * to the namespaces entitled to it — today a JDBC source's credential secret,
 * tagged `{prefix}:namespace = "<namespaceId> [<namespaceId> ...]"`.
 *
 * Carries `prefix` (not the static brand) for the same reason `eventSourcePrefix`
 * does: the resource being tagged is account-global and therefore shared, so two
 * deployments co-located in one account must bind independently. A secret
 * onboarded to `scl` is not readable by a `coa` deployment, whose IAM conditions
 * name their own key.
 *
 * Single source of truth for the synth side. The runtime derives the same key
 * from `RESOURCE_TAG_PREFIX` (see `namespace_tag_key()` in
 * libs/common/constants.py) — the two must agree exactly or registration writes
 * a tag the IAM conditions cannot match.
 */
export function namespaceTagKey(node: Node): string {
  return `${resolveContext(node).prefix}:namespace`;
}

/**
 * Resolve the `lambda_reserved_concurrency` context value shared by the VKG
 * reload and doc-preprocessing Lambdas.
 *
 * Returns the reserved-concurrency count to apply, or `undefined` meaning "do
 * not reserve" (pass straight to `reservedConcurrentExecutions` — CDK omits the
 * property when it is `undefined`). A value of `0` maps to `undefined` so that
 * on reduced-quota accounts the reservation can be disabled without CDK
 * rejecting a literal `0` reservation.
 *
 * Validates that the value is a non-negative integer and throws at synth time
 * otherwise, matching the fail-fast behavior of the other numeric context
 * knobs (e.g. `dbScanEnrichmentTimeoutMinutes`).
 */
export function resolveLambdaReservedConcurrency(
  node: Node,
): number | undefined {
  const raw: unknown = node.tryGetContext(CTX_LAMBDA_RESERVED_CONCURRENCY);
  const value = Number(raw ?? DEFAULT_LAMBDA_RESERVED_CONCURRENCY);
  if (!Number.isInteger(value) || value < 0) {
    throw new Error(
      `${CTX_LAMBDA_RESERVED_CONCURRENCY} must be a non-negative integer, got: ${String(raw)}`,
    );
  }
  return value === 0 ? undefined : value;
}
