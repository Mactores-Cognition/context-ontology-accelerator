// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import * as cdk from "aws-cdk-lib";
import * as dynamodb from "aws-cdk-lib/aws-dynamodb";
import * as ec2 from "aws-cdk-lib/aws-ec2";
import * as lambda from "aws-cdk-lib/aws-lambda";
import { Template, Match } from "aws-cdk-lib/assertions";
import { NamespaceStack } from "../../lib/stacks/services";
import { DEFAULT_RESOURCE_PREFIX, DEFAULT_ENV } from "../../lib/constants";

// Mock bundlePython to avoid running pip install and hashing the monorepo root.
jest.mock("../../lib/utils/python-bundling", () => ({
  bundlePython: () =>
    lambda.Code.fromInline("def handler(event, context): pass"),
}));

const TEST_CONTEXT = {
  resource_prefix: DEFAULT_RESOURCE_PREFIX,
  env: DEFAULT_ENV,
  "aws:cdk:bundling-stacks": [],
};

describe("NamespaceStack", () => {
  let template: Template;

  beforeAll(() => {
    const app = new cdk.App({ context: TEST_CONTEXT });

    // Provide VPC and DynamoDB tables from a dependency stack
    const depStack = new cdk.Stack(app, "DepStack");
    const vpc = new ec2.Vpc(depStack, "Vpc");
    const lambdaSecurityGroup = new ec2.SecurityGroup(depStack, "LambdaSG", {
      vpc,
      allowAllOutbound: true,
    });
    const rolesTable = new dynamodb.Table(depStack, "Roles", {
      partitionKey: { name: "PK", type: dynamodb.AttributeType.STRING },
    });
    const rrmTable = new dynamodb.Table(depStack, "RRM", {
      partitionKey: { name: "PK", type: dynamodb.AttributeType.STRING },
    });

    template = Template.fromStack(
      new NamespaceStack(app, "TestNamespace", {
        vpc,
        lambdaSecurityGroup,
        rolesTable,
        resourceRoleMappingsTable: rrmTable,
      }),
    );
  });

  // Build a fresh NamespaceStack with extra context and return the smus-login
  // role's AssumeRolePolicyDocument as a JSON string for trust-principal checks.
  function loginRoleTrustJson(
    id: string,
    extraContext: Record<string, string>,
  ): string {
    const app = new cdk.App({ context: { ...TEST_CONTEXT, ...extraContext } });
    const depStack = new cdk.Stack(app, `${id}Dep`);
    const vpc = new ec2.Vpc(depStack, "Vpc");
    const lambdaSecurityGroup = new ec2.SecurityGroup(depStack, "LambdaSG", {
      vpc,
      allowAllOutbound: true,
    });
    const rolesTable = new dynamodb.Table(depStack, "Roles", {
      partitionKey: { name: "PK", type: dynamodb.AttributeType.STRING },
    });
    const rrmTable = new dynamodb.Table(depStack, "RRM", {
      partitionKey: { name: "PK", type: dynamodb.AttributeType.STRING },
    });
    const tpl = Template.fromStack(
      new NamespaceStack(app, id, {
        vpc,
        lambdaSecurityGroup,
        rolesTable,
        resourceRoleMappingsTable: rrmTable,
      }),
    );
    const role = Object.values(
      tpl.findResources("AWS::IAM::Role", {
        Properties: {
          RoleName: `${DEFAULT_RESOURCE_PREFIX}-${DEFAULT_ENV}-smus-login`,
        },
      }),
    )[0] as any;
    return JSON.stringify(role.Properties.AssumeRolePolicyDocument);
  }

  test("creates Namespaces table with PK/SK", () => {
    template.hasResourceProperties("AWS::DynamoDB::Table", {
      TableName: Match.stringLikeRegexp("namespaces$"),
      KeySchema: Match.arrayWith([
        { AttributeName: "PK", KeyType: "HASH" },
        { AttributeName: "SK", KeyType: "RANGE" },
      ]),
    });
  });

  test("Namespaces table has NameByIndex GSI", () => {
    template.hasResourceProperties("AWS::DynamoDB::Table", {
      TableName: Match.stringLikeRegexp("namespaces$"),
      GlobalSecondaryIndexes: Match.arrayWith([
        Match.objectLike({
          IndexName: "NameByIndex",
          KeySchema: [{ AttributeName: "name", KeyType: "HASH" }],
          Projection: { ProjectionType: "ALL" },
        }),
      ]),
    });
  });

  test("creates DataZone domain with IAM auth (no SSO)", () => {
    template.hasResourceProperties("AWS::DataZone::Domain", {
      Name: `${DEFAULT_RESOURCE_PREFIX}-${DEFAULT_ENV}-smus-catalog`,
      DomainVersion: "V2",
    });
    // Verify no SingleSignOn property is set (IAM-based)
    const domains = template.findResources("AWS::DataZone::Domain");
    const domainKey = Object.keys(domains)[0];
    expect(domains[domainKey].Properties).not.toHaveProperty("SingleSignOn");
  });

  // ── DataZone teardown ─────────────────────────────────────

  test("every native DataZone L1 resource has RemovalPolicy.RETAIN", () => {
    // DataZone rejects deleting these directly via CFN: a FormType must
    // be DISABLED before DeleteFormType succeeds; a domain's native
    // DeleteDomain has no force/cascade option and fails outright with
    // "Domain cannot be deleted because there are existing projects
    // under this domain" — and a namespace-created project always
    // exists under this domain while any namespace does, so that's not
    // an edge case (confirmed against a real stuck stack — see PR
    // discussion). The native CfnDomain/CfnFormType/CfnProject/... L1
    // resources have no delete hook to handle any of this, so cdk
    // destroy fails and leaves the stack DELETE_FAILED. RETAIN means CFN
    // never attempts to delete these itself; destroy.sh instead calls
    // DataZone's own delete-domain --skip-deletion-check directly, which
    // does support a full cascading force-delete at the domain level
    // (unlike DeleteProject's cascade, which stops at cross-project
    // asset-type references). This does not apply to CoaRelationalTable
    // (AWS::DataZone::AssetType has no native CFN resource at all, so it
    // must remain a custom resource regardless).
    const RETAINED_TYPES = [
      "AWS::DataZone::Domain",
      "AWS::DataZone::UserProfile",
      "AWS::DataZone::ProjectProfile",
      "AWS::DataZone::PolicyGrant",
      "AWS::DataZone::Project",
      "AWS::DataZone::FormType",
      "AWS::DataZone::ProjectMembership",
      "AWS::DataZone::Owner",
    ];
    for (const type of RETAINED_TYPES) {
      const resources = template.findResources(type);
      const ids = Object.keys(resources);
      expect(ids.length).toBeGreaterThan(0);
      for (const id of ids) {
        expect(resources[id].DeletionPolicy).toBe("Retain");
        expect(resources[id].UpdateReplacePolicy).toBe("Retain");
      }
    }
  });

  test("creates domain execution role with DataZone service principal", () => {
    template.hasResourceProperties("AWS::IAM::Role", {
      RoleName: `${DEFAULT_RESOURCE_PREFIX}-${DEFAULT_ENV}-smus-exec`,
      AssumeRolePolicyDocument: {
        Statement: Match.arrayWith([
          Match.objectLike({
            Effect: "Allow",
            Principal: { Service: "datazone.amazonaws.com" },
          }),
        ]),
      },
    });
  });

  test("trust policy includes sts:SetContext with source account condition", () => {
    template.hasResourceProperties("AWS::IAM::Role", {
      RoleName: `${DEFAULT_RESOURCE_PREFIX}-${DEFAULT_ENV}-smus-exec`,
      AssumeRolePolicyDocument: {
        Statement: Match.arrayWith([
          Match.objectLike({
            Action: Match.arrayWith(["sts:AssumeRole", "sts:SetContext"]),
            Condition: {
              StringEquals: { "aws:SourceAccount": Match.anyValue() },
            },
          }),
        ]),
      },
    });
  });

  test("domain execution role uses scoped custom policy, not permissive AWS-managed policy", () => {
    // The role must NOT have SageMakerStudioAdminIAMPermissiveExecutionPolicy,
    // which grants overly broad iam:* and s3:* wildcards (CWE-269).
    const executionRole = Object.values(
      template.findResources("AWS::IAM::Role", {
        Properties: {
          RoleName: `${DEFAULT_RESOURCE_PREFIX}-${DEFAULT_ENV}-smus-exec`,
        },
      }),
    )[0] as any;

    const managedPolicies = executionRole.Properties.ManagedPolicyArns || [];
    const managedPolicyNames = managedPolicies
      .map((arn: any) => {
        if (typeof arn === "string") return arn;
        if (arn["Fn::Join"]) {
          const parts = arn["Fn::Join"][1];
          return parts[parts.length - 1];
        }
        return "";
      })
      .filter(Boolean);

    expect(managedPolicyNames).not.toContain(
      "SageMakerStudioAdminIAMPermissiveExecutionPolicy",
    );

    // Verify a custom managed policy is attached instead
    template.hasResourceProperties("AWS::IAM::ManagedPolicy", {
      ManagedPolicyName: `${DEFAULT_RESOURCE_PREFIX}-${DEFAULT_ENV}-smus-exec-policy`,
    });
  });

  test("domain execution role custom policy has scoped S3 access, not s3:*", () => {
    // S3 permissions must be scoped to deployment prefix, not wildcard.
    const policy = Object.values(
      template.findResources("AWS::IAM::ManagedPolicy", {
        Properties: {
          ManagedPolicyName: `${DEFAULT_RESOURCE_PREFIX}-${DEFAULT_ENV}-smus-exec-policy`,
        },
      }),
    )[0] as any;

    expect(policy).toBeDefined();
    const s3Statement = policy.Properties.PolicyDocument.Statement.find(
      (stmt: any) =>
        Array.isArray(stmt.Action) && stmt.Action.includes("s3:GetObject"),
    );
    expect(s3Statement).toBeDefined();
    // Verify resources are scoped to prefix, not wildcard
    expect(s3Statement.Resource).toContain(
      `arn:aws:s3:::${DEFAULT_RESOURCE_PREFIX}-${DEFAULT_ENV}-*`,
    );
    expect(s3Statement.Resource).not.toContain("arn:aws:s3:::*");
  });

  test("domain execution role custom policy has scoped IAM access, not iam:*", () => {
    // IAM permissions must be scoped to roles with deployment prefix, not wildcard.
    const policy = Object.values(
      template.findResources("AWS::IAM::ManagedPolicy", {
        Properties: {
          ManagedPolicyName: `${DEFAULT_RESOURCE_PREFIX}-${DEFAULT_ENV}-smus-exec-policy`,
        },
      }),
    )[0] as any;

    expect(policy).toBeDefined();
    const iamStatement = policy.Properties.PolicyDocument.Statement.find(
      (stmt: any) =>
        Array.isArray(stmt.Action) && stmt.Action.includes("iam:GetRole"),
    );
    expect(iamStatement).toBeDefined();
    // Verify resource scoped to prefixed roles, not all roles
    const resourceStr = JSON.stringify(iamStatement.Resource);
    expect(resourceStr).toContain(
      `:role/${DEFAULT_RESOURCE_PREFIX}-${DEFAULT_ENV}-*`,
    );
    expect(resourceStr).not.toContain(":role/*");
  });

  test("domain execution role custom policy has scoped SageMaker resources", () => {
    // SageMaker operations must be scoped to specific resource types, not wildcard.
    const policy = Object.values(
      template.findResources("AWS::IAM::ManagedPolicy", {
        Properties: {
          ManagedPolicyName: `${DEFAULT_RESOURCE_PREFIX}-${DEFAULT_ENV}-smus-exec-policy`,
        },
      }),
    )[0] as any;

    expect(policy).toBeDefined();
    const sagemakerStatement = policy.Properties.PolicyDocument.Statement.find(
      (stmt: any) =>
        Array.isArray(stmt.Action) &&
        stmt.Action.includes("sagemaker:CreateDomain"),
    );
    expect(sagemakerStatement).toBeDefined();
    // Verify resources are scoped to specific types, not wildcard
    const resourceStr = JSON.stringify(sagemakerStatement.Resource);
    expect(resourceStr).toContain(":domain/*");
    expect(resourceStr).toContain(":user-profile/*");
    expect(resourceStr).toContain(":space/*");
  });

  test("domain login role has PassExecutionRole inline policy", () => {
    template.hasResourceProperties("AWS::IAM::Role", {
      Policies: Match.arrayWith([
        Match.objectLike({
          PolicyName: "PassExecutionRole",
        }),
      ]),
    });
  });

  test("domain login role trusts a specific admin role, not account root", () => {
    const loginRole = Object.values(
      template.findResources("AWS::IAM::Role", {
        Properties: {
          RoleName: `${DEFAULT_RESOURCE_PREFIX}-${DEFAULT_ENV}-smus-login`,
        },
      }),
    )[0] as any;
    const trustJson = JSON.stringify(
      loginRole.Properties.AssumeRolePolicyDocument,
    );
    // Trust targets the specific admin federation role...
    expect(trustJson).toContain(":role/Admin");
    // ...and NOT the whole account root.
    expect(trustJson).not.toContain(":root");
  });

  test("domain login role has NO MFA deny by default", () => {
    const loginRole = Object.values(
      template.findResources("AWS::IAM::Role", {
        Properties: {
          RoleName: `${DEFAULT_RESOURCE_PREFIX}-${DEFAULT_ENV}-smus-login`,
        },
      }),
    )[0] as any;
    const trustJson = JSON.stringify(
      loginRole.Properties.AssumeRolePolicyDocument,
    );
    // Default (no smus_require_mfa): some federated sessions report
    // mfaAuthenticated=false, so a BoolIfExists MFA deny would lock admins out.
    expect(trustJson).not.toContain("MultiFactorAuthPresent");
  });

  test("domain login role adds an MFA deny when smus_require_mfa is set", () => {
    const app = new cdk.App({
      context: { ...TEST_CONTEXT, smus_require_mfa: "true" },
    });
    const depStack = new cdk.Stack(app, "DepStack2");
    const vpc = new ec2.Vpc(depStack, "Vpc");
    const lambdaSecurityGroup = new ec2.SecurityGroup(depStack, "LambdaSG", {
      vpc,
      allowAllOutbound: true,
    });
    const rolesTable = new dynamodb.Table(depStack, "Roles", {
      partitionKey: { name: "PK", type: dynamodb.AttributeType.STRING },
    });
    const rrmTable = new dynamodb.Table(depStack, "RRM", {
      partitionKey: { name: "PK", type: dynamodb.AttributeType.STRING },
    });
    const mfaTemplate = Template.fromStack(
      new NamespaceStack(app, "TestNamespaceMfa", {
        vpc,
        lambdaSecurityGroup,
        rolesTable,
        resourceRoleMappingsTable: rrmTable,
      }),
    );
    mfaTemplate.hasResourceProperties("AWS::IAM::Role", {
      RoleName: `${DEFAULT_RESOURCE_PREFIX}-${DEFAULT_ENV}-smus-login`,
      AssumeRolePolicyDocument: {
        Statement: Match.arrayWith([
          Match.objectLike({
            Sid: "DenyAssumeWithoutMFA",
            Effect: "Deny",
            Action: "sts:AssumeRole",
            Condition: {
              BoolIfExists: { "aws:MultiFactorAuthPresent": "false" },
            },
          }),
        ]),
      },
    });
  });

  describe("smus_admin_principal_arns parsing", () => {
    const A = "arn:aws:iam::111122223333:role/AdminA";
    const B = "arn:aws:iam::111122223333:role/AdminB";

    test("uses multiple comma-separated ARNs", () => {
      const trust = loginRoleTrustJson("MultiArn", {
        smus_admin_principal_arns: `${A},${B}`,
      });
      expect(trust).toContain(A);
      expect(trust).toContain(B);
      expect(trust).not.toContain(":root");
    });

    test("trims leading/trailing whitespace around ARNs", () => {
      const trust = loginRoleTrustJson("WhitespaceArn", {
        smus_admin_principal_arns: `  ${A} ,  ${B}  `,
      });
      // Trimmed ARNs are present; no space-prefixed variant leaks through.
      expect(trust).toContain(A);
      expect(trust).toContain(B);
      expect(trust).not.toContain(` ${A}`);
    });

    test("drops empty segments from mixed valid/empty input", () => {
      const trust = loginRoleTrustJson("MixedArn", {
        smus_admin_principal_arns: `${A},,${B}, `,
      });
      expect(trust).toContain(A);
      expect(trust).toContain(B);
      // No empty principal artifact.
      expect(trust).not.toContain('"AWS":[""]');
      expect(trust).not.toContain('""');
    });

    test("falls back to the default Admin role when value is whitespace-only", () => {
      const trust = loginRoleTrustJson("WhitespaceOnly", {
        smus_admin_principal_arns: "   ",
      });
      expect(trust).toContain(":role/Admin");
      expect(trust).not.toContain(":root");
    });

    test("falls back to the default Admin role when value is commas-only", () => {
      const trust = loginRoleTrustJson("CommasOnly", {
        smus_admin_principal_arns: ",, ,",
      });
      expect(trust).toContain(":role/Admin");
      expect(trust).not.toContain(":root");
    });

    test("rejects a wildcard principal (over-broad trust)", () => {
      expect(() =>
        loginRoleTrustJson("WildcardArn", {
          smus_admin_principal_arns: "*",
        }),
      ).toThrow(/invalid IAM role\/user ARNs/);
    });

    test("rejects a malformed ARN string", () => {
      expect(() =>
        loginRoleTrustJson("MalformedArn", {
          smus_admin_principal_arns: "not-an-arn",
        }),
      ).toThrow(/invalid IAM role\/user ARNs/);
    });

    test("accepts a valid IAM user ARN (not just roles)", () => {
      const userArn = "arn:aws:iam::111122223333:user/break-glass-admin";
      const trust = loginRoleTrustJson("UserArn", {
        smus_admin_principal_arns: userArn,
      });
      expect(trust).toContain(userArn);
    });
  });

  test("creates SSM parameter for domain ID", () => {
    template.hasResourceProperties("AWS::SSM::Parameter", {
      Name: `/${DEFAULT_RESOURCE_PREFIX}/smus/domain-id`,
    });
  });

  test("creates Create Namespace Lambda in VPC with private subnets", () => {
    template.hasResourceProperties("AWS::Lambda::Function", {
      FunctionName: `${DEFAULT_RESOURCE_PREFIX}-${DEFAULT_ENV}-namespace-api`,
      Runtime: "python3.12",
      Timeout: 15,
      MemorySize: 256,
      VpcConfig: {
        SubnetIds: Match.anyValue(),
        SecurityGroupIds: Match.anyValue(),
      },
      Environment: {
        Variables: Match.objectLike({
          NAMESPACES_TABLE: Match.anyValue(),
          ROLES_TABLE: Match.anyValue(),
          RESOURCE_ROLE_MAPPINGS_TABLE: Match.anyValue(),
          DATAZONE_DOMAIN_ID: Match.anyValue(),
        }),
      },
    });
  });

  test("Lambda has least-privilege DataZone CreateProject permission", () => {
    template.hasResourceProperties("AWS::IAM::Policy", {
      PolicyDocument: {
        Statement: Match.arrayWith([
          Match.objectLike({
            Action: Match.arrayWith(["datazone:CreateProject"]),
            Effect: "Allow",
          }),
        ]),
      },
    });
  });

  test("does not create the legacy datazone-project-access role", () => {
    const roles = template.findResources("AWS::IAM::Role");
    const legacy = Object.values(roles).find(
      (r) =>
        r.Properties?.RoleName ===
        `${DEFAULT_RESOURCE_PREFIX}-${DEFAULT_ENV}-datazone-project-access`,
    );
    expect(legacy).toBeUndefined();
  });

  test("does not publish the legacy project-access-role-arn SSM param", () => {
    const params = template.findResources("AWS::SSM::Parameter");
    const legacy = Object.values(params).find(
      (p) =>
        p.Properties?.Name ===
        `/${DEFAULT_RESOURCE_PREFIX}/smus/project-access-role-arn`,
    );
    expect(legacy).toBeUndefined();
  });

  test("publishes the new dz-project-access-role-arn SSM param", () => {
    template.hasResourceProperties("AWS::SSM::Parameter", {
      Name: `/${DEFAULT_RESOURCE_PREFIX}/smus/dz-project-access-role-arn`,
    });
  });

  test("NamespaceApiFn is granted read-only Query on the sources table ByNamespace GSI for the active-sources precondition check", () => {
    template.hasResourceProperties("AWS::IAM::Policy", {
      PolicyDocument: {
        Statement: Match.arrayWith([
          Match.objectLike({
            Action: "dynamodb:Query",
            Resource: Match.arrayWith([
              {
                "Fn::Join": [
                  "",
                  Match.arrayWith([
                    Match.stringLikeRegexp("sources/index/ByNamespace"),
                  ]),
                ],
              },
            ]),
          }),
        ]),
      },
      PolicyName: Match.stringLikeRegexp("NamespaceApiFn"),
    });
  });

  test("NamespaceApiFn has SOURCES_TABLE in its environment", () => {
    template.hasResourceProperties("AWS::Lambda::Function", {
      FunctionName: Match.stringLikeRegexp("namespace-api$"),
      Environment: {
        Variables: Match.objectLike({
          SOURCES_TABLE: Match.anyValue(),
        }),
      },
    });
  });

  // When a VKG cluster is wired in, GetNamespace resolves VKG health from ECS
  // (resolve_vkg_health), which needs both VKG_CLUSTER_ARN and a scoped
  // ecs:DescribeServices grant. Without these, health resolves to UNKNOWN for
  // every namespace.
  describe("with vkgClusterArn", () => {
    const vkgClusterArn =
      "arn:aws:ecs:us-east-1:123456789012:cluster/coa-test-vkg";
    let vkgTemplate: Template;

    beforeAll(() => {
      const app = new cdk.App({ context: TEST_CONTEXT });
      const depStack = new cdk.Stack(app, "VkgDepStack");
      const vpc = new ec2.Vpc(depStack, "Vpc");
      const lambdaSecurityGroup = new ec2.SecurityGroup(depStack, "LambdaSG", {
        vpc,
        allowAllOutbound: true,
      });
      const rolesTable = new dynamodb.Table(depStack, "Roles", {
        partitionKey: { name: "PK", type: dynamodb.AttributeType.STRING },
      });
      const rrmTable = new dynamodb.Table(depStack, "RRM", {
        partitionKey: { name: "PK", type: dynamodb.AttributeType.STRING },
      });
      vkgTemplate = Template.fromStack(
        new NamespaceStack(app, "TestNamespaceVkg", {
          vpc,
          lambdaSecurityGroup,
          rolesTable,
          resourceRoleMappingsTable: rrmTable,
          vkgClusterArn,
        }),
      );
    });

    test("NamespaceApiFn has VKG_CLUSTER_ARN in its environment", () => {
      vkgTemplate.hasResourceProperties("AWS::Lambda::Function", {
        FunctionName: Match.stringLikeRegexp("namespace-api$"),
        Environment: {
          Variables: Match.objectLike({
            VKG_CLUSTER_ARN: vkgClusterArn,
          }),
        },
      });
    });

    test("NamespaceApiFn is granted ecs read actions scoped to the VKG cluster", () => {
      vkgTemplate.hasResourceProperties("AWS::IAM::Policy", {
        PolicyDocument: {
          Statement: Match.arrayWith([
            Match.objectLike({
              Action: [
                "ecs:DescribeServices",
                "ecs:ListTasks",
                "ecs:DescribeTasks",
              ],
              Effect: "Allow",
              Resource: "*",
              Condition: {
                ArnLike: { "ecs:cluster": vkgClusterArn },
              },
            }),
          ]),
        },
        PolicyName: Match.stringLikeRegexp("NamespaceApiFn"),
      });
    });
  });

  test("NamespaceApiFn omits VKG_CLUSTER_ARN when no cluster is wired in", () => {
    const fn = Object.values(
      template.findResources("AWS::Lambda::Function", {
        Properties: {
          FunctionName: `${DEFAULT_RESOURCE_PREFIX}-${DEFAULT_ENV}-namespace-api`,
        },
      }),
    )[0] as any;
    expect(fn.Properties.Environment.Variables).not.toHaveProperty(
      "VKG_CLUSTER_ARN",
    );
  });
});

// DynamoDB metadata store (metadata_store=dynamodb). The DataZone domain and
// everything around it is replaced by one DynamoDB table holding table/column
// metadata. The mode is opt-in through CDK context, so every test above keeps
// running in the default (DataZone) mode unchanged.
describe("NamespaceStack in DynamoDB mode (metadata_store=dynamodb)", () => {
  const PREFIX = `${DEFAULT_RESOURCE_PREFIX}-${DEFAULT_ENV}`;
  const ASSET_TABLE = `${PREFIX}-metadata-assets`;
  let template: Template;

  beforeAll(() => {
    const app = new cdk.App({
      context: { ...TEST_CONTEXT, metadata_store: "dynamodb" },
    });
    const depStack = new cdk.Stack(app, "DdbDepStack");
    const vpc = new ec2.Vpc(depStack, "Vpc");
    const lambdaSecurityGroup = new ec2.SecurityGroup(depStack, "LambdaSG", {
      vpc,
      allowAllOutbound: true,
    });
    const rolesTable = new dynamodb.Table(depStack, "Roles", {
      partitionKey: { name: "PK", type: dynamodb.AttributeType.STRING },
    });
    const rrmTable = new dynamodb.Table(depStack, "RRM", {
      partitionKey: { name: "PK", type: dynamodb.AttributeType.STRING },
    });
    template = Template.fromStack(
      new NamespaceStack(app, "TestNamespaceDdb", {
        vpc,
        lambdaSecurityGroup,
        rolesTable,
        resourceRoleMappingsTable: rrmTable,
      }),
    );
  });

  /** The namespace-api Lambda resource. */
  function namespaceApiFn(): any {
    const fn = Object.values(
      template.findResources("AWS::Lambda::Function", {
        Properties: { FunctionName: `${PREFIX}-namespace-api` },
      }),
    )[0];
    expect(fn).toBeDefined();
    return fn;
  }

  /** IAM statements attached to the namespace-api Lambda's role. */
  function namespaceApiStatements(): any[] {
    const roleId = namespaceApiFn().Properties.Role["Fn::GetAtt"][0];
    return Object.values(template.findResources("AWS::IAM::Policy"))
      .filter((p: any) => p.Properties.Roles?.some((r: any) => r.Ref === roleId))
      .flatMap((p: any) => p.Properties.PolicyDocument.Statement);
  }

  const actionsOf = (s: any): string[] =>
    Array.isArray(s.Action) ? s.Action : [s.Action];

  test("DynamoDB mode: creates the metadata-assets table with PK/SK", () => {
    template.hasResourceProperties("AWS::DynamoDB::Table", {
      TableName: ASSET_TABLE,
      KeySchema: [
        { AttributeName: "PK", KeyType: "HASH" },
        { AttributeName: "SK", KeyType: "RANGE" },
      ],
    });
  });

  test("DynamoDB mode: metadata-assets table has an AssetById GSI on assetId", () => {
    template.hasResourceProperties("AWS::DynamoDB::Table", {
      TableName: ASSET_TABLE,
      GlobalSecondaryIndexes: Match.arrayWith([
        Match.objectLike({
          IndexName: "AssetById",
          KeySchema: [{ AttributeName: "assetId", KeyType: "HASH" }],
        }),
      ]),
    });
  });

  test("DynamoDB mode: creates no AWS::DataZone resources", () => {
    const dataZoneTypes = Object.values(template.toJSON().Resources ?? {})
      .map((r: any) => r.Type as string)
      .filter((t) => t.startsWith("AWS::DataZone::"));
    expect(dataZoneTypes).toEqual([]);
  });

  test("DynamoDB mode: creates no SMUS execution, login or project-access role", () => {
    const roleNames = Object.values(
      template.findResources("AWS::IAM::Role"),
    ).map((r: any) => r.Properties?.RoleName);
    expect(roleNames).not.toContain(`${PREFIX}-smus-exec`);
    expect(roleNames).not.toContain(`${PREFIX}-smus-login`);
    expect(roleNames).not.toContain(`${PREFIX}-dz-project-access`);
  });

  test("DynamoDB mode: publishes the placeholder domain id to /smus/domain-id", () => {
    template.hasResourceProperties("AWS::SSM::Parameter", {
      Name: `/${DEFAULT_RESOURCE_PREFIX}/smus/domain-id`,
      Value: "NO_DATAZONE_DOMAIN",
    });
  });

  test("DynamoDB mode: publishes the metadata-assets table name to SSM", () => {
    template.hasResourceProperties("AWS::SSM::Parameter", {
      Name: `/${DEFAULT_RESOURCE_PREFIX}/metadata-store/table-name`,
    });
  });

  test("DynamoDB mode: NamespaceApiFn selects the DynamoDB backend and the asset table", () => {
    template.hasResourceProperties("AWS::Lambda::Function", {
      FunctionName: `${PREFIX}-namespace-api`,
      Environment: {
        Variables: Match.objectLike({
          METADATA_STORE_BACKEND: "dynamodb",
          METADATA_STORE_TABLE: Match.anyValue(),
        }),
      },
    });
  });

  test("DynamoDB mode: NamespaceApiFn has no DataZone env or permissions and can write the asset table", () => {
    const vars = namespaceApiFn().Properties.Environment.Variables;
    expect(vars).not.toHaveProperty("DATAZONE_DOMAIN_ID");
    expect(vars).not.toHaveProperty("DATAZONE_PROJECT_PROFILE_ID");
    expect(vars).not.toHaveProperty("PROJECT_ACCESS_ROLE_ARN_SSM");

    const statements = namespaceApiStatements();
    const dataZoneActions = statements
      .flatMap(actionsOf)
      .filter((a) => a.startsWith("datazone:"));
    expect(dataZoneActions).toEqual([]);

    // The grant targets the asset table either by reference (logical ID starts
    // with "MetadataAssets") or by its physical name.
    const assetTableWrites = statements.filter((s) => {
      const resource = JSON.stringify(s.Resource);
      return (
        actionsOf(s).includes("dynamodb:PutItem") &&
        (resource.includes("MetadataAssets") ||
          resource.includes("metadata-assets"))
      );
    });
    expect(assetTableWrites.length).toBeGreaterThan(0);
  });

  test("DynamoDB mode: still creates the Namespaces table", () => {
    template.hasResourceProperties("AWS::DynamoDB::Table", {
      TableName: Match.stringLikeRegexp("namespaces$"),
    });
  });

  test("DynamoDB mode: NamespaceApiFn is still in the VPC private subnets", () => {
    template.hasResourceProperties("AWS::Lambda::Function", {
      FunctionName: `${PREFIX}-namespace-api`,
      VpcConfig: Match.objectLike({
        SubnetIds: Match.anyValue(),
        SecurityGroupIds: Match.anyValue(),
      }),
    });
  });
});

// Internal ALB instead of Cloud Map (service_discovery=alb): the VKG listener
// ARN reaches the deletion pipeline so namespace delete can remove the
// namespace's listener rule and target group.
describe("NamespaceStack in ALB mode (service_discovery=alb)", () => {
  test("ALB mode: DeletePlatform gets the VKG listener ARN and no Cloud Map namespace", () => {
    const listenerArn =
      "arn:aws:elasticloadbalancing:us-east-1:123456789012:listener/app/coa-svc/abc/def";
    const app = new cdk.App({
      context: { ...TEST_CONTEXT, service_discovery: "alb" },
    });
    const depStack = new cdk.Stack(app, "AlbDepStack");
    const vpc = new ec2.Vpc(depStack, "Vpc");
    const lambdaSecurityGroup = new ec2.SecurityGroup(depStack, "LambdaSG", {
      vpc,
      allowAllOutbound: true,
    });
    const rolesTable = new dynamodb.Table(depStack, "Roles", {
      partitionKey: { name: "PK", type: dynamodb.AttributeType.STRING },
    });
    const rrmTable = new dynamodb.Table(depStack, "RRM", {
      partitionKey: { name: "PK", type: dynamodb.AttributeType.STRING },
    });
    const tpl = Template.fromStack(
      new NamespaceStack(app, "TestNamespaceAlb", {
        vpc,
        lambdaSecurityGroup,
        rolesTable,
        resourceRoleMappingsTable: rrmTable,
        vkgClusterArn: "arn:aws:ecs:us-east-1:123456789012:cluster/coa-test-vkg",
        vkgListenerArn: listenerArn,
      }),
    );
    const fn = Object.values(
      tpl.findResources("AWS::Lambda::Function", {
        Properties: { FunctionName: Match.stringLikeRegexp("ns-deletion-platform$") },
      }),
    )[0] as any;
    expect(fn).toBeDefined();
    const vars = fn.Properties.Environment.Variables;
    expect(vars.VKG_LISTENER_ARN).toBe(listenerArn);
    expect(vars.SERVICE_DISCOVERY_BACKEND).toBe("alb");
    expect(vars).not.toHaveProperty("CLOUD_MAP_NAMESPACE_ID");

    // HTTPS: the namespace API (active-jobs check on ontology-engine) trusts
    // the private CA through the CA root in SSM.
    const api = Object.values(
      tpl.findResources("AWS::Lambda::Function", {
        Properties: { FunctionName: Match.stringLikeRegexp("namespace-api$") },
      }),
    )[0] as any;
    expect(api.Properties.Environment.Variables.SERVICE_CA_CERT_PARAM).toBe(
      "/coa/services/ca-cert",
    );
    const statements = Object.values(tpl.findResources("AWS::IAM::Policy"))
      .filter((p: any) => (p.Properties.PolicyName as string).includes("NamespaceApiFn"))
      .flatMap((p: any) => p.Properties.PolicyDocument.Statement);
    expect(
      statements.some(
        (st: any) =>
          [st.Action].flat().includes("ssm:GetParameter") &&
          JSON.stringify(st.Resource).includes("/coa/services/ca-cert"),
      ),
    ).toBe(true);
  });
});
