// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import * as cdk from "aws-cdk-lib";
import { Template, Match } from "aws-cdk-lib/assertions";
import { NetworkStack } from "../../lib/stacks/foundation/network-stack";

// Env required for fromLookup tests
const TEST_ENV = { account: "123456789012", region: "us-east-1" };

function buildStack(context: Record<string, string> = {}): Template {
  const app = new cdk.App({ context });
  const stack = new NetworkStack(app, "TestNetwork");
  return Template.fromStack(stack);
}

/** Egress rules of the Glue-connector security group. */
function connectorEgress(template: Template): any[] {
  const sgs = template.findResources("AWS::EC2::SecurityGroup");
  const connector = (Object.values(sgs) as any[]).find((sg) =>
    (sg.Properties.GroupDescription as string).startsWith(
      "Glue Connection / Athena connector",
    ),
  );
  expect(connector).toBeDefined();
  return connector.Properties.SecurityGroupEgress ?? [];
}

/** Egress rules of the shared Lambda security group. */
function lambdaEgress(template: Template): any[] {
  const sgs = template.findResources("AWS::EC2::SecurityGroup");
  const lambda = (Object.values(sgs) as any[]).find((sg) =>
    (sg.Properties.GroupDescription as string).startsWith("Lambda functions"),
  );
  expect(lambda).toBeDefined();
  return lambda.Properties.SecurityGroupEgress ?? [];
}

/** Egress rules dedicated to direct-discovery Snowflake OCSP. */
function discoveryOcspEgress(template: Template): any[] {
  const sgs = template.findResources("AWS::EC2::SecurityGroup");
  const discovery = (Object.values(sgs) as any[]).find((sg) =>
    (sg.Properties.GroupDescription as string).startsWith(
      "Snowflake discovery OCSP",
    ),
  );
  expect(discovery).toBeDefined();
  return discovery.Properties.SecurityGroupEgress ?? [];
}

function buildStackWithEnv(context: Record<string, string> = {}): Template {
  const app = new cdk.App({ context });
  const stack = new NetworkStack(app, "TestNetwork", { env: TEST_ENV });
  return Template.fromStack(stack);
}

describe("NetworkStack", () => {
  describe("VPC", () => {
    it("creates a VPC with public and private subnets", () => {
      const template = buildStack();
      template.hasResourceProperties("AWS::EC2::VPC", {});
      // 2 AZs x 2 subnet types = 4 subnets
      template.resourceCountIs("AWS::EC2::Subnet", 4);
    });

    it("creates exactly one NAT gateway", () => {
      const template = buildStack();
      template.resourceCountIs("AWS::EC2::NatGateway", 1);
    });

    it("uses fromLookup when vpc_id context is set (no VPC resource created)", () => {
      // fromLookup with a known account/region produces a dummy VPC at synth time
      const template = buildStackWithEnv({ vpc_id: "vpc-0abc123" });
      template.resourceCountIs("AWS::EC2::VPC", 0);
    });
  });

  describe("VPC Endpoints", () => {
    it("creates S3 and DynamoDB gateway endpoints", () => {
      const template = buildStack();
      const endpoints = template.findResources("AWS::EC2::VPCEndpoint");
      // Gateway endpoints have ServiceName as Fn::Sub object
      const serviceNames = Object.values(endpoints).map((e: any) =>
        JSON.stringify(e.Properties.ServiceName),
      );
      expect(serviceNames.some((s) => s.includes("s3"))).toBe(true);
      expect(serviceNames.some((s) => s.includes("dynamodb"))).toBe(true);
    });

    it("creates Bedrock Runtime, CloudWatch Logs, ECR API, and ECR Docker interface endpoints", () => {
      const template = buildStack();
      const endpoints = template.findResources("AWS::EC2::VPCEndpoint");
      const serviceNames = Object.values(endpoints).map((e: any) =>
        JSON.stringify(e.Properties.ServiceName),
      );
      expect(serviceNames.some((s) => s.includes("bedrock-runtime"))).toBe(
        true,
      );
      expect(serviceNames.some((s) => s.includes("logs"))).toBe(true);
      expect(serviceNames.some((s) => s.includes("ecr.api"))).toBe(true);
      expect(serviceNames.some((s) => s.includes("ecr.dkr"))).toBe(true);
    });

    it("creates the additional AWS API interface endpoints for scoped egress", () => {
      const template = buildStack();
      const endpoints = template.findResources("AWS::EC2::VPCEndpoint");
      const serviceNames = Object.values(endpoints).map((e: any) =>
        JSON.stringify(e.Properties.ServiceName),
      );
      for (const fragment of [
        ".sts",
        ".events",
        ".sqs",
        ".states",
        ".athena",
        ".glue",
        ".lambda",
        ".ecs",
        ".datazone",
        "neptune-graph",
        ".aoss",
      ]) {
        expect(serviceNames.some((s) => s.includes(fragment))).toBe(true);
      }
    });

    it("does not create VPC endpoints when importing an existing VPC", () => {
      const template = buildStackWithEnv({ vpc_id: "vpc-0abc123" });
      template.resourceCountIs("AWS::EC2::VPCEndpoint", 0);
    });
  });

  describe("Security Groups", () => {
    it("creates Neptune SG with correct description", () => {
      const template = buildStack();
      template.hasResourceProperties("AWS::EC2::SecurityGroup", {
        GroupDescription: "Neptune cluster security group",
      });
    });

    it("Neptune SG ingress on 8182 is scoped to the ECS + Lambda SGs (not VPC CIDR)", () => {
      const template = buildStack();
      // Ingress is added after both client SGs exist, so it materializes as
      // standalone SecurityGroupIngress resources referencing a source SG.
      const ingress = template.findResources("AWS::EC2::SecurityGroupIngress", {
        Properties: {
          FromPort: 8182,
          ToPort: 8182,
          IpProtocol: "tcp",
          SourceSecurityGroupId: Match.anyValue(),
        },
      });
      // One rule from ECS SG + one from Lambda SG.
      expect(Object.keys(ingress).length).toBe(2);
      // And none of them open 8182 to a CIDR block.
      const cidrIngress = template.findResources(
        "AWS::EC2::SecurityGroupIngress",
        {
          Properties: { FromPort: 8182, CidrIp: Match.anyValue() },
        },
      );
      expect(Object.keys(cidrIngress).length).toBe(0);
    });

    it("creates ECS and Lambda security groups with egress locked down (no allow-all)", () => {
      const template = buildStack();
      template.hasResourceProperties("AWS::EC2::SecurityGroup", {
        GroupDescription: "ECS Fargate tasks security group",
      });
      template.hasResourceProperties("AWS::EC2::SecurityGroup", {
        GroupDescription: "Lambda functions security group",
      });
      // Neither SG should carry the CDK allow-all egress rule
      // (0.0.0.0/0 on IpProtocol "-1").
      const sgs = template.findResources("AWS::EC2::SecurityGroup");
      for (const sg of Object.values(sgs) as any[]) {
        const desc = sg.Properties.GroupDescription as string;
        if (desc.startsWith("ECS Fargate") || desc.startsWith("Lambda")) {
          const egress = sg.Properties.SecurityGroupEgress ?? [];
          expect(
            egress.some(
              (r: any) => r.IpProtocol === "-1" && r.CidrIp === "0.0.0.0/0",
            ),
          ).toBe(false);
          // 443 egress should be present.
          expect(
            egress.some((r: any) => r.FromPort === 443 && r.ToPort === 443),
          ).toBe(true);
        }
      }
    });

    it("Lambda SG has egress to source-DB ports (direct JDBC discovery)", () => {
      const template = buildStack();
      const sgs = template.findResources("AWS::EC2::SecurityGroup");
      const lambdaSg = (Object.values(sgs) as any[]).find((sg) =>
        (sg.Properties.GroupDescription as string).startsWith(
          "Lambda functions",
        ),
      );
      expect(lambdaSg).toBeDefined();
      const egress = lambdaSg.Properties.SecurityGroupEgress ?? [];
      for (const port of [5432, 3306, 1433, 5439, 1521]) {
        expect(
          egress.some((r: any) => r.FromPort === port && r.ToPort === port),
        ).toBe(true);
      }
    });

    it("Connector SG restricts egress to source-DB ports + 443 (no allow-all)", () => {
      const template = buildStack();
      const sgs = template.findResources("AWS::EC2::SecurityGroup");
      const connector = (Object.values(sgs) as any[]).find((sg) =>
        (sg.Properties.GroupDescription as string).startsWith(
          "Glue Connection / Athena connector",
        ),
      );
      expect(connector).toBeDefined();
      const egress = connector.Properties.SecurityGroupEgress ?? [];
      // No allow-all egress.
      expect(
        egress.some(
          (r: any) => r.IpProtocol === "-1" && r.CidrIp === "0.0.0.0/0",
        ),
      ).toBe(false);
      // Each supported DB port + 443 present.
      for (const port of [5432, 3306, 1433, 5439, 1521, 443]) {
        expect(
          egress.some((r: any) => r.FromPort === port && r.ToPort === port),
        ).toBe(true);
      }
    });

    it("Connector and direct-discovery SGs allow port 80 for Snowflake OCSP by default", () => {
      const template = buildStack();
      for (const egress of [
        connectorEgress(template),
        discoveryOcspEgress(template),
      ]) {
        expect(
          egress.some((r: any) => r.FromPort === 80 && r.ToPort === 80),
        ).toBe(true);
        expect(
          egress.some(
            (r: any) => r.IpProtocol === "-1" && r.CidrIp === "0.0.0.0/0",
          ),
        ).toBe(false);
      }
      expect(
        lambdaEgress(template).some(
          (r: any) => r.FromPort === 80 && r.ToPort === 80,
        ),
      ).toBe(false);
    });

    it("Connector and direct-discovery SGs drop OCSP when connector_ocsp_egress=false", () => {
      // A deployment with no Snowflake source has no use for port 80 egress.
      const template = buildStack({ connector_ocsp_egress: "false" });
      for (const egress of [
        connectorEgress(template),
        discoveryOcspEgress(template),
      ]) {
        expect(
          egress.some((r: any) => r.FromPort === 80 && r.ToPort === 80),
        ).toBe(false);
      }
      const egress = connectorEgress(template);
      expect(
        egress.some((r: any) => r.FromPort === 443 && r.ToPort === 443),
      ).toBe(true);
    });

    it("connector_egress_cidrs scopes every connector egress rule, not just one port", () => {
      // The default is 0.0.0.0/0 because a managed connector reaching a SaaS
      // warehouse has no enumerable destination set; where the egress path IS
      // known (fixed NAT/proxy), this narrows OCSP, 443 and the DB ports alike.
      const egress = connectorEgress(
        buildStack({ connector_egress_cidrs: "10.20.0.0/16,192.0.2.10/32" }),
      );
      const internetBound = egress.filter((r: any) =>
        [80, 443, 5432, 3306, 1433, 5439, 1521].includes(r.FromPort),
      );
      expect(internetBound.length).toBeGreaterThan(0);
      for (const rule of internetBound) {
        expect(["10.20.0.0/16", "192.0.2.10/32"]).toContain(rule.CidrIp);
      }
      expect(egress.some((r: any) => r.CidrIp === "0.0.0.0/0")).toBe(false);
      // Both CIDRs get the full port set, so narrowing never silently drops one.
      for (const cidr of ["10.20.0.0/16", "192.0.2.10/32"]) {
        for (const port of [80, 443, 1521]) {
          expect(
            egress.some((r: any) => r.CidrIp === cidr && r.FromPort === port),
          ).toBe(true);
        }
      }
    });

    it("connector_egress_cidrs also scopes direct-discovery OCSP egress", () => {
      const egress = discoveryOcspEgress(
        buildStack({
          connector_egress_cidrs: "10.20.0.0/16,192.0.2.10/32",
        }),
      ).filter((r: any) => r.FromPort === 80 && r.ToPort === 80);

      expect(egress).toHaveLength(2);
      expect(egress.map((r: any) => r.CidrIp).sort()).toEqual(
        ["10.20.0.0/16", "192.0.2.10/32"].sort(),
      );
      expect(egress.some((r: any) => r.CidrIp === "0.0.0.0/0")).toBe(false);
    });
  });

  describe("Cloud Map Namespace", () => {
    it("creates a shared PrivateDnsNamespace associated with the VPC", () => {
      const template = buildStack();
      template.hasResourceProperties(
        "AWS::ServiceDiscovery::PrivateDnsNamespace",
        {
          Name: Match.stringLikeRegexp(".*-services\\.local$"),
          Vpc: Match.anyValue(),
        },
      );
    });
  });

  describe("Outputs", () => {
    it("exports VPC ID", () => {
      const template = buildStack();
      const outputs = template.findOutputs("VpcId");
      expect(Object.keys(outputs).length).toBe(1);
    });

    // Read by scripts/deploy-managed-databricks-connector.sh, which attaches the COA-operated
    // connector to this VPC. The env segment keeps two environments in one account apart.
    it("publishes the VPC ID and private subnet IDs under the environment's SSM path", () => {
      const template = buildStack({ resource_prefix: "scl", env: "beta" });
      template.hasResourceProperties("AWS::SSM::Parameter", {
        Name: "/scl/beta/network/vpc-id",
        Value: { Ref: Match.stringLikeRegexp("Vpc") },
      });
      const subnets = template.findResources("AWS::SSM::Parameter", {
        Properties: { Name: "/scl/beta/network/private-subnet-ids" },
      });
      const [param] = Object.values(subnets) as any[];
      expect(param).toBeDefined();
      // Both private subnets and no public one: a Lambda in a public subnet gets no egress.
      const joined = JSON.stringify(param.Properties.Value);
      expect(joined.match(/PrivateSubnet/g)).toHaveLength(2);
      expect(joined).not.toMatch(/PublicSubnet/);
    });
  });

  describe("Cross-network JDBC connectivity", () => {
    it("creates no connectivity resources by default", () => {
      const template = buildStack();
      template.resourceCountIs("AWS::EC2::VPCPeeringConnection", 0);
      template.resourceCountIs("AWS::EC2::TransitGatewayVpcAttachment", 0);
    });

    it("provisions VPC peering + routes from context", () => {
      const template = buildStack({
        jdbc_peer_vpc_id: "vpc-0peer123",
        jdbc_peer_cidrs: "10.20.0.0/16,10.21.0.0/16",
      });
      template.hasResourceProperties("AWS::EC2::VPCPeeringConnection", {
        PeerVpcId: "vpc-0peer123",
      });
      const routes = template.findResources("AWS::EC2::Route", {
        Properties: { VpcPeeringConnectionId: Match.anyValue() },
      });
      // 2 private subnets x 2 CIDRs.
      expect(Object.keys(routes).length).toBe(4);
    });

    it("throws when peer VPC is set without CIDRs", () => {
      expect(() => buildStack({ jdbc_peer_vpc_id: "vpc-0peer123" })).toThrow(
        /jdbc_peer_cidrs/,
      );
    });

    it("provisions a Transit Gateway attachment + routes from context", () => {
      const template = buildStack({
        jdbc_tgw_id: "tgw-0abc",
        jdbc_tgw_cidrs: "10.30.0.0/16",
      });
      template.hasResourceProperties("AWS::EC2::TransitGatewayVpcAttachment", {
        TransitGatewayId: "tgw-0abc",
      });
    });

    it("provisions a PrivateLink interface endpoint from context", () => {
      const template = buildStack({
        jdbc_privatelink_service: "com.amazonaws.vpce.us-east-1.vpce-svc-0abc",
        jdbc_privatelink_port: "5432",
      });
      template.hasResourceProperties("AWS::EC2::VPCEndpoint", {
        ServiceName: "com.amazonaws.vpce.us-east-1.vpce-svc-0abc",
        VpcEndpointType: "Interface",
      });
    });

    it("throws when PrivateLink service has an invalid port", () => {
      expect(() =>
        buildStack({
          jdbc_privatelink_service:
            "com.amazonaws.vpce.us-east-1.vpce-svc-0abc",
          jdbc_privatelink_port: "not-a-port",
        }),
      ).toThrow(/jdbc_privatelink_port/);
    });

    it("does not provision connectivity when importing an existing VPC", () => {
      const template = buildStackWithEnv({
        vpc_id: "vpc-0abc123",
        jdbc_peer_vpc_id: "vpc-0peer123",
        jdbc_peer_cidrs: "10.20.0.0/16",
      });
      template.resourceCountIs("AWS::EC2::VPCPeeringConnection", 0);
    });
  });
});

// DynamoDB metadata store (metadata_store=dynamodb): nothing calls DataZone, so
// the DataZone interface endpoint is not created. Every other endpoint stays.
describe("NetworkStack in DynamoDB mode (metadata_store=dynamodb)", () => {
  const endpointServiceNames = (template: Template): string[] =>
    Object.values(template.findResources("AWS::EC2::VPCEndpoint")).map(
      (e: any) => JSON.stringify(e.Properties.ServiceName),
    );

  it("DynamoDB mode: creates no DataZone interface endpoint", () => {
    const template = buildStack({ metadata_store: "dynamodb" });
    expect(
      endpointServiceNames(template).some((s) => s.includes(".datazone")),
    ).toBe(false);
  });

  it("DynamoDB mode: still creates every other endpoint", () => {
    const serviceNames = endpointServiceNames(
      buildStack({ metadata_store: "dynamodb" }),
    );
    for (const fragment of [
      "s3",
      "dynamodb",
      "bedrock-runtime",
      "logs",
      "ecr.api",
      "ecr.dkr",
      ".sts",
      ".events",
      ".sqs",
      ".states",
      ".athena",
      ".glue",
      ".lambda",
      ".ecs",
      "neptune-graph",
      ".aoss",
    ]) {
      expect(serviceNames.some((s) => s.includes(fragment))).toBe(true);
    }
  });
});

// Internal ALB instead of Cloud Map (service_discovery=alb). Cloud Map is not
// approved in every account, so ontology-engine and the per-namespace VKG
// services can sit behind one internal ALB instead. Default mode is unchanged.
describe("NetworkStack in ALB mode (service_discovery=alb)", () => {
  const ALB_CONTEXT = { service_discovery: "alb" };

  /** Security group whose GroupDescription starts with the given text. */
  const sgLogicalId = (template: Template, description: string): string => {
    const sgs = template.findResources("AWS::EC2::SecurityGroup");
    const id = Object.keys(sgs).find((key) =>
      (sgs[key].Properties.GroupDescription as string).startsWith(description),
    );
    expect(id).toBeDefined();
    return id!;
  };

  it("ALB mode: creates no Cloud Map namespace", () => {
    buildStack(ALB_CONTEXT).resourceCountIs(
      "AWS::ServiceDiscovery::PrivateDnsNamespace",
      0,
    );
  });

  it("ALB mode: creates one internal ALB with a 180 s idle timeout", () => {
    const template = buildStack(ALB_CONTEXT);
    template.resourceCountIs("AWS::ElasticLoadBalancingV2::LoadBalancer", 1);
    template.hasResourceProperties(
      "AWS::ElasticLoadBalancingV2::LoadBalancer",
      {
        Scheme: "internal",
        Type: "application",
        LoadBalancerAttributes: Match.arrayWith([
          { Key: "idle_timeout.timeout_seconds", Value: "180" },
        ]),
      },
    );
  });

  it("ALB mode: ALB security group takes 8001 from the Lambda SG and 8080 from the VPC only", () => {
    const template = buildStack(ALB_CONTEXT);
    const albSg = template.findResources("AWS::EC2::SecurityGroup")[
      sgLogicalId(template, "Internal services ALB")
    ];
    const inline = albSg.Properties.SecurityGroupIngress ?? [];
    const standalone = Object.values(
      template.findResources("AWS::EC2::SecurityGroupIngress"),
    )
      .map((r: any) => r.Properties)
      .filter(
        (p: any) =>
          JSON.stringify(p.GroupId).includes(
            sgLogicalId(template, "Internal services ALB"),
          ),
      );
    const ingress = [...inline, ...standalone];
    expect(ingress.some((r: any) => r.CidrIp === "0.0.0.0/0")).toBe(false);
    expect(
      ingress.some(
        (r: any) =>
          r.FromPort === 8001 &&
          JSON.stringify(r.SourceSecurityGroupId).includes(
            sgLogicalId(template, "Lambda functions"),
          ),
      ),
    ).toBe(true);
    expect(
      ingress.some((r: any) => r.FromPort === 8080 && r.CidrIp !== undefined),
    ).toBe(true);
  });

  it("ALB mode: ECS tasks accept 8080 from the ALB, and Lambdas reach the ALB on 8001", () => {
    const template = buildStack(ALB_CONTEXT);
    const albId = sgLogicalId(template, "Internal services ALB");
    template.hasResourceProperties("AWS::EC2::SecurityGroupIngress", {
      FromPort: 8080,
      ToPort: 8080,
      GroupId: { "Fn::GetAtt": [sgLogicalId(template, "ECS Fargate"), "GroupId"] },
      SourceSecurityGroupId: { "Fn::GetAtt": [albId, "GroupId"] },
    });
    const lambdaToAlb = [
      ...lambdaEgress(template),
      ...Object.values(template.findResources("AWS::EC2::SecurityGroupEgress")).map(
        (r: any) => r.Properties,
      ),
    ].filter(
      (r: any) =>
        r.FromPort === 8001 &&
        JSON.stringify(r.DestinationSecurityGroupId ?? "").includes(albId),
    );
    expect(lambdaToAlb.length).toBe(1);
  });

  it("default mode: creates no ALB", () => {
    buildStack().resourceCountIs("AWS::ElasticLoadBalancingV2::LoadBalancer", 0);
  });
});

// HTTPS for the internal ALB (replace-cloud-map-notes.txt sections 14-15):
// services get names in a Route 53 private zone and a certificate for them,
// from AWS Private CA (service_cert=private-ca, the default) or a public ACM
// certificate (service_cert=acm-public). Default (Cloud Map) mode is unchanged.
describe("NetworkStack HTTPS for the internal ALB", () => {
  const ALB = { service_discovery: "alb" };
  const PUBLIC = {
    service_discovery: "alb",
    service_cert: "acm-public",
    service_domain: "coa-poc.example.com",
    public_zone_id: "Z0123456789ABC",
  };

  it("ALB mode: private hosted zone <prefix>-<env>.internal attached to the VPC", () => {
    buildStack(ALB).hasResourceProperties("AWS::Route53::HostedZone", {
      Name: "coa-dev.internal.",
      VPCs: Match.arrayWith([Match.objectLike({ VPCId: Match.anyValue() })]),
    });
  });

  it("ALB mode: ontology-engine and vkg records alias the internal ALB", () => {
    const template = buildStack(ALB);
    for (const name of ["ontology-engine.coa-dev.internal.", "vkg.coa-dev.internal."]) {
      template.hasResourceProperties("AWS::Route53::RecordSet", {
        Name: name,
        Type: "A",
        AliasTarget: Match.objectLike({ DNSName: Match.anyValue() }),
      });
    }
  });

  it("ALB mode (private-ca, default): general-purpose root CA, activated, ACM may use it", () => {
    const template = buildStack(ALB);
    template.hasResourceProperties("AWS::ACMPCA::CertificateAuthority", {
      Type: "ROOT",
      UsageMode: "GENERAL_PURPOSE",
    });
    template.resourceCountIs("AWS::ACMPCA::Certificate", 1);
    template.hasResourceProperties("AWS::ACMPCA::CertificateAuthorityActivation", {
      Status: "ACTIVE",
    });
    template.hasResourceProperties("AWS::ACMPCA::Permission", {
      Principal: "acm.amazonaws.com",
      Actions: Match.arrayWith(["IssueCertificate", "GetCertificate", "ListPermissions"]),
    });
  });

  it("ALB mode (private-ca): root CA certificate template ARN uses the stack partition (GovCloud)", () => {
    const [cert] = Object.values(
      buildStack(ALB).findResources("AWS::ACMPCA::Certificate"),
    ) as any[];
    const arn = JSON.stringify(cert.Properties.TemplateArn);
    expect(arn).toContain("AWS::Partition");
    expect(arn).toContain("template/RootCACertificate/V1");
  });

  it("ALB mode (private-ca): ACM private certificate for *.<zone>, issued by the CA", () => {
    buildStack(ALB).hasResourceProperties("AWS::CertificateManager::Certificate", {
      DomainName: "*.coa-dev.internal",
      CertificateAuthorityArn: Match.anyValue(),
    });
  });

  it("ALB mode (private-ca): CA root certificate published to SSM for the callers", () => {
    buildStack(ALB).hasResourceProperties("AWS::SSM::Parameter", {
      Name: "/coa/services/ca-cert",
    });
  });

  it("service_cert=acm-public: DNS-validated public certificate, no Private CA, no CA parameter", () => {
    const template = buildStack(PUBLIC);
    template.hasResourceProperties("AWS::CertificateManager::Certificate", {
      DomainName: "*.coa-poc.example.com",
      ValidationMethod: "DNS",
      DomainValidationOptions: Match.arrayWith([
        Match.objectLike({ HostedZoneId: "Z0123456789ABC" }),
      ]),
    });
    template.resourceCountIs("AWS::ACMPCA::CertificateAuthority", 0);
    expect(
      Object.keys(
        template.findResources("AWS::SSM::Parameter", {
          Properties: { Name: "/coa/services/ca-cert" },
        }),
      ),
    ).toHaveLength(0);
    template.hasResourceProperties("AWS::Route53::HostedZone", {
      Name: "coa-poc.example.com.",
    });
  });

  it("service_cert=acm-public without service_domain or public_zone_id fails synth", () => {
    expect(() =>
      buildStack({ service_discovery: "alb", service_cert: "acm-public" }),
    ).toThrow(/service_domain|public_zone_id/);
  });

  it("an unknown service_cert fails synth", () => {
    expect(() =>
      buildStack({ service_discovery: "alb", service_cert: "self-signed" }),
    ).toThrow(/service_cert/);
  });

  it("default mode: no hosted zone, no CA, no certificate", () => {
    const template = buildStack();
    template.resourceCountIs("AWS::Route53::HostedZone", 0);
    template.resourceCountIs("AWS::ACMPCA::CertificateAuthority", 0);
    template.resourceCountIs("AWS::CertificateManager::Certificate", 0);
  });
});

// The client's own certificate (replace-cloud-map-notes.txt section 18): the
// client imports it into ACM and the deploy takes its ARN
// (service_cert=imported). It covers the services' names under the client's
// subdomain, so no Private CA; an alarm warns before it expires because
// imported certificates do not renew.
describe("NetworkStack with the client's certificate (service_cert=imported)", () => {
  const CERT_ARN =
    "arn:aws:acm:us-east-1:123456789012:certificate/11111111-2222-3333-4444-555555555555";
  const IMPORTED = {
    service_discovery: "alb",
    service_cert: "imported",
    service_cert_arn: CERT_ARN,
    service_domain: "coa.example.com",
  };
  const expiryAlarms = (template: Template): any[] =>
    Object.values(
      template.findResources("AWS::CloudWatch::Alarm", {
        Properties: { MetricName: "DaysToExpiry" },
      }),
    );

  it("imported: private zone and records under the client's subdomain", () => {
    const template = buildStack(IMPORTED);
    template.hasResourceProperties("AWS::Route53::HostedZone", {
      Name: "coa.example.com.",
    });
    for (const name of ["ontology-engine.coa.example.com.", "vkg.coa.example.com."]) {
      template.hasResourceProperties("AWS::Route53::RecordSet", { Name: name, Type: "A" });
    }
  });

  it("imported: no Private CA, no new certificate, no CA parameter", () => {
    const template = buildStack(IMPORTED);
    template.resourceCountIs("AWS::ACMPCA::CertificateAuthority", 0);
    template.resourceCountIs("AWS::CertificateManager::Certificate", 0);
    expect(
      Object.keys(
        template.findResources("AWS::SSM::Parameter", {
          Properties: { Name: "/coa/services/ca-cert" },
        }),
      ),
    ).toHaveLength(0);
  });

  it("imported: alarm when the certificate has less than 30 days left", () => {
    const [alarm] = expiryAlarms(buildStack(IMPORTED));
    expect(alarm).toBeDefined();
    expect(alarm.Properties).toMatchObject({
      Namespace: "AWS/CertificateManager",
      Threshold: 30,
      ComparisonOperator: "LessThanThreshold",
      Dimensions: [{ Name: "CertificateArn", Value: CERT_ARN }],
    });
  });

  it("imported without service_cert_arn fails synth", () => {
    expect(() =>
      buildStack({
        service_discovery: "alb",
        service_cert: "imported",
        service_domain: "coa.example.com",
      }),
    ).toThrow(/service_cert_arn/);
  });

  it("imported without service_domain fails synth", () => {
    expect(() =>
      buildStack({
        service_discovery: "alb",
        service_cert: "imported",
        service_cert_arn: CERT_ARN,
      }),
    ).toThrow(/service_domain/);
  });

  it("private-ca (default): no expiry alarm (the CA renews its certificate)", () => {
    expect(expiryAlarms(buildStack({ service_discovery: "alb" }))).toHaveLength(0);
  });
});
