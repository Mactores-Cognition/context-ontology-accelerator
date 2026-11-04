// Keys import (add-on) stack. Expected results: work/add-bulk-import.txt,
// section 3.6 (K1-K8).

import * as cdk from "aws-cdk-lib";
import * as lambda from "aws-cdk-lib/aws-lambda";
import { Match, Template } from "aws-cdk-lib/assertions";
import { NetworkStack } from "../../lib/stacks/foundation/network-stack";
import { KeysImportStack } from "../../lib/stacks/services/keys-import-stack";

// Mock bundlePython to avoid fingerprinting the entire repo root during tests.
jest.mock("../../lib/utils/python-bundling", () => ({
  bundlePython: () =>
    lambda.Code.fromInline("def handler(event, context): pass"),
}));

const TEST_ENV = { account: "123456789012", region: "us-east-1" };
const TEST_CONTEXT = { "aws:cdk:bundling-stacks": [] };
const API_HANDLER = "coa_sources.database.keys_import.api_handler.handler";
const WORKER_HANDLER =
  "coa_sources.database.keys_import.worker_handler.handler";

function synth(enabled: boolean): Template {
  const app = new cdk.App({ context: TEST_CONTEXT });
  const network = new NetworkStack(app, "TestNetwork", { env: TEST_ENV });
  return Template.fromStack(
    new KeysImportStack(app, `TestKeysImport${enabled ? "On" : "Off"}`, {
      vpc: network.vpc,
      lambdaSecurityGroup: network.lambdaSecurityGroup,
      allowedOrigin: "https://ui.example.com",
      enabled,
      env: TEST_ENV,
    }),
  );
}

/** Logical id of the Lambda function with this handler. */
function functionId(template: Template, handler: string): string {
  const fns = template.findResources("AWS::Lambda::Function", {
    Properties: { Handler: handler },
  });
  const ids = Object.keys(fns);
  expect(ids).toHaveLength(1);
  return ids[0];
}

/** Every IAM statement attached to the role of the function with this handler. */
function statementsFor(template: Template, handler: string): any[] {
  const fn = template.findResources("AWS::Lambda::Function")[
    functionId(template, handler)
  ];
  const roleId = fn.Properties.Role["Fn::GetAtt"][0];
  return Object.values(template.findResources("AWS::IAM::Policy"))
    .filter((p: any) =>
      (p.Properties.Roles ?? []).some((r: any) => r.Ref === roleId),
    )
    .flatMap((p: any) => p.Properties.PolicyDocument.Statement);
}

function actionsOf(statements: any[]): string[] {
  return statements.flatMap((s) => [].concat(s.Action));
}

describe("KeysImportStack (enabled)", () => {
  let template: Template;
  beforeAll(() => {
    template = synth(true);
  });

  test("K1 upload bucket: private, SSL only, encrypted, CORS for the UI, files expire after 30 days", () => {
    template.hasResourceProperties("AWS::S3::Bucket", {
      PublicAccessBlockConfiguration: {
        BlockPublicAcls: true,
        BlockPublicPolicy: true,
        IgnorePublicAcls: true,
        RestrictPublicBuckets: true,
      },
      BucketEncryption: Match.objectLike({
        ServerSideEncryptionConfiguration: [
          Match.objectLike({
            ServerSideEncryptionByDefault: { SSEAlgorithm: "AES256" },
          }),
        ],
      }),
      CorsConfiguration: {
        CorsRules: [
          Match.objectLike({
            AllowedOrigins: ["https://ui.example.com"],
            AllowedMethods: Match.arrayWith(["PUT"]),
          }),
        ],
      },
      LifecycleConfiguration: {
        Rules: Match.arrayWith([
          Match.objectLike({ ExpirationInDays: 30, Status: "Enabled" }),
        ]),
      },
    });
    template.hasResourceProperties("AWS::S3::BucketPolicy", {
      PolicyDocument: {
        Statement: Match.arrayWith([
          Match.objectLike({
            Effect: "Deny",
            Condition: { Bool: { "aws:SecureTransport": "false" } },
          }),
        ]),
      },
    });
  });

  test("K2 job table: PK/SK strings, on-demand, point-in-time recovery", () => {
    template.hasResourceProperties("AWS::DynamoDB::Table", {
      KeySchema: [
        { AttributeName: "PK", KeyType: "HASH" },
        { AttributeName: "SK", KeyType: "RANGE" },
      ],
      BillingMode: "PAY_PER_REQUEST",
      PointInTimeRecoverySpecification: { PointInTimeRecoveryEnabled: true },
    });
  });

  test("K3 queue: encrypted, visibility longer than the worker timeout, dead-letter queue after 3 deliveries", () => {
    template.resourceCountIs("AWS::SQS::Queue", 2);
    template.hasResourceProperties("AWS::SQS::Queue", {
      VisibilityTimeout: 1200,
      SqsManagedSseEnabled: true,
      RedrivePolicy: Match.objectLike({ maxReceiveCount: 3 }),
    });
  });

  test("K4 API Lambda: handler, settings, in the VPC", () => {
    template.hasResourceProperties("AWS::Lambda::Function", {
      Handler: API_HANDLER,
      Runtime: "python3.12",
      Timeout: 30,
      VpcConfig: Match.objectLike({ SubnetIds: Match.anyValue() }),
      Environment: {
        Variables: Match.objectLike({
          KEYS_IMPORT_ENABLED: "true",
          KEYS_IMPORT_BUCKET: Match.anyValue(),
          KEYS_IMPORT_JOBS_TABLE: Match.anyValue(),
          KEYS_IMPORT_QUEUE_URL: Match.anyValue(),
          SOURCES_TABLE: Match.objectLike({
            Ref: Match.stringLikeRegexp("SsmParameterValue.*sourcestablename"),
          }),
          NAMESPACES_TABLE: Match.anyValue(),
          SMUS_DOMAIN_ID: Match.anyValue(),
          PROJECT_ACCESS_ROLE_ARN: Match.anyValue(),
        }),
      },
    });
  });

  test("K5 worker Lambda: 15-minute timeout, one message at a time, partial batch failures, at most 2 at once", () => {
    template.hasResourceProperties("AWS::Lambda::Function", {
      Handler: WORKER_HANDLER,
      Timeout: 900,
      VpcConfig: Match.objectLike({ SubnetIds: Match.anyValue() }),
      Environment: {
        Variables: Match.objectLike({
          KEYS_IMPORT_BUCKET: Match.anyValue(),
          KEYS_IMPORT_JOBS_TABLE: Match.anyValue(),
          SOURCES_TABLE: Match.anyValue(),
          SMUS_DOMAIN_ID: Match.anyValue(),
        }),
      },
    });
    template.hasResourceProperties("AWS::Lambda::EventSourceMapping", {
      FunctionName: { Ref: functionId(template, WORKER_HANDLER) },
      BatchSize: 1,
      FunctionResponseTypes: ["ReportBatchItemFailures"],
      ScalingConfig: { MaximumConcurrency: 2 },
    });
  });

  test("K6 API Lambda permissions: its own bucket, table and queue; sources and namespaces read-only; DataZone through the project role", () => {
    const actions = actionsOf(statementsFor(template, API_HANDLER));
    expect(actions).toEqual(
      expect.arrayContaining([
        "s3:PutObject",
        "s3:GetObject*",
        "dynamodb:PutItem",
        "dynamodb:Query",
        "sqs:SendMessage",
        "sts:AssumeRole",
      ]),
    );
    // Never writes the existing sources or namespaces tables directly.
    const sourcesWrites = statementsFor(template, API_HANDLER).filter(
      (s) =>
        JSON.stringify(s.Resource).includes("sourcestablename") &&
        []
          .concat(s.Action)
          .some((a: string) => /Put|Update|Delete|Write/.test(a)),
    );
    expect(sourcesWrites).toEqual([]);
  });

  test("K6 worker permissions: read the CSV, write reports and jobs, consume the queue, DataZone through the project role", () => {
    const actions = actionsOf(statementsFor(template, WORKER_HANDLER));
    expect(actions).toEqual(
      expect.arrayContaining([
        "s3:GetObject*",
        "s3:PutObject",
        "dynamodb:UpdateItem",
        "sqs:ReceiveMessage",
        "sqs:DeleteMessage",
        "sts:AssumeRole",
      ]),
    );
    expect(actions).not.toContain("sqs:SendMessage");
  });

  test("K7 publishes the API Lambda ARN for the API stack", () => {
    template.hasResourceProperties("AWS::SSM::Parameter", {
      Name: "/coa/keys-import/api-fn-arn",
      Value: {
        "Fn::GetAtt": [functionId(template, API_HANDLER), "Arn"],
      },
    });
  });
});

describe("KeysImportStack (switched off)", () => {
  test("K8 only the API Lambda (answers 'not enabled') and its ARN; no bucket, table, queue or worker", () => {
    const template = synth(false);
    template.resourceCountIs("AWS::S3::Bucket", 0);
    template.resourceCountIs("AWS::DynamoDB::Table", 0);
    template.resourceCountIs("AWS::SQS::Queue", 0);
    template.resourceCountIs("AWS::Lambda::EventSourceMapping", 0);
    template.hasResourceProperties("AWS::Lambda::Function", {
      Handler: API_HANDLER,
      Environment: {
        Variables: Match.objectLike({ KEYS_IMPORT_ENABLED: "false" }),
      },
    });
    expect(
      Object.keys(
        template.findResources("AWS::Lambda::Function", {
          Properties: { Handler: WORKER_HANDLER },
        }),
      ),
    ).toEqual([]);
    template.hasResourceProperties("AWS::SSM::Parameter", {
      Name: "/coa/keys-import/api-fn-arn",
    });
  });
});

describe("KeysImportStack (CORS origin)", () => {
  // Found on the first deploy: coa_common.response.api_response raises without
  // ALLOWED_ORIGIN, so every API call answered "Internal server error". The worker
  // needs it too: it saves keys through the existing handler, which uses api_response.
  test("K9 every Lambda gets ALLOWED_ORIGIN (on and off)", () => {
    for (const enabled of [true, false]) {
      const template = synth(enabled);
      const fns = Object.values(
        template.findResources("AWS::Lambda::Function", {
          Properties: { Handler: Match.stringLikeRegexp("keys_import") },
        }),
      );
      expect(fns).toHaveLength(enabled ? 2 : 1);
      for (const fn of fns as any[]) {
        expect(fn.Properties.Environment.Variables.ALLOWED_ORIGIN).toBe(
          "https://ui.example.com",
        );
      }
    }
  });
});
