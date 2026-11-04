// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import * as cdk from "aws-cdk-lib";
import * as dynamodb from "aws-cdk-lib/aws-dynamodb";
import * as ec2 from "aws-cdk-lib/aws-ec2";
import * as iam from "aws-cdk-lib/aws-iam";
import * as lambda from "aws-cdk-lib/aws-lambda";
import * as lambdaEventSources from "aws-cdk-lib/aws-lambda-event-sources";
import * as s3 from "aws-cdk-lib/aws-s3";
import * as sqs from "aws-cdk-lib/aws-sqs";
import * as ssm from "aws-cdk-lib/aws-ssm";
import { Construct } from "constructs";
import { TABLE_NAMES } from "@coa/shared";
import { AccessLogsBucket, DynamoDBTable } from "../../constructs";
import { SCLStack } from "../../constructs/scl-stack";
import {
  isDynamoDbMetadataStore,
  METADATA_STORE_DYNAMODB,
  resolveContext,
} from "../../context";
import { Paths, fromRoot } from "../../paths";
import { bundlePython } from "../../utils/python-bundling";

export interface KeysImportStackProps extends cdk.StackProps {
  /** VPC for Lambda placement (private subnets with egress). */
  readonly vpc: ec2.IVpc;
  /** Security group shared by the service Lambdas. */
  readonly lambdaSecurityGroup: ec2.ISecurityGroup;
  /** Web app origin allowed to PUT the CSV to the upload bucket (CORS). */
  readonly allowedOrigin?: string;
  /**
   * When false, only the API Lambda is deployed and every route answers 404
   * "not enabled"; no bucket, table, queue or worker. The routes stay wired so
   * the API stack's 501-stub guard holds.
   */
  readonly enabled: boolean;
}

/**
 * Keys import (add-on): add primary keys and foreign keys to a DATABASE
 * source's tables from a CSV file. Plan: work/add-bulk-import.txt, section 2.
 *
 * A separate stack so the feature can be removed cleanly. It reads shared
 * values through SSM (like MetricServiceStack) and saves keys through the
 * existing sources code, bundled into its own Lambdas:
 *
 *   - Upload bucket (CSV files and error reports; 30-day expiry)
 *   - Job table (one item per run)
 *   - Queue + dead-letter queue (one message per run)
 *   - API Lambda (5 routes, keys-import.smithy) and worker Lambda (SQS)
 *
 * Publishes `/{prefix}/keys-import/api-fn-arn` for the API stack.
 */
export class KeysImportStack extends SCLStack {
  public readonly apiFn: lambda.Function;

  constructor(scope: Construct, id: string, props: KeysImportStackProps) {
    super(scope, id, props);
    const { ssmPrefix } = resolveContext(this.node);

    // ── Shared values via SSM (published by the sources and namespace stacks) ──
    const sourcesTableName = ssm.StringParameter.valueForStringParameter(
      this,
      `${ssmPrefix}/sources/sources-table-name`,
    );
    const namespacesTableName = ssm.StringParameter.valueForStringParameter(
      this,
      `${ssmPrefix}/namespace/namespaces-table-name`,
    );
    const sourcesTable = dynamodb.Table.fromTableName(
      this,
      "SourcesTable",
      sourcesTableName,
    );
    const namespacesTable = dynamodb.Table.fromTableName(
      this,
      "NamespacesTable",
      namespacesTableName,
    );

    // Table-metadata store (as SourcesStack / MetricServiceStack). DataZone:
    // the project access role, which trusts the account, is assumed for
    // reads and asset revisions. DynamoDB: the metadata-assets table.
    let metadataStoreEnv: Record<string, string>;
    let grantMetadataStore: (fn: lambda.Function) => void;
    if (isDynamoDbMetadataStore(this.node)) {
      const metadataAssetsTable = dynamodb.Table.fromTableAttributes(
        this,
        "MetadataAssetsTable",
        {
          tableName: this.prefixed(TABLE_NAMES.METADATA_ASSETS),
          globalIndexes: ["AssetById"],
        },
      );
      metadataStoreEnv = {
        METADATA_STORE_BACKEND: METADATA_STORE_DYNAMODB,
        METADATA_STORE_TABLE: metadataAssetsTable.tableName,
      };
      grantMetadataStore = (fn) => metadataAssetsTable.grantReadWriteData(fn);
    } else {
      const domainId = ssm.StringParameter.valueForStringParameter(
        this,
        `${ssmPrefix}/smus/domain-id`,
      );
      const projectAccessRoleArn = ssm.StringParameter.valueForStringParameter(
        this,
        `${ssmPrefix}/smus/dz-project-access-role-arn`,
      );
      metadataStoreEnv = {
        SMUS_DOMAIN_ID: domainId,
        PROJECT_ACCESS_ROLE_ARN: projectAccessRoleArn,
      };
      grantMetadataStore = (fn) => {
        fn.addToRolePolicy(
          new iam.PolicyStatement({
            sid: "AssumeDataZoneProjectAccessRole",
            actions: ["sts:AssumeRole"],
            resources: [projectAccessRoleArn],
          }),
        );
        // Same read-only DataZone calls the sources API makes with its own role.
        fn.addToRolePolicy(
          new iam.PolicyStatement({
            sid: "DataZoneRead",
            actions: ["datazone:Search", "datazone:GetAsset"],
            resources: [
              `arn:aws:datazone:${this.region}:${this.account}:domain/${domainId}`,
              `arn:aws:datazone:${this.region}:${this.account}:domain/${domainId}/*`,
            ],
          }),
        );
      };
    }

    const code = bundlePython({
      srcDirs: [
        fromRoot("packages/sources/src"),
        Paths.commonLib,
        Paths.smithyGeneratedControlPlanePythonServer,
      ],
      requirementsFile: fromRoot("packages/sources/requirements.txt"),
      architecture: "arm64",
    });
    // coa_common.response.api_response needs it for every HTTP answer (the worker
    // too: it saves keys through the existing handler, which uses api_response).
    const allowedOrigin = props.allowedOrigin ?? "*";
    const sharedEnv = {
      ALLOWED_ORIGIN: allowedOrigin,
      SOURCES_TABLE: sourcesTableName,
      NAMESPACES_TABLE: namespacesTableName,
      RESOURCE_PREFIX: this.prefixed(""),
      ...metadataStoreEnv,
    };
    const lambdaPlacement = {
      vpc: props.vpc,
      vpcSubnets: { subnetType: ec2.SubnetType.PRIVATE_WITH_EGRESS },
      securityGroups: [props.lambdaSecurityGroup],
    };

    if (!props.enabled) {
      this.apiFn = new lambda.Function(this, "KeysImportApiFn", {
        functionName: this.prefixed("keys-import-api"),
        runtime: lambda.Runtime.PYTHON_3_12,
        architecture: lambda.Architecture.ARM_64,
        handler: "coa_sources.database.keys_import.api_handler.handler",
        code,
        timeout: cdk.Duration.seconds(30),
        memorySize: 256,
        ...lambdaPlacement,
        environment: {
          ALLOWED_ORIGIN: allowedOrigin,
          KEYS_IMPORT_ENABLED: "false",
        },
      });
      this.publishApiArn(ssmPrefix);
      return;
    }

    // ── Upload bucket (CSV files and error reports) ─────────────────────
    const accessLogs = new AccessLogsBucket(this, "KeysImportAccessLogs", {
      nameSuffix: "keys-import-logs",
    });
    const bucket = new s3.Bucket(this, "KeysImportBucket", {
      bucketName: cdk.Lazy.string({
        produce: () => `${this.prefixed("keys-import")}-${this.account}`,
      }),
      encryption: s3.BucketEncryption.S3_MANAGED,
      serverAccessLogsBucket: accessLogs.bucket,
      serverAccessLogsPrefix: "keys-import/",
      blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
      enforceSSL: true,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
      autoDeleteObjects: true,
      cors: [
        {
          allowedOrigins: [allowedOrigin],
          allowedMethods: [s3.HttpMethods.PUT, s3.HttpMethods.GET],
          allowedHeaders: ["Content-Type"],
          maxAge: 3600,
        },
      ],
      lifecycleRules: [{ expiration: cdk.Duration.days(30) }],
    });

    // ── Job table (one item per run; PK = SRC#{sourceId}, SK = job id) ──
    const jobsTable = new DynamoDBTable(this, "KeysImportJobsTable", {
      tableName: "keys-import-jobs",
      partitionKey: { name: "PK", type: dynamodb.AttributeType.STRING },
      sortKey: { name: "SK", type: dynamodb.AttributeType.STRING },
    }).table;

    // ── Queue (one message per run) ─────────────────────────────────────
    const workerTimeout = cdk.Duration.minutes(15);
    const dlq = new sqs.Queue(this, "KeysImportDLQ", {
      queueName: this.prefixed("keys-import-dlq"),
      retentionPeriod: cdk.Duration.days(14),
      encryption: sqs.QueueEncryption.SQS_MANAGED,
      enforceSSL: true,
    });
    const queue = new sqs.Queue(this, "KeysImportQueue", {
      queueName: this.prefixed("keys-import-queue"),
      // Longer than the worker timeout, or SQS redelivers a run still in progress.
      visibilityTimeout: cdk.Duration.minutes(20),
      encryption: sqs.QueueEncryption.SQS_MANAGED,
      enforceSSL: true,
      // Matches worker_handler.MAX_ATTEMPTS.
      deadLetterQueue: { maxReceiveCount: 3, queue: dlq },
    });

    const featureEnv = {
      KEYS_IMPORT_ENABLED: "true",
      KEYS_IMPORT_BUCKET: bucket.bucketName,
      KEYS_IMPORT_JOBS_TABLE: jobsTable.tableName,
      KEYS_IMPORT_QUEUE_URL: queue.queueUrl,
    };

    // ── API Lambda (the 5 keys-import routes) ───────────────────────────
    this.apiFn = new lambda.Function(this, "KeysImportApiFn", {
      functionName: this.prefixed("keys-import-api"),
      runtime: lambda.Runtime.PYTHON_3_12,
      architecture: lambda.Architecture.ARM_64,
      handler: "coa_sources.database.keys_import.api_handler.handler",
      code,
      timeout: cdk.Duration.seconds(30),
      memorySize: 512,
      ...lambdaPlacement,
      environment: { ...sharedEnv, ...featureEnv },
    });
    bucket.grantReadWrite(this.apiFn);
    jobsTable.grantReadWriteData(this.apiFn);
    queue.grantSendMessages(this.apiFn);
    sourcesTable.grantReadData(this.apiFn);
    namespacesTable.grantReadData(this.apiFn);
    grantMetadataStore(this.apiFn);

    // ── Worker Lambda (one run per message) ─────────────────────────────
    const workerFn = new lambda.Function(this, "KeysImportWorkerFn", {
      functionName: this.prefixed("keys-import-worker"),
      runtime: lambda.Runtime.PYTHON_3_12,
      architecture: lambda.Architecture.ARM_64,
      handler: "coa_sources.database.keys_import.worker_handler.handler",
      code,
      timeout: workerTimeout,
      memorySize: 1024,
      ...lambdaPlacement,
      environment: { ...sharedEnv, ...featureEnv },
    });
    workerFn.addEventSource(
      new lambdaEventSources.SqsEventSource(queue, {
        batchSize: 1,
        reportBatchItemFailures: true,
        // Two runs at most at once, so DataZone is not flooded.
        maxConcurrency: 2,
      }),
    );
    bucket.grantReadWrite(workerFn);
    jobsTable.grantReadWriteData(workerFn);
    sourcesTable.grantReadData(workerFn);
    namespacesTable.grantReadData(workerFn);
    grantMetadataStore(workerFn);

    this.publishApiArn(ssmPrefix);
  }

  private publishApiArn(ssmPrefix: string): void {
    new ssm.StringParameter(this, "SsmKeysImportApiFnArn", {
      parameterName: `${ssmPrefix}/keys-import/api-fn-arn`,
      stringValue: this.apiFn.functionArn,
    });
  }
}
