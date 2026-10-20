// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import * as cdk from "aws-cdk-lib";
import * as ec2 from "aws-cdk-lib/aws-ec2";
import * as ecs from "aws-cdk-lib/aws-ecs";
import * as elbv2 from "aws-cdk-lib/aws-elasticloadbalancingv2";
import * as logs from "aws-cdk-lib/aws-logs";
import * as s3 from "aws-cdk-lib/aws-s3";
import * as wafv2 from "aws-cdk-lib/aws-wafv2";
import { Construct } from "constructs";
import { RuntimeConfig } from "@coa/shared";
import type { IAlarmActionStrategy } from "cdk-monitoring-constructs/lib/common/alarm/action";

import { AccessLogsBucket } from "./access-logs-bucket";
import { buildContentSecurityPolicy } from "./content-security-policy";
import { SCLConstruct } from "./scl-construct";
import { WafWebAcl } from "./waf-web-acl";
import { SclMonitoring } from "./monitoring";
import { resolveContext } from "../context";
import { Paths } from "../paths";

/** Port nginx listens on; non-root cannot bind below 1024. */
const CONTAINER_PORT = 8080;

export interface WebAlbConstructProps {
  /** VPC for the ALB (public subnets) and the service (private subnets). */
  readonly vpc: ec2.IVpc;
  /** Runtime config handed to the container as RUNTIME_CONFIG_JSON. */
  readonly runtimeConfig: RuntimeConfig;
  /**
   * Custom domain for the UI. With {@link uiCertificateArn} this selects the
   * HTTPS listener; omitting both serves plain HTTP (dev only).
   */
  readonly uiDomainName?: string;
  /** ACM certificate ARN for {@link uiDomainName}, in the deploy region. */
  readonly uiCertificateArn?: string;
  /** Fully custom CSP header value, replacing the derived one. */
  readonly contentSecurityPolicy?: string;
  /** Cognito hosted-UI domain prefix, when the deployment has a user pool. */
  readonly cognitoDomainPrefix?: string;
  /** REGIONAL WebACL ARN to associate. A WebACL is created when omitted. */
  readonly webAclArn?: string;
  /** Fargate CPU units. @default 256 */
  readonly cpu?: number;
  /** Fargate memory in MiB. @default 512 */
  readonly memoryLimitMiB?: number;
  /** Alarm action strategy for monitoring alarms. */
  readonly alarmAction?: IAlarmActionStrategy;
}

/**
 * Internet-facing ALB in front of an nginx Fargate service serving the SPA.
 *
 * Replaces the CloudFront + S3 distribution: TLS terminates on the ALB with an
 * operator-supplied certificate, the SPA is baked into the container image, and
 * security headers (including the CSP) are emitted by nginx rather than a
 * CloudFront response-headers policy.
 */
export class WebAlbConstruct extends SCLConstruct {
  public readonly loadBalancer: elbv2.ApplicationLoadBalancer;
  public readonly service: ecs.FargateService;
  /** Bucket for operator drop-ins (questions/), synced in at task start. */
  public readonly contentBucket: s3.Bucket;
  /** Origin the browser is served from; `http://` when no cert is configured. */
  public readonly siteUrl: string;
  /** True when serving over plain HTTP. */
  public readonly insecureTransport: boolean;

  constructor(scope: Construct, id: string, props: WebAlbConstructProps) {
    super(scope, id);

    const stack = cdk.Stack.of(this);
    const { envName } = resolveContext(this.node);
    const isProd = envName === "prod";
    const vpc = props.vpc;

    const certificateArn = props.uiCertificateArn;
    this.insecureTransport = !certificateArn;

    // An imported VPC (-c vpc_id) may have no IGW-routed subnets. CDK's own
    // error here is opaque, and the two-AZ requirement otherwise surfaces only
    // at CreateLoadBalancer.
    // vpc.publicSubnets, not selectSubnets: the latter throws its own opaque
    // "no 'Public' subnet groups" error before this check can run.
    if (vpc.publicSubnets.length < 2) {
      throw new Error(
        "The internet-facing UI ALB needs public subnets in at least 2 AZs; this VPC has " +
          `${vpc.publicSubnets.length}. When importing a VPC with -c vpc_id, it must have ` +
          "IGW-routed subnets in two Availability Zones.",
      );
    }

    // ── Security groups ────────────────────────────────────────────────
    // Deliberately not NetworkStack's shared ecsSecurityGroup: VkgStack opens
    // 8080 on it to the whole VPC CIDR, which would expose this service too.
    const albSecurityGroup = new ec2.SecurityGroup(this, "AlbSg", {
      vpc,
      securityGroupName: this.prefixed("web-alb-sg"),
      description: "UI load balancer",
      allowAllOutbound: false,
    });
    const serviceSecurityGroup = new ec2.SecurityGroup(this, "ServiceSg", {
      vpc,
      securityGroupName: this.prefixed("web-svc-sg"),
      description: "UI nginx tasks",
      allowAllOutbound: false,
    });

    albSecurityGroup.addIngressRule(
      ec2.Peer.anyIpv4(),
      ec2.Port.tcp(443),
      "HTTPS from the internet",
    );
    albSecurityGroup.addIngressRule(
      ec2.Peer.anyIpv4(),
      ec2.Port.tcp(80),
      this.insecureTransport
        ? "HTTP from the internet (no certificate configured)"
        : "HTTP from the internet, redirected to HTTPS",
    );
    // The ALB->task rules (egress on the ALB SG, ingress on the service SG) are
    // created by CDK when the target group registers the service. Adding them
    // by hand too would duplicate them and fail with InvalidPermission.Duplicate.

    // ECR, CloudWatch Logs and the questions/ sync.
    serviceSecurityGroup.addEgressRule(
      ec2.Peer.anyIpv4(),
      ec2.Port.tcp(443),
      "ECR, CloudWatch Logs, S3",
    );

    // ── Operator content bucket ───────────────────────────────────────
    this.contentBucket = new s3.Bucket(this, "ContentBucket", {
      bucketName: `${this.prefixed("ui-content")}-${stack.account}-${stack.region}`,
      blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
      encryption: s3.BucketEncryption.S3_MANAGED,
      enforceSSL: true,
      versioned: false,
      removalPolicy: isProd
        ? cdk.RemovalPolicy.RETAIN
        : cdk.RemovalPolicy.DESTROY,
      autoDeleteObjects: !isProd,
    });

    // ── Load balancer ─────────────────────────────────────────────────
    const albAccessLogs = new AccessLogsBucket(this, "AlbAccessLogs", {
      nameSuffix: "alb-logs",
    });

    this.loadBalancer = new elbv2.ApplicationLoadBalancer(this, "Alb", {
      loadBalancerName: this.prefixed("web-alb"),
      vpc,
      internetFacing: true,
      vpcSubnets: { subnetType: ec2.SubnetType.PUBLIC },
      securityGroup: albSecurityGroup,
      dropInvalidHeaderFields: true,
      // IPv4 only: a dual-stack ALB requires an IPv6 CIDR on the VPC, which
      // NetworkStack does not allocate. WebStack creates an A record only.
    });
    this.loadBalancer.logAccessLogs(albAccessLogs.bucket, "alb");

    this.siteUrl = props.uiDomainName
      ? `https://${props.uiDomainName}`
      : `${this.insecureTransport ? "http" : "https"}://${this.loadBalancer.loadBalancerDnsName}`;

    // ── Task definition ───────────────────────────────────────────────
    const logGroup = new logs.LogGroup(this, "LogGroup", {
      logGroupName: `/ecs/${this.prefixed("web")}`,
      retention: logs.RetentionDays.ONE_MONTH,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
    });

    const taskDef = new ecs.FargateTaskDefinition(this, "TaskDef", {
      family: this.prefixed("web"),
      cpu: props.cpu ?? 256,
      memoryLimitMiB: props.memoryLimitMiB ?? 512,
      runtimePlatform: {
        cpuArchitecture: ecs.CpuArchitecture.ARM64,
        operatingSystemFamily: ecs.OperatingSystemFamily.LINUX,
      },
    });

    // Pre-built image from CI, else a local Docker build.
    const webAppImageUri = this.node.tryGetContext("web_app_image_uri") as
      | string
      | undefined;
    const ecrRepositoryArn = this.node.tryGetContext("ecr_repository_arn") as
      | string
      | undefined;
    const ecrRepositoryName = this.node.tryGetContext("ecr_repository_name") as
      | string
      | undefined;

    let containerImage: ecs.ContainerImage;
    if (webAppImageUri && ecrRepositoryArn && ecrRepositoryName) {
      const ecrRepo = cdk.aws_ecr.Repository.fromRepositoryAttributes(
        this,
        "WebAppEcrRepo",
        { repositoryArn: ecrRepositoryArn, repositoryName: ecrRepositoryName },
      );
      containerImage = ecs.ContainerImage.fromEcrRepository(
        ecrRepo,
        webAppImageUri.split(":").pop() || "latest",
      );
    } else {
      containerImage = ecs.ContainerImage.fromAsset(Paths.webApp, {
        platform: cdk.aws_ecr_assets.Platform.LINUX_ARM64,
      });
    }

    // toJsonString keeps the SSM/cross-stack Refs intact inside the JSON string.
    // Nothing secret may enter RuntimeConfig: this is readable via
    // ecs:DescribeTaskDefinition and is served to the browser regardless.
    taskDef.addContainer("Nginx", {
      containerName: this.prefixed("web"),
      image: containerImage,
      environment: {
        RUNTIME_CONFIG_JSON: stack.toJsonString(props.runtimeConfig),
        CSP_HEADER: buildContentSecurityPolicy(
          props.runtimeConfig,
          props.contentSecurityPolicy,
          props.cognitoDomainPrefix,
          this.insecureTransport,
        ),
        UI_CONTENT_BUCKET: this.contentBucket.bucketName,
        ...(this.insecureTransport && { INSECURE_TRANSPORT: "1" }),
      },
      portMappings: [
        { containerPort: CONTAINER_PORT, protocol: ecs.Protocol.TCP },
      ],
      logging: ecs.LogDrivers.awsLogs({ streamPrefix: "web", logGroup }),
    });

    // grantRead also grants the ListBucket that `aws s3 sync` needs.
    this.contentBucket.grantRead(taskDef.taskRole, "questions/*");

    // ── Service ───────────────────────────────────────────────────────
    const cluster = new ecs.Cluster(this, "Cluster", {
      clusterName: this.prefixed("web-cluster"),
      vpc,
    });

    this.service = new ecs.FargateService(this, "Service", {
      serviceName: this.prefixed("web"),
      cluster,
      taskDefinition: taskDef,
      desiredCount: isProd ? 2 : 1,
      vpcSubnets: { subnetType: ec2.SubnetType.PRIVATE_WITH_EGRESS },
      securityGroups: [serviceSecurityGroup],
      assignPublicIp: false,
      circuitBreaker: { rollback: true },
      minHealthyPercent: 100,
      maxHealthyPercent: 200,
      // Required with a target group attached, or ECS can kill tasks before
      // registration completes.
      healthCheckGracePeriod: cdk.Duration.seconds(30),
    });

    const targetGroup = new elbv2.ApplicationTargetGroup(this, "TargetGroup", {
      targetGroupName: this.prefixed("web-tg"),
      vpc,
      port: CONTAINER_PORT,
      protocol: elbv2.ApplicationProtocol.HTTP,
      targetType: elbv2.TargetType.IP,
      targets: [
        this.service.loadBalancerTarget({
          containerName: this.prefixed("web"),
          containerPort: CONTAINER_PORT,
        }),
      ],
      // The 300s default adds five minutes to every deploy of a static server.
      deregistrationDelay: cdk.Duration.seconds(10),
      healthCheck: {
        path: "/health",
        healthyHttpCodes: "200",
        interval: cdk.Duration.seconds(15),
        timeout: cdk.Duration.seconds(5),
        healthyThresholdCount: 2,
      },
    });

    // ── Listeners ─────────────────────────────────────────────────────
    if (certificateArn) {
      this.loadBalancer.addListener("Https", {
        port: 443,
        protocol: elbv2.ApplicationProtocol.HTTPS,
        certificates: [elbv2.ListenerCertificate.fromArn(certificateArn)],
        // CDK defaults to ELBSecurityPolicy-2016-08, which still allows
        // TLS 1.0/1.1 — a downgrade from CloudFront's TLS_V1_2_2021.
        sslPolicy: elbv2.SslPolicy.RECOMMENDED_TLS13,
        defaultTargetGroups: [targetGroup],
      });
      this.loadBalancer.addListener("HttpRedirect", {
        port: 80,
        protocol: elbv2.ApplicationProtocol.HTTP,
        defaultAction: elbv2.ListenerAction.redirect({
          protocol: "HTTPS",
          port: "443",
          permanent: true,
        }),
      });
    } else {
      this.loadBalancer.addListener("Http", {
        port: 80,
        protocol: elbv2.ApplicationProtocol.HTTP,
        defaultTargetGroups: [targetGroup],
      });
      cdk.Annotations.of(this).addWarning(
        "The UI is served over plain HTTP: no ui_cert_arn/ui_domain was configured. " +
          "Cognito sign-in is unavailable at this origin (Cognito requires https callback " +
          "URLs except for localhost), so use `pnpm dev` on localhost:5173 for auth testing. " +
          "Set -c ui_domain=<host> -c ui_cert_arn=<deploy-region ACM ARN> for HTTPS.",
      );
    }

    // ── WAF ───────────────────────────────────────────────────────────
    const webAclArn =
      props.webAclArn ??
      new WafWebAcl(this, "UiWaf", { scope: "REGIONAL", namePrefix: "ui" })
        .webAclArn;
    new wafv2.CfnWebACLAssociation(this, "UiWafAssociation", {
      resourceArn: this.loadBalancer.loadBalancerArn,
      webAclArn,
    });

    // ── Monitoring ────────────────────────────────────────────────────
    new SclMonitoring(this, "Monitoring", {
      alarmNamePrefix: this.prefixed("web"),
      ...(props.alarmAction && { alarmAction: props.alarmAction }),
    })
      .monitorAlb(this.loadBalancer, "ui")
      .monitorFargateService(this.service);

    // ── Outputs ───────────────────────────────────────────────────────
    new cdk.CfnOutput(this, "LoadBalancerDnsName", {
      value: this.loadBalancer.loadBalancerDnsName,
      description: "UI load balancer DNS name",
    });
    new cdk.CfnOutput(this, "WebsiteURL", {
      value: this.siteUrl,
      description: "Website URL",
    });
    new cdk.CfnOutput(this, "UiContentBucketName", {
      value: this.contentBucket.bucketName,
      description:
        "Bucket for operator UI drop-ins (questions/<namespaceId>.json)",
    });
  }
}
