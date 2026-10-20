// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import * as cdk from "aws-cdk-lib";
import * as ec2 from "aws-cdk-lib/aws-ec2";
import { Match, Template } from "aws-cdk-lib/assertions";
import { WebAlbConstruct } from "../../lib/constructs/web-alb-construct";
import { DEFAULT_RESOURCE_PREFIX } from "../../lib/constants";
import type { RuntimeConfig } from "@coa/shared";

const RUNTIME_CONFIG: RuntimeConfig = {
  region: "us-east-1",
  stage: "dev",
  authority: "https://cognito-idp.us-east-1.amazonaws.com/us-east-1_abc",
  clientId: "client-abc",
};

/** Each call gets its own App so context does not leak between tests. */
function render(
  vpcOptions: ec2.VpcProps = { maxAzs: 2 },
): { template: Template; stack: cdk.Stack } {
  const app = new cdk.App({
    context: { resource_prefix: DEFAULT_RESOURCE_PREFIX, env: "dev" },
  });
  const stack = new cdk.Stack(app, "TestStack");
  const vpc = new ec2.Vpc(stack, "Vpc", vpcOptions);
  new WebAlbConstruct(stack, "WebAlb", { vpc, runtimeConfig: RUNTIME_CONFIG });
  return { template: Template.fromStack(stack), stack };
}

describe("WebAlbConstruct network isolation", () => {
  test("the service accepts 8080 from the load balancer SG only", () => {
    // NetworkStack's shared ecsSecurityGroup already opens 8080 to the whole
    // VPC CIDR for VKG, which is why this service has its own SG.
    const { template } = render();

    template.hasResourceProperties("AWS::EC2::SecurityGroupIngress", {
      FromPort: 8080,
      ToPort: 8080,
      IpProtocol: "tcp",
      SourceSecurityGroupId: Match.anyValue(),
    });

    const vpcWideOn8080 = Object.values(
      template.findResources("AWS::EC2::SecurityGroupIngress"),
    ).filter(
      (r) => r.Properties.FromPort === 8080 && r.Properties.CidrIp !== undefined,
    );
    expect(vpcWideOn8080).toEqual([]);
  });

  test("the load balancer SG takes 443 and 80 from the internet", () => {
    const { template } = render();
    const alb = Object.values(
      template.findResources("AWS::EC2::SecurityGroup"),
    ).find((sg) =>
      (sg.Properties.SecurityGroupIngress ?? []).some(
        (r: { CidrIp?: string }) => r.CidrIp === "0.0.0.0/0",
      ),
    );
    const ports = (
      alb?.Properties.SecurityGroupIngress as { FromPort: number }[]
    ).map((r) => r.FromPort);
    expect(ports.sort()).toEqual([80, 443]);
  });

  test("tasks run in private subnets without public IPs", () => {
    const { template } = render();
    template.hasResourceProperties("AWS::ECS::Service", {
      NetworkConfiguration: Match.objectLike({
        AwsvpcConfiguration: Match.objectLike({ AssignPublicIp: "DISABLED" }),
      }),
    });
  });
});

describe("WebAlbConstruct public-subnet guard", () => {
  test("refuses a VPC with fewer than two public subnets", () => {
    // An imported VPC (-c vpc_id) may have no IGW-routed subnets; CDK's own
    // error is opaque and the 2-AZ rule otherwise fails at CreateLoadBalancer.
    expect(() =>
      render({
        maxAzs: 1,
        subnetConfiguration: [
          {
            name: "Private",
            subnetType: ec2.SubnetType.PRIVATE_ISOLATED,
            cidrMask: 24,
          },
        ],
      }),
    ).toThrow(/public subnets in at least 2 AZs/);
  });
});

describe("WebAlbConstruct operator content", () => {
  test("creates a private content bucket and grants the task read on questions/", () => {
    const { template } = render();

    template.hasResourceProperties("AWS::S3::Bucket", {
      PublicAccessBlockConfiguration: {
        BlockPublicAcls: true,
        BlockPublicPolicy: true,
        IgnorePublicAcls: true,
        RestrictPublicBuckets: true,
      },
    });

    template.hasResourceProperties("AWS::IAM::Policy", {
      PolicyDocument: Match.objectLike({
        Statement: Match.arrayWith([
          Match.objectLike({
            Action: "s3:ListBucket",
            Condition: { StringLike: { "s3:prefix": ["questions/*"] } },
          }),
        ]),
      }),
    });

    template.hasResourceProperties("AWS::ECS::TaskDefinition", {
      ContainerDefinitions: Match.arrayWith([
        Match.objectLike({
          Environment: Match.arrayWith([
            Match.objectLike({ Name: "UI_CONTENT_BUCKET" }),
          ]),
        }),
      ]),
    });
  });
});

describe("WebAlbConstruct WAF", () => {
  test("creates a REGIONAL WebACL and associates it with the ALB", () => {
    const { template } = render();
    template.hasResourceProperties("AWS::WAFv2::WebACL", {
      Scope: "REGIONAL",
      Name: `${DEFAULT_RESOURCE_PREFIX}-dev-ui-waf`,
    });
    template.resourceCountIs("AWS::WAFv2::WebACLAssociation", 1);
  });
});
