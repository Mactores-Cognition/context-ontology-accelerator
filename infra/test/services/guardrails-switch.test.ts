// Bedrock guardrails on/off switch: `-c guardrails_enabled=false` on the deploy
// command turns the guardrails off in every part of COA that uses them; the
// default (no key) keeps them on. Expected results G1-G6.

import * as cdk from "aws-cdk-lib";
import * as dynamodb from "aws-cdk-lib/aws-dynamodb";
import * as ec2 from "aws-cdk-lib/aws-ec2";
import * as lambda from "aws-cdk-lib/aws-lambda";
import { Template } from "aws-cdk-lib/assertions";
import { NetworkStack } from "../../lib/stacks/foundation/network-stack";
import { StorageStack } from "../../lib/stacks/foundation/storage-stack";
import { OntologyStack } from "../../lib/stacks/services/ontology-stack";
import { ServeStack } from "../../lib/stacks/services/serve-stack";
import { SourcesStack } from "../../lib/stacks/services/sources-stack";
import { DEFAULT_ENV, DEFAULT_RESOURCE_PREFIX } from "../../lib/constants";
import { guardrailsEnabled } from "../../lib/utils/guardrails";

jest.mock("../../lib/utils/python-bundling", () => ({
  bundlePython: () =>
    lambda.Code.fromInline("def handler(event, context): pass"),
}));

const TEST_ENV = { account: "123456789012", region: "us-east-1" };
const BASE_CONTEXT = {
  resource_prefix: DEFAULT_RESOURCE_PREFIX,
  env: DEFAULT_ENV,
  ecr_repository_arn: "arn:aws:ecr:us-east-1:123456789012:repository/coa-test",
  ecr_repository_name: "coa-test",
  sources_db_enrichment_image_uri:
    "123456789012.dkr.ecr.us-east-1.amazonaws.com/coa-test:db-enrichment-test",
  sources_preprocessing_image_uri:
    "123456789012.dkr.ecr.us-east-1.amazonaws.com/coa-test:preprocessing-test",
  sources_kg_build_image_uri:
    "123456789012.dkr.ecr.us-east-1.amazonaws.com/coa-test:kg-build-test",
  ontology_engine_image_uri:
    "123456789012.dkr.ecr.us-east-1.amazonaws.com/coa-test:ontology-engine-test",
  context_manager_image_uri:
    "123456789012.dkr.ecr.us-east-1.amazonaws.com/coa-dev:latest",
  "aws:cdk:bundling-stacks": [],
};

const GUARDRAIL_VARS = [
  "GUARDRAIL_SSM_PARAM",
  "RETRIEVAL_GUARDRAIL_ID",
  "RETRIEVAL_GUARDRAIL_VERSION",
];

function app(context: Record<string, unknown> = {}): cdk.App {
  return new cdk.App({ context: { ...BASE_CONTEXT, ...context } });
}

function sources(context: Record<string, unknown> = {}): Template {
  const a = app(context);
  const network = new NetworkStack(a, "Network", { env: TEST_ENV });
  const storage = new StorageStack(a, "Storage", { network, env: TEST_ENV });
  return Template.fromStack(
    new SourcesStack(a, "Sources", { network, storage, env: TEST_ENV }),
  );
}

function ontology(context: Record<string, unknown> = {}): Template {
  const a = app(context);
  const network = new NetworkStack(a, "Network");
  return Template.fromStack(
    new OntologyStack(a, "Ontology", {
      network,
      serviceNamespace: network.serviceNamespace,
      neptuneClusterArn:
        "arn:aws:neptune-db:us-east-1:123456789012:cluster-abc123/*",
      neptuneClusterEndpoint:
        "test-neptune.cluster-abc.us-east-1.neptune.amazonaws.com",
      ontologyArtifactsBucket: cdk.aws_s3.Bucket.fromBucketName(
        network,
        "ArtifactsBucket",
        "coa-dev-ontology-artifacts-123456789012",
      ),
      smusDomainId: "dzd-test123",
      allowedOrigin: "https://test.example.com",
    }),
  );
}

function serve(context: Record<string, unknown> = {}): Template {
  const a = app(context);
  const dep = new cdk.Stack(a, "DepStack", { env: TEST_ENV });
  const vpc = new ec2.Vpc(dep, "Vpc", { maxAzs: 2 });
  const sg = (id: string) => new ec2.SecurityGroup(dep, id, { vpc });
  const table = (id: string) =>
    new dynamodb.Table(dep, id, {
      partitionKey: { name: "PK", type: dynamodb.AttributeType.STRING },
    });
  return Template.fromStack(
    new ServeStack(a, "Serve", {
      env: TEST_ENV,
      vpc,
      aossSecurityGroup: sg("AossSG"),
      neptuneSecurityGroup: sg("NeptuneSG"),
      lambdaSecurityGroup: sg("LambdaSG"),
      neptuneClusterArn:
        "arn:aws:neptune-db:us-east-1:123456789012:cluster:test-cluster/*",
      neptuneEndpoint:
        "test-neptune.cluster-abc.us-east-1.neptune.amazonaws.com",
      ontologyBucketArn: "arn:aws:s3:::coa-dev-ontology-artifacts",
      vkgEndpoint: "http://vkg.coa-dev-services.local:8080",
      rolesTable: table("Roles"),
      resourceRoleMappingsTable: table("RRM"),
    }),
  );
}

/** Names of the environment variables set on every ECS container. */
function containerVarNames(t: Template): string[] {
  return Object.values(t.findResources("AWS::ECS::TaskDefinition")).flatMap(
    (d: any) =>
      (d.Properties.ContainerDefinitions ?? []).flatMap((c: any) =>
        (c.Environment ?? []).map((e: { Name: string }) => e.Name),
      ),
  );
}

/** Environment variables of every AgentCore runtime. */
function runtimeVars(t: Template): Record<string, unknown>[] {
  return Object.values(t.findResources("AWS::BedrockAgentCore::Runtime")).map(
    (r: any) => r.Properties.EnvironmentVariables ?? {},
  );
}

function scope(context: Record<string, unknown>): cdk.Stack {
  return new cdk.Stack(app(context), "Scope");
}

describe("guardrails_enabled context switch", () => {
  test("G1 reading the switch: default on; false/0/off/no turn it off; anything else is refused", () => {
    expect(guardrailsEnabled(scope({}), "dev")).toBe(true);
    for (const on of ["true", "1", "on", "yes", " TRUE ", true]) {
      expect(guardrailsEnabled(scope({ guardrails_enabled: on }), "dev")).toBe(
        true,
      );
    }
    for (const off of ["false", "0", "off", "no", " False ", false]) {
      expect(guardrailsEnabled(scope({ guardrails_enabled: off }), "dev")).toBe(
        false,
      );
    }
    expect(() =>
      guardrailsEnabled(scope({ guardrails_enabled: "maybe" }), "dev"),
    ).toThrow(/guardrails_enabled must be true or false/);
  });

  test("G2 prod refuses guardrails_enabled=false; prod with it on is fine", () => {
    expect(() =>
      guardrailsEnabled(scope({ guardrails_enabled: "false" }), "prod"),
    ).toThrow(/cannot be turned off in prod/);
    expect(
      guardrailsEnabled(scope({ guardrails_enabled: "true" }), "prod"),
    ).toBe(true);
  });

  test("G3 sources, default: enrichment and document screening get their guardrail", () => {
    const names = containerVarNames(sources());
    for (const v of GUARDRAIL_VARS) expect(names).toContain(v);
  });

  test("G4 sources, off: no guardrail variable on any container", () => {
    const names = containerVarNames(sources({ guardrails_enabled: "false" }));
    for (const v of GUARDRAIL_VARS) expect(names).not.toContain(v);
  });

  test("G5 ontology engine: guardrail by default, none when off", () => {
    expect(containerVarNames(ontology())).toContain("GUARDRAIL_SSM_PARAM");
    expect(
      containerVarNames(ontology({ guardrails_enabled: "false" })),
    ).not.toContain("GUARDRAIL_SSM_PARAM");
  });

  test("G6 serve: no override by default; SERVE_GUARDRAILS_DISABLED=true when off", () => {
    for (const vars of runtimeVars(serve())) {
      expect(vars).not.toHaveProperty("SERVE_GUARDRAILS_DISABLED");
    }
    const off = runtimeVars(serve({ guardrails_enabled: "false" }));
    expect(off.length).toBeGreaterThan(0);
    for (const vars of off) {
      expect(vars.SERVE_GUARDRAILS_DISABLED).toBe("true");
    }
  });
});
