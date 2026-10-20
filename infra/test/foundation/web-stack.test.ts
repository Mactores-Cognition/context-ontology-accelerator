// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import * as cdk from "aws-cdk-lib";
import * as ec2 from "aws-cdk-lib/aws-ec2";
import * as fs from "fs";
import * as os from "os";
import * as path from "path";
import { Annotations, Match, Template } from "aws-cdk-lib/assertions";
import { WebStack } from "../../lib/stacks/foundation";
import {
  readRepoVersion,
  buildRuntimeConfig,
} from "../../lib/stacks/foundation/web-stack";
import { DEFAULT_RESOURCE_PREFIX } from "../../lib/constants";

/**
 * WebStack now needs a VPC for the ALB and the nginx service. A throwaway
 * sibling stack keeps each test's VPC isolated; `maxAzs: 2` satisfies the
 * construct's two-public-subnet requirement.
 */
function vpcFor(app: cdk.App, id: string): ec2.IVpc {
  return new ec2.Vpc(new cdk.Stack(app, `${id}Net`), "Vpc", { maxAzs: 2 });
}

const UI_CERT = "arn:aws:acm:us-east-1:123456789012:certificate/ui-cert";

const BASE_PROPS = {
  isCognitoMode: true,
};

/**
 * Regression coverage for the ExplicitAuthFlows drift bug: the
 * UpdateCognitoCallbacks custom resource calls Cognito's updateUserPoolClient,
 * which is a FULL REPLACEMENT of the client's auth flows. If this list doesn't
 * mirror idp-authentication-stack.ts's addClient() config, a dev deploy of the
 * web stack silently strips ALLOW_USER_PASSWORD_AUTH after the auth stack
 * correctly enabled it — breaking integ-test/CLI Cognito sign-in with no
 * CloudFormation error (the custom resource "succeeds").
 */
describe("WebStack (Cognito ExplicitAuthFlows parity)", () => {
  /**
   * The custom resource's `parameters` are serialized as either:
   * - a plain JSON **string** — when `siteUrl` has no runtime token (e.g. a
   *   static custom domain), CDK can fully resolve the value at synth time; or
   * - an `Fn::Join` of string literals + intrinsics (e.g. `Fn::GetAtt` for the
   *   generated ALB domain) — when `siteUrl` depends on a token only
   *   known at deploy time.
   * Neither is safe to `JSON.parse` unconditionally (the Fn::Join form isn't
   * JSON), so normalize both into a searchable string: pass the plain string
   * through as-is, or concatenate only the literal parts of an Fn::Join
   * (skipping intrinsic objects like Fn::GetAtt).
   */
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  function joinedCallLiterals(customResource: any): string {
    const call = (customResource.Properties.Update ??
      customResource.Properties.Create) as
      | string
      | { "Fn::Join"?: [string, unknown[]] };
    if (typeof call === "string") return call;
    const parts = call["Fn::Join"]?.[1] ?? [];
    return parts.filter((p): p is string => typeof p === "string").join("");
  }

  test("dev: UpdateCognitoCallbacks includes ALLOW_USER_PASSWORD_AUTH", () => {
    const app = new cdk.App({
      context: { resource_prefix: DEFAULT_RESOURCE_PREFIX, env: "dev" },
    });
    const template = Template.fromStack(
      new WebStack(app, "TestWebDev", {
        ...BASE_PROPS,
        vpc: vpcFor(app, "TestWebDev"),
      }),
    );

    const customResource = Object.values(
      template.findResources("Custom::AWS"),
    )[0];
    const literals = joinedCallLiterals(customResource);
    expect(literals).toContain(
      '"ExplicitAuthFlows":["ALLOW_USER_SRP_AUTH","ALLOW_REFRESH_TOKEN_AUTH","ALLOW_USER_PASSWORD_AUTH"]',
    );
  });

  test("prod: UpdateCognitoCallbacks does NOT include ALLOW_USER_PASSWORD_AUTH", () => {
    const app = new cdk.App({
      context: { resource_prefix: DEFAULT_RESOURCE_PREFIX, env: "prod" },
    });
    const template = Template.fromStack(
      new WebStack(app, "TestWebProd", {
        ...BASE_PROPS,
        vpc: vpcFor(app, "TestWebProd"),
        // A certificate is required for the patch to run at all: Cognito
        // rejects http callback URLs, so the certless path deliberately skips
        // it outside dev. prod always has a UI domain (bin/app.ts enforces it).
        customDomain: {
          uiDomainName: "app.example.com",
          uiCertificateArn: UI_CERT,
        },
      }),
    );

    const customResource = Object.values(
      template.findResources("Custom::AWS"),
    )[0];
    const literals = joinedCallLiterals(customResource);
    expect(literals).toContain(
      '"ExplicitAuthFlows":["ALLOW_USER_SRP_AUTH","ALLOW_REFRESH_TOKEN_AUTH"]',
    );
    expect(literals).not.toContain("ALLOW_USER_PASSWORD_AUTH");
  });

  test("customDomain still patches Cognito callbacks with the custom UI domain (auth flows unaffected)", () => {
    // NOTE: unlike the pre-customDomain-feature behavior (a bare string that
    // short-circuited all patching), WebStack now always patches Cognito
    // callbacks — customDomain only changes the *siteUrl* used in those
    // callbacks (custom UI domain vs. generated ALB domain). This test
    // guards that ExplicitAuthFlows parity (this suite's actual concern)
    // still holds when a custom domain is configured, not just the default path.
    const app = new cdk.App({
      context: { resource_prefix: DEFAULT_RESOURCE_PREFIX, env: "dev" },
    });
    const template = Template.fromStack(
      new WebStack(app, "TestWebCustomDomain", {
        ...BASE_PROPS,
        vpc: vpcFor(app, "TestWebCustomDomain"),
        customDomain: {
          uiDomainName: "app.example.com",
          uiCertificateArn: UI_CERT,
          apiDomainName: "api.example.com",
          apiCertificateArn:
            "arn:aws:acm:us-east-1:123456789012:certificate/api-cert",
          hostedZoneId: "Z123456ABCDEFG",
        },
      }),
    );

    const customResource = Object.values(
      template.findResources("Custom::AWS"),
    )[0];
    const literals = joinedCallLiterals(customResource);
    expect(literals).toContain("https://app.example.com/authenticate/");
    expect(literals).toContain(
      '"ExplicitAuthFlows":["ALLOW_USER_SRP_AUTH","ALLOW_REFRESH_TOKEN_AUTH","ALLOW_USER_PASSWORD_AUTH"]',
    );
  });
});

describe("WebStack UI WAF WebACL (REGIONAL, on the ALB)", () => {
  test("associates a provided REGIONAL WebACL ARN and creates none", () => {
    const app = new cdk.App({
      context: { resource_prefix: DEFAULT_RESOURCE_PREFIX, env: "dev" },
    });
    const arn =
      "arn:aws:wafv2:us-east-1:123456789012:regional/webacl/byo-ui/def";
    const template = Template.fromStack(
      new WebStack(app, "TestWebByoWaf", {
        ...BASE_PROPS,
        vpc: vpcFor(app, "TestWebByoWaf"),
        webAclArn: arn,
      }),
    );

    template.hasResourceProperties("AWS::WAFv2::WebACLAssociation", {
      WebACLArn: arn,
    });
    template.resourceCountIs("AWS::WAFv2::WebACL", 0);
  });

  test("auto-creates a REGIONAL WebACL in-region, with no cross-region SSM read", () => {
    // The CLOUDFRONT-scope ACL had to live in us-east-1 and was read back via a
    // custom resource. A REGIONAL ACL is local, so that machinery must be gone.
    const app = new cdk.App({
      context: { resource_prefix: DEFAULT_RESOURCE_PREFIX, env: "dev" },
    });
    const template = Template.fromStack(
      new WebStack(app, "TestWebAutoWaf", {
        ...BASE_PROPS,
        vpc: vpcFor(app, "TestWebAutoWaf"),
      }),
    );

    template.hasResourceProperties("AWS::WAFv2::WebACL", { Scope: "REGIONAL" });
    template.resourceCountIs("AWS::WAFv2::WebACLAssociation", 1);

    const readers = Object.values(template.findResources("Custom::AWS")).filter(
      (cr) => {
        const call = cr.Properties.Update ?? cr.Properties.Create;
        return typeof call === "string" && call.includes("getParameter");
      },
    );
    expect(readers).toHaveLength(0);
  });
});

describe("WebStack (no CloudFront remains)", () => {
  test("synthesizes no CloudFront resources at all", () => {
    const app = new cdk.App({
      context: { resource_prefix: DEFAULT_RESOURCE_PREFIX, env: "dev" },
    });
    const template = Template.fromStack(
      new WebStack(app, "TestWebNoCf", {
        ...BASE_PROPS,
        vpc: vpcFor(app, "TestWebNoCf"),
      }),
    );

    const cloudfront = Object.keys(template.toJSON().Resources ?? {}).filter(
      (k) =>
        String(template.toJSON().Resources[k].Type).startsWith(
          "AWS::CloudFront::",
        ),
    );
    expect(cloudfront).toEqual([]);
  });
});

describe("readRepoVersion (VERSION → runtime-config)", () => {
  test("returns the trimmed, bare-semver contents of the repo-root VERSION file", () => {
    // Independently resolve the monorepo root (dir containing pnpm-workspace.yaml)
    // so the test does not hardcode a version that VERSION bumps will break.
    let dir = __dirname;
    while (!fs.existsSync(path.join(dir, "pnpm-workspace.yaml"))) {
      const parent = path.dirname(dir);
      if (parent === dir) throw new Error("monorepo root not found");
      dir = parent;
    }
    const expected = fs.readFileSync(path.join(dir, "VERSION"), "utf-8").trim();

    const version = readRepoVersion();
    expect(version).toBe(expected);
    // The value is interpolated into the UI badge as `v{version}`, so it must
    // be a bare semver (no leading "v", no stray whitespace).
    expect(version).toMatch(/^\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?$/);
  });

  test("returns undefined when the VERSION file is missing (deploy is never blocked)", () => {
    const missing = path.join(
      fs.mkdtempSync(path.join(os.tmpdir(), "ver-")),
      "VERSION",
    );
    expect(readRepoVersion(missing)).toBeUndefined();
  });

  test("returns undefined for an empty or whitespace-only VERSION file", () => {
    const dir = fs.mkdtempSync(path.join(os.tmpdir(), "ver-"));
    const empty = path.join(dir, "VERSION");
    fs.writeFileSync(empty, "   \n\t ");
    expect(readRepoVersion(empty)).toBeUndefined();
  });
});

describe("buildRuntimeConfig (version include/omit)", () => {
  const base = {
    region: "us-east-1",
    stage: "dev",
    authority: "https://issuer",
    clientId: "abc",
  };

  test("includes version when provided", () => {
    expect(buildRuntimeConfig({ ...base, version: "0.2.3" })).toMatchObject({
      version: "0.2.3",
    });
  });

  test("omits the version key entirely when undefined", () => {
    expect(buildRuntimeConfig(base)).not.toHaveProperty("version");
  });

  test("omits the version key when empty", () => {
    expect(buildRuntimeConfig({ ...base, version: "" })).not.toHaveProperty(
      "version",
    );
  });

  test("omits apiEndpoint and serveRuntimeArn when not provided", () => {
    const cfg = buildRuntimeConfig(base);
    expect(cfg).not.toHaveProperty("apiEndpoint");
    expect(cfg).not.toHaveProperty("serveRuntimeArn");
  });
});

/**
 * Reads a container environment variable off the single task definition.
 * Values carrying SSM/cross-stack Refs render as an Fn::Join, so return the
 * raw value and let callers normalize.
 */
// eslint-disable-next-line @typescript-eslint/no-explicit-any
function containerEnv(template: Template, name: string): any {
  const taskDef = Object.values(
    template.findResources("AWS::ECS::TaskDefinition"),
  )[0];
  const entry = taskDef.Properties.ContainerDefinitions[0].Environment.find(
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    (e: any) => e.Name === name,
  );
  expect(entry).toBeDefined();
  return entry.Value;
}

/** Concatenate the literal parts of a plain string or an Fn::Join. */
// eslint-disable-next-line @typescript-eslint/no-explicit-any
function joinLiterals(value: any): string {
  if (typeof value === "string") return value;
  const parts = value["Fn::Join"]?.[1] ?? [];
  return parts.filter((p: unknown): p is string => typeof p === "string").join("");
}

describe("WebStack RUNTIME_CONFIG_JSON (end-to-end)", () => {
  test("carries the repo VERSION to the container", () => {
    // Previously asserted against the BucketDeployment asset on disk. The SPA
    // is now baked into an image and the config arrives as an env var, so the
    // env var IS the artifact.
    let dir = __dirname;
    while (!fs.existsSync(path.join(dir, "pnpm-workspace.yaml"))) {
      dir = path.dirname(dir);
    }
    const expected = fs.readFileSync(path.join(dir, "VERSION"), "utf-8").trim();

    const app = new cdk.App({
      context: { resource_prefix: DEFAULT_RESOURCE_PREFIX, env: "dev" },
    });
    const template = Template.fromStack(
      new WebStack(app, "TestWebArtifact", {
        isCognitoMode: true,
        vpc: vpcFor(app, "TestWebArtifact"),
      }),
    );

    expect(joinLiterals(containerEnv(template, "RUNTIME_CONFIG_JSON"))).toContain(
      `"version":"${expected}"`,
    );
  });

  test("resolves authority and clientId through CFN refs, not literals", () => {
    const app = new cdk.App({
      context: { resource_prefix: DEFAULT_RESOURCE_PREFIX, env: "dev" },
    });
    const template = Template.fromStack(
      new WebStack(app, "TestWebArtifactRefs", {
        isCognitoMode: true,
        vpc: vpcFor(app, "TestWebArtifactRefs"),
      }),
    );

    const value = containerEnv(template, "RUNTIME_CONFIG_JSON");
    // An Fn::Join, not a plain string: the SSM-backed values are Refs.
    expect(typeof value).not.toBe("string");
    const literals = joinLiterals(value);
    expect(literals).toContain('"authority":"');
    expect(literals).toContain('"clientId":"');
  });
});

// ═══════════════════════════════════════════════════════════════════
// WebStack — CSP allowlists the Cognito hosted-UI domain (issue #130)
// ═══════════════════════════════════════════════════════════════════
describe("WebStack (CSP hosted-UI origin)", () => {
  /** The CSP handed to nginx via the container's CSP_HEADER env var. */
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  function cspOf(template: Template): any {
    return containerEnv(template, "CSP_HEADER");
  }

  test("the CSP is an Fn::Join carrying the hosted-UI host, resolved from SSM", () => {
    // End-to-end for the fix: the prefix arrives as a CFN dynamic reference, so
    // the header cannot be a plain string — it must be a Join whose literal parts
    // include the hosted-UI suffix around the resolved prefix. A plain string here
    // would mean the origin was dropped at synth, which is the bug.
    const app = new cdk.App({
      context: { resource_prefix: DEFAULT_RESOURCE_PREFIX, env: "prod" },
    });
    const template = Template.fromStack(
      new WebStack(app, "TestWebCsp", {
        isCognitoMode: true,
        vpc: vpcFor(app, "TestWebCsp"),
      }),
    );

    const csp = cspOf(template);
    expect(typeof csp).not.toBe("string");
    const literals: string[] = (csp["Fn::Join"][1] as unknown[]).filter(
      (p): p is string => typeof p === "string",
    );
    const joined = literals.join("");
    expect(joined).toContain(`.auth.`);
    expect(joined).toContain(`.amazoncognito.com`);
    // Both directives, not just connect-src: silent renew uses an iframe.
    expect(joined).toContain("connect-src");
    expect(joined).toContain("frame-src");
    // The exact prefix, never a wildcard host.
    expect(joined).not.toContain("*.auth.");
  });

  test("reads the hosted-UI prefix from the auth stack's parameter", () => {
    const app = new cdk.App({
      context: { resource_prefix: DEFAULT_RESOURCE_PREFIX, env: "prod" },
    });
    const template = Template.fromStack(
      new WebStack(app, "TestWebCspParam", {
        isCognitoMode: true,
        vpc: vpcFor(app, "TestWebCspParam"),
      }),
    );

    // `valueForStringParameter` renders as a CFN template Parameter of type
    // AWS::SSM::Parameter::Value<String> whose Default is the path, with the CSP
    // holding only a Ref to it — so the path is asserted here, not in the header.
    // A rename on either side fails here rather than at sign-in time.
    const parameters = template.toJSON().Parameters ?? {};
    const defaults = Object.values(parameters).map(
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      (p: any) => p.Default,
    );
    expect(defaults).toContain(
      `/${DEFAULT_RESOURCE_PREFIX}/cognito-domain-prefix`,
    );

    // And the CSP really does Ref that parameter, rather than inlining a literal.
    const referenced = Object.entries(parameters)
      .filter(
        // eslint-disable-next-line @typescript-eslint/no-explicit-any
        ([, p]: [string, any]) =>
          p.Default === `/${DEFAULT_RESOURCE_PREFIX}/cognito-domain-prefix`,
      )
      .map(([logicalId]) => logicalId);
    expect(referenced.length).toBe(1);
    expect(JSON.stringify(cspOf(template))).toContain(referenced[0]);
  });

  test("direct-OIDC mode neither reads the parameter nor widens the CSP", () => {
    const app = new cdk.App({
      context: { resource_prefix: DEFAULT_RESOURCE_PREFIX, env: "prod" },
    });
    const template = Template.fromStack(
      new WebStack(app, "TestWebCspOidc", {
        isCognitoMode: false,
        vpc: vpcFor(app, "TestWebCspOidc"),
      }),
    );

    const csp = JSON.stringify(cspOf(template));
    expect(csp).not.toContain("cognito-domain-prefix");
    expect(csp).not.toContain("amazoncognito.com");
  });

  test("the content_security_policy context key replaces the derived policy", () => {
    // The override prop existed but reached nothing, so an operator hitting a CSP
    // gap had to patch CDK source. Wired now — this asserts the wiring, not the
    // prop's existence.
    const custom = "default-src 'none'; script-src 'self'";
    const app = new cdk.App({
      context: {
        resource_prefix: DEFAULT_RESOURCE_PREFIX,
        env: "prod",
      },
    });
    const template = Template.fromStack(
      new WebStack(app, "TestWebCspOverride", {
        isCognitoMode: true,
        vpc: vpcFor(app, "TestWebCspOverride"),
        contentSecurityPolicy: custom,
      }),
    );

    expect(cspOf(template)).toBe(custom);
  });
});

// ═══════════════════════════════════════════════════════════════════
// WebStack — HTTPS vs plain-HTTP listener modes
// ═══════════════════════════════════════════════════════════════════
describe("WebStack listener modes", () => {
  function render(id: string, cert: boolean, env = "dev"): Template {
    const app = new cdk.App({
      context: { resource_prefix: DEFAULT_RESOURCE_PREFIX, env },
    });
    return Template.fromStack(
      new WebStack(app, id, {
        ...BASE_PROPS,
        vpc: vpcFor(app, id),
        ...(cert && {
          customDomain: {
            uiDomainName: "app.example.com",
            uiCertificateArn: UI_CERT,
          },
        }),
      }),
    );
  }

  test("with a cert: HTTPS listener pinned to a modern SSL policy", () => {
    // CDK defaults to ELBSecurityPolicy-2016-08, which still allows TLS 1.0/1.1
    // — a downgrade from CloudFront's TLS_V1_2_2021.
    const template = render("TestWebHttps", true);
    template.hasResourceProperties(
      "AWS::ElasticLoadBalancingV2::Listener",
      Match.objectLike({
        Port: 443,
        Protocol: "HTTPS",
        Certificates: [{ CertificateArn: UI_CERT }],
        SslPolicy: Match.stringLikeRegexp("TLS13"),
      }),
    );
  });

  test("with a cert: port 80 redirects to HTTPS", () => {
    const template = render("TestWebRedirect", true);
    template.hasResourceProperties(
      "AWS::ElasticLoadBalancingV2::Listener",
      Match.objectLike({
        Port: 80,
        DefaultActions: Match.arrayWith([
          Match.objectLike({
            Type: "redirect",
            RedirectConfig: Match.objectLike({
              Protocol: "HTTPS",
              Port: "443",
              StatusCode: "HTTP_301",
            }),
          }),
        ]),
      }),
    );
  });

  test("without a cert: a single port-80 forwarding listener, no certificate", () => {
    const template = render("TestWebHttp", false);
    template.resourceCountIs("AWS::ElasticLoadBalancingV2::Listener", 1);
    template.hasResourceProperties(
      "AWS::ElasticLoadBalancingV2::Listener",
      Match.objectLike({
        Port: 80,
        Protocol: "HTTP",
        Certificates: Match.absent(),
        DefaultActions: Match.arrayWith([
          Match.objectLike({ Type: "forward" }),
        ]),
      }),
    );
  });

  test("without a cert: the CSP drops upgrade-insecure-requests", () => {
    // On an http:// document that directive rewrites every asset request to
    // https:// against a listener with no TLS, so the page loads nothing.
    const http = render("TestWebHttpCsp", false);
    expect(joinLiterals(containerEnv(http, "CSP_HEADER"))).not.toContain(
      "upgrade-insecure-requests",
    );
    expect(containerEnv(http, "INSECURE_TRANSPORT")).toBe("1");

    const https = render("TestWebHttpsCsp", true);
    expect(joinLiterals(containerEnv(https, "CSP_HEADER"))).toContain(
      "upgrade-insecure-requests",
    );
  });

  test("without a cert: warns that HTTP is in use", () => {
    const app = new cdk.App({
      context: { resource_prefix: DEFAULT_RESOURCE_PREFIX, env: "dev" },
    });
    const stack = new WebStack(app, "TestWebHttpWarn", {
      ...BASE_PROPS,
      vpc: vpcFor(app, "TestWebHttpWarn"),
    });
    Annotations.fromStack(stack).hasWarning(
      "*",
      Match.stringLikeRegexp("plain HTTP"),
    );
  });

  test("the ALB is internet-facing and drops invalid headers", () => {
    const template = render("TestWebScheme", true);
    template.hasResourceProperties(
      "AWS::ElasticLoadBalancingV2::LoadBalancer",
      Match.objectLike({
        Scheme: "internet-facing",
        LoadBalancerAttributes: Match.arrayWith([
          {
            Key: "routing.http.drop_invalid_header_fields.enabled",
            Value: "true",
          },
        ]),
      }),
    );
  });

  test("the target group health-checks /health with a short dereg delay", () => {
    const template = render("TestWebTg", true);
    template.hasResourceProperties(
      "AWS::ElasticLoadBalancingV2::TargetGroup",
      Match.objectLike({
        Port: 8080,
        Protocol: "HTTP",
        TargetType: "ip",
        HealthCheckPath: "/health",
        TargetGroupAttributes: Match.arrayWith([
          { Key: "deregistration_delay.timeout_seconds", Value: "10" },
        ]),
      }),
    );
  });
});

// ═══════════════════════════════════════════════════════════════════
// WebStack — Cognito callbacks cannot be http:// at a non-localhost origin
// ═══════════════════════════════════════════════════════════════════
describe("WebStack Cognito callbacks by transport", () => {
  function cognitoCall(template: Template): string | undefined {
    const resources = Object.values(template.findResources("Custom::AWS"))
      .map((cr) => cr.Properties.Update ?? cr.Properties.Create)
      .map((call) =>
        typeof call === "string"
          ? call
          : // eslint-disable-next-line @typescript-eslint/no-explicit-any
            ((call as any)["Fn::Join"]?.[1] ?? [])
              .filter((p: unknown): p is string => typeof p === "string")
              .join(""),
      );
    return resources.find((c) => c.includes("updateUserPoolClient"));
  }

  test("HTTP + dev registers localhost only, never the http ALB origin", () => {
    // Cognito rejects non-localhost http callback URLs, so sending the ALB name
    // fails the deploy with InvalidParameterException. Guard against that.
    const app = new cdk.App({
      context: { resource_prefix: DEFAULT_RESOURCE_PREFIX, env: "dev" },
    });
    const template = Template.fromStack(
      new WebStack(app, "TestWebCbHttpDev", {
        ...BASE_PROPS,
        vpc: vpcFor(app, "TestWebCbHttpDev"),
      }),
    );

    const call = cognitoCall(template);
    expect(call).toBeDefined();
    expect(call).toContain("http://localhost:5173/authenticate/");
    expect(call).not.toContain("elb.amazonaws.com");
  });

  test("HTTP + non-dev skips the patch rather than sending empty CallbackURLs", () => {
    // Cognito also rejects an empty CallbackURLs when AllowedOAuthFlows is set.
    const app = new cdk.App({
      context: { resource_prefix: DEFAULT_RESOURCE_PREFIX, env: "test" },
    });
    const template = Template.fromStack(
      new WebStack(app, "TestWebCbHttpTest", {
        ...BASE_PROPS,
        vpc: vpcFor(app, "TestWebCbHttpTest"),
      }),
    );

    expect(cognitoCall(template)).toBeUndefined();
  });

  test("HTTPS registers the custom domain callback", () => {
    const app = new cdk.App({
      context: { resource_prefix: DEFAULT_RESOURCE_PREFIX, env: "dev" },
    });
    const template = Template.fromStack(
      new WebStack(app, "TestWebCbHttps", {
        ...BASE_PROPS,
        vpc: vpcFor(app, "TestWebCbHttps"),
        customDomain: {
          uiDomainName: "app.example.com",
          uiCertificateArn: UI_CERT,
        },
      }),
    );

    expect(cognitoCall(template)).toContain(
      "https://app.example.com/authenticate/",
    );
  });
});
