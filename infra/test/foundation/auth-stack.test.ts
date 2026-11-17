// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import * as cdk from "aws-cdk-lib";
import * as cognito from "aws-cdk-lib/aws-cognito";
import { Template, Match } from "aws-cdk-lib/assertions";
import { IdpAuthenticationStack } from "../../lib/stacks/foundation";
import { IdpType, SsmConfig } from "../../lib/types";
import { DEFAULT_RESOURCE_PREFIX, DEFAULT_ENV } from "../../lib/constants";

const TEST_CONTEXT = {
  resource_prefix: DEFAULT_RESOURCE_PREFIX,
  env: DEFAULT_ENV,
};

// ═══════════════════════════════════════════════════════════════════
// IdpAuthenticationStack — Cognito mode (default)
// ═══════════════════════════════════════════════════════════════════
describe("IdpAuthenticationStack (Cognito)", () => {
  let template: Template;

  beforeAll(() => {
    const app = new cdk.App({ context: TEST_CONTEXT });
    template = Template.fromStack(new IdpAuthenticationStack(app, "TestAuth"));
  });

  test("creates Cognito User Pool", () => {
    template.hasResourceProperties("AWS::Cognito::UserPool", {
      UserPoolName: `${DEFAULT_RESOURCE_PREFIX}-${DEFAULT_ENV}-user-pool`,
      AdminCreateUserConfig: { AllowAdminCreateUserOnly: true },
      Policies: { PasswordPolicy: { MinimumLength: 12 } },
    });
  });

  test("creates User Pool Client with OAuth and default refresh token", () => {
    template.hasResourceProperties("AWS::Cognito::UserPoolClient", {
      AllowedOAuthFlows: ["code"],
      AllowedOAuthScopes: ["openid", "email", "profile"],
      RefreshTokenValidity: 720,
      TokenValidityUnits: Match.objectLike({ RefreshToken: "minutes" }),
    });
  });

  test("creates SSM params including userpool-id", () => {
    template.hasResourceProperties("AWS::SSM::Parameter", {
      Name: `/${DEFAULT_RESOURCE_PREFIX}/userpool-id`,
    });
    template.hasResourceProperties("AWS::SSM::Parameter", {
      Name: `/${DEFAULT_RESOURCE_PREFIX}/userpool-client-id`,
    });
    template.hasResourceProperties("AWS::SSM::Parameter", {
      Name: `/${DEFAULT_RESOURCE_PREFIX}/issuer`,
    });
    template.hasResourceProperties("AWS::SSM::Parameter", {
      Name: `/${DEFAULT_RESOURCE_PREFIX}/authentication-group-token-name`,
    });
  });

  test("does not create Secrets Manager secret", () => {
    template.resourceCountIs("AWS::SecretsManager::Secret", 0);
  });
});

// ═══════════════════════════════════════════════════════════════════
// IdpAuthenticationStack — OIDC mode
// ═══════════════════════════════════════════════════════════════════
describe("IdpAuthenticationStack (OIDC)", () => {
  let template: Template;

  beforeAll(() => {
    const app = new cdk.App({ context: TEST_CONTEXT });
    template = Template.fromStack(
      new IdpAuthenticationStack(app, "TestAuthOidc", {
        idpType: IdpType.OIDC,
        oidcSettings: {
          issuerUrl: "https://dev-123.okta.com/oauth2/default",
          clientId: "0oa1bcdef",
          groupClaim: "groups",
        },
      }),
    );
  });

  test("does NOT create Cognito User Pool", () => {
    template.resourceCountIs("AWS::Cognito::UserPool", 0);
    template.resourceCountIs("AWS::Cognito::UserPoolClient", 0);
  });

  test("stores external issuer in SSM", () => {
    template.hasResourceProperties("AWS::SSM::Parameter", {
      Name: `/${DEFAULT_RESOURCE_PREFIX}/issuer`,
      Value: "https://dev-123.okta.com/oauth2/default",
    });
  });

  test("stores external clientId in SSM", () => {
    template.hasResourceProperties("AWS::SSM::Parameter", {
      Name: `/${DEFAULT_RESOURCE_PREFIX}/userpool-client-id`,
      Value: "0oa1bcdef",
    });
  });

  test("stores external clientId as the MCP client ID in SSM", () => {
    // External OIDC reuses one client ID for both web and MCP flows (see
    // idp-authentication-stack.ts). serve-stack.ts and mcp-stack.ts both
    // read /mcp-client-id unconditionally, regardless of idpType — this
    // parameter must exist on every path or their SSM lookup 404s at
    // deploy time (`Unable to fetch parameters [/mcp-client-id] ...`).
    template.hasResourceProperties("AWS::SSM::Parameter", {
      Name: `/${DEFAULT_RESOURCE_PREFIX}/mcp-client-id`,
      Value: "0oa1bcdef",
    });
  });

  test("stores group claim in SSM", () => {
    template.hasResourceProperties("AWS::SSM::Parameter", {
      Name: `/${DEFAULT_RESOURCE_PREFIX}/authentication-group-token-name`,
      Value: "groups",
    });
  });
});

// ═══════════════════════════════════════════════════════════════════
// IdpAuthenticationStack — OIDC with jwksUri
// ═══════════════════════════════════════════════════════════════════
describe("IdpAuthenticationStack (OIDC with jwksUri)", () => {
  test("stores jwksUri in SSM when provided", () => {
    const app = new cdk.App({ context: TEST_CONTEXT });
    const template = Template.fromStack(
      new IdpAuthenticationStack(app, "OidcJwks", {
        idpType: IdpType.OIDC,
        oidcSettings: {
          issuerUrl: "https://idp.example.com",
          clientId: "abc123",
          jwksUri: "https://idp.example.com/.well-known/jwks.json",
        },
      }),
    );

    template.hasResourceProperties("AWS::SSM::Parameter", {
      Name: `/${DEFAULT_RESOURCE_PREFIX}/jwks-uri`,
      Value: "https://idp.example.com/.well-known/jwks.json",
    });
  });

  test("does NOT create jwks-uri SSM param when jwksUri not provided", () => {
    const app = new cdk.App({ context: TEST_CONTEXT });
    const template = Template.fromStack(
      new IdpAuthenticationStack(app, "OidcNoJwks", {
        idpType: IdpType.OIDC,
        oidcSettings: {
          issuerUrl: "https://idp.example.com",
          clientId: "abc123",
        },
      }),
    );

    // Should have 4 SSM params (client-id, mcp-client-id, issuer,
    // group-token-name) but NOT jwks-uri
    template.resourceCountIs("AWS::SSM::Parameter", 4);
  });
});

// ═══════════════════════════════════════════════════════════════════
// IdpAuthenticationStack — OIDC missing settings
// ═══════════════════════════════════════════════════════════════════
describe("IdpAuthenticationStack (OIDC missing settings)", () => {
  test("throws when oidcSettings not provided", () => {
    const app = new cdk.App({ context: TEST_CONTEXT });
    expect(
      () => new IdpAuthenticationStack(app, "Bad", { idpType: IdpType.OIDC }),
    ).toThrow("oidcSettings is required when idpType is OIDC");
  });

  test("throws when OIDC clientId is empty", () => {
    const app = new cdk.App({ context: TEST_CONTEXT });
    expect(
      () =>
        new IdpAuthenticationStack(app, "EmptyClient", {
          idpType: IdpType.OIDC,
          oidcSettings: {
            clientId: "  ",
            issuerUrl: "https://idp.example.com",
          },
        }),
    ).toThrow("OIDC clientId and issuerUrl must be non-empty strings");
  });

  test("throws when OIDC issuerUrl is empty", () => {
    const app = new cdk.App({ context: TEST_CONTEXT });
    expect(
      () =>
        new IdpAuthenticationStack(app, "EmptyIssuer", {
          idpType: IdpType.OIDC,
          oidcSettings: { clientId: "abc123", issuerUrl: "" },
        }),
    ).toThrow("OIDC clientId and issuerUrl must be non-empty strings");
  });
});

// ═══════════════════════════════════════════════════════════════════
// IdpAuthenticationStack — Callback / Logout URL validation
// ═══════════════════════════════════════════════════════════════════
describe("IdpAuthenticationStack (URL validation)", () => {
  test("throws when callbackUrls contains non-HTTPS URL", () => {
    const app = new cdk.App({
      context: { ...TEST_CONTEXT, callbackUrls: ["http://example.com"] },
    });
    expect(() => new IdpAuthenticationStack(app, "BadCb")).toThrow(
      "callbackUrls must be a non-empty array of HTTPS URLs",
    );
  });

  test("throws when callbackUrls is empty array", () => {
    const app = new cdk.App({
      context: { ...TEST_CONTEXT, callbackUrls: [] },
    });
    expect(() => new IdpAuthenticationStack(app, "EmptyCb")).toThrow(
      "callbackUrls must be a non-empty array of HTTPS URLs",
    );
  });

  test("throws when logoutUrls contains non-HTTPS URL", () => {
    const app = new cdk.App({
      context: { ...TEST_CONTEXT, logoutUrls: ["http://example.com"] },
    });
    expect(() => new IdpAuthenticationStack(app, "BadLogout")).toThrow(
      "logoutUrls must be a non-empty array of HTTPS URLs",
    );
  });

  test("throws when logoutUrls is empty array", () => {
    const app = new cdk.App({
      context: { ...TEST_CONTEXT, logoutUrls: [] },
    });
    expect(() => new IdpAuthenticationStack(app, "EmptyLogout")).toThrow(
      "logoutUrls must be a non-empty array of HTTPS URLs",
    );
  });
});

// ═══════════════════════════════════════════════════════════════════
// IdpAuthenticationStack — Cognito federation (SAML provider)
// ═══════════════════════════════════════════════════════════════════
describe("IdpAuthenticationStack (SAML federation)", () => {
  let template: Template;

  beforeAll(() => {
    const app = new cdk.App({ context: TEST_CONTEXT });
    template = Template.fromStack(
      new IdpAuthenticationStack(app, "SamlAuth", {
        idpType: IdpType.SAML,
        samlProviders: [
          {
            name: "EnterpriseIdP",
            metadata: cognito.UserPoolIdentityProviderSamlMetadata.url(
              "https://idp.example.com/saml/metadata",
            ),
            userPool: undefined as any, // filled by stack
          },
        ],
        cognitoCustomAttributes: {
          groupids: new cognito.StringAttribute({ mutable: true }),
        },
        attributeMapping: {
          "custom:groupids": cognito.ProviderAttribute.other("groups"),
        },
      }),
    );
  });

  test("creates SAML identity provider", () => {
    template.hasResourceProperties("AWS::Cognito::UserPoolIdentityProvider", {
      ProviderName: "EnterpriseIdP",
      ProviderType: "SAML",
    });
  });

  test("creates User Pool with custom attributes", () => {
    template.hasResourceProperties("AWS::Cognito::UserPool", {
      Schema: Match.arrayWith([
        Match.objectLike({
          Name: "groupids",
          AttributeDataType: "String",
          Mutable: true,
        }),
      ]),
    });
  });

  test("client supports both Cognito and SAML provider", () => {
    template.hasResourceProperties("AWS::Cognito::UserPoolClient", {
      SupportedIdentityProviders: Match.arrayWith(["COGNITO", "EnterpriseIdP"]),
    });
  });
});

// ═══════════════════════════════════════════════════════════════════
// IdpAuthenticationStack — Auth flows by environment
// ═══════════════════════════════════════════════════════════════════
describe("IdpAuthenticationStack (auth flows)", () => {
  test("enables USER_PASSWORD_AUTH in dev environment", () => {
    const app = new cdk.App({ context: { ...TEST_CONTEXT, env: "dev" } });
    const template = Template.fromStack(
      new IdpAuthenticationStack(app, "DevAuth"),
    );

    // Match.arrayWith enforces relative order, so use direct inspection
    // (mirrors the "does NOT enable" sibling test below) for an
    // order-independent containment check.
    const clients = template.findResources("AWS::Cognito::UserPoolClient");
    const flows = Object.values(clients)[0].Properties?.ExplicitAuthFlows ?? [];
    expect(flows).toContain("ALLOW_USER_SRP_AUTH");
    expect(flows).toContain("ALLOW_USER_PASSWORD_AUTH");
  });

  test("does NOT enable USER_PASSWORD_AUTH in non-dev environments", () => {
    const app = new cdk.App({ context: { ...TEST_CONTEXT, env: "prod" } });
    const template = Template.fromStack(
      new IdpAuthenticationStack(app, "ProdAuth"),
    );

    const clients = template.findResources("AWS::Cognito::UserPoolClient");
    const flows = Object.values(clients)[0].Properties?.ExplicitAuthFlows ?? [];
    expect(flows).toContain("ALLOW_USER_SRP_AUTH");
    expect(flows).not.toContain("ALLOW_USER_PASSWORD_AUTH");
  });
});

// ═══════════════════════════════════════════════════════════════════
// IdpAuthenticationStack — Custom refresh token validity
// ═══════════════════════════════════════════════════════════════════
describe("IdpAuthenticationStack (custom refresh token)", () => {
  test("uses custom refresh token validity", () => {
    const app = new cdk.App({ context: TEST_CONTEXT });
    const template = Template.fromStack(
      new IdpAuthenticationStack(app, "CustomRefresh", {
        refreshTokenValidity: cdk.Duration.days(30),
      }),
    );

    template.hasResourceProperties("AWS::Cognito::UserPoolClient", {
      RefreshTokenValidity: 43200,
      TokenValidityUnits: Match.objectLike({ RefreshToken: "minutes" }),
    });
  });
});

// ═══════════════════════════════════════════════════════════════════
// IdpAuthenticationStack — hosted-UI domain prefix published for the CSP
// ═══════════════════════════════════════════════════════════════════
describe("IdpAuthenticationStack (hosted-UI domain prefix, issue #130)", () => {
  test("publishes the hosted-UI domain prefix to SSM", () => {
    // The web stack's Content-Security-Policy needs this origin or the browser
    // blocks the OAuth token exchange and every sign-in fails. Published rather
    // than re-derived so the naming rule has one owner — a drifting copy would
    // not fail a deploy, it would silently block login.
    const app = new cdk.App({ context: TEST_CONTEXT });
    const template = Template.fromStack(
      new IdpAuthenticationStack(app, "TestAuthDomainPrefix"),
    );

    template.hasResourceProperties("AWS::SSM::Parameter", {
      Name: `/${DEFAULT_RESOURCE_PREFIX}/cognito-domain-prefix`,
    });
  });

  test("the published prefix is the one the user-pool domain actually uses", () => {
    // Pins the two together: the parameter is only useful if it names the domain
    // that exists. Reading them out of the same template catches a rename of
    // either side.
    const app = new cdk.App({ context: TEST_CONTEXT });
    const template = Template.fromStack(
      new IdpAuthenticationStack(app, "TestAuthDomainParity"),
    );

    const domain = Object.values(
      template.findResources("AWS::Cognito::UserPoolDomain"),
    )[0];
    const param = Object.values(
      template.findResources("AWS::SSM::Parameter"),
    ).find(
      (r) =>
        r.Properties.Name ===
        `/${DEFAULT_RESOURCE_PREFIX}/cognito-domain-prefix`,
    );

    expect(param).toBeDefined();
    expect(param!.Properties.Value).toEqual(domain.Properties.Domain);
  });
});

// ═══════════════════════════════════════════════════════════════════
// IdpAuthenticationStack — Amazon Quick (MCP client redirect URL and
// access-token claims for MCP clients that use OAuth)
// ═══════════════════════════════════════════════════════════════════
const MCP_CLIENT_NAME = `${DEFAULT_RESOURCE_PREFIX}-${DEFAULT_ENV}-mcp-client`;
const LOCALHOST_CALLBACK = "http://localhost:9876/oauth/callback";
const QUICK_REDIRECT_URL =
  "https://us-east-1.quicksight.aws.amazon.com/sn/oauthcallback";
const MCP_CLIENT_ID_PARAM = `/${DEFAULT_RESOURCE_PREFIX}/mcp-client-id`;

function preTokenGenerationFunctions(template: Template) {
  return template.findResources("AWS::Lambda::Function", {
    Properties: {
      Environment: { Variables: { MCP_CLIENT_ID_PARAM: Match.anyValue() } },
    },
  });
}

describe("IdpAuthenticationStack (Amazon Quick)", () => {
  let template: Template;

  beforeAll(() => {
    const app = new cdk.App({ context: TEST_CONTEXT });
    template = Template.fromStack(new IdpAuthenticationStack(app, "Quick"));
  });

  test("MCP client keeps only the localhost callback by default", () => {
    template.hasResourceProperties("AWS::Cognito::UserPoolClient", {
      ClientName: MCP_CLIENT_NAME,
      CallbackURLs: [LOCALHOST_CALLBACK],
    });
  });

  test("MCP client also allows the Quick redirect URLs when configured", () => {
    const app = new cdk.App({ context: TEST_CONTEXT });
    const withQuick = Template.fromStack(
      new IdpAuthenticationStack(app, "QuickUrls", {
        quickRedirectUrls: [QUICK_REDIRECT_URL],
      }),
    );
    withQuick.hasResourceProperties("AWS::Cognito::UserPoolClient", {
      ClientName: MCP_CLIENT_NAME,
      CallbackURLs: [LOCALHOST_CALLBACK, QUICK_REDIRECT_URL],
    });
  });

  test("throws when a Quick redirect URL is not HTTPS", () => {
    const app = new cdk.App({ context: TEST_CONTEXT });
    expect(
      () =>
        new IdpAuthenticationStack(app, "QuickHttp", {
          quickRedirectUrls: ["http://quick.example.com/sn/oauthcallback"],
        }),
    ).toThrow("quickRedirectUrls must be HTTPS URLs");
  });

  test("fromSsmConfig passes quickRedirectUrls through", () => {
    const cfg: SsmConfig = { quickRedirectUrls: [QUICK_REDIRECT_URL] };
    expect(IdpAuthenticationStack.fromSsmConfig(cfg).quickRedirectUrls).toEqual(
      [QUICK_REDIRECT_URL],
    );
  });

  test("user pool has the pre-token-generation trigger, event version V2_0", () => {
    template.hasResourceProperties("AWS::Cognito::UserPool", {
      LambdaConfig: {
        PreTokenGenerationConfig: {
          LambdaVersion: "V2_0",
          LambdaArn: Match.anyValue(),
        },
      },
    });
  });

  test("pre-token-generation Lambda reads the MCP client ID parameter", () => {
    const fns = Object.values(preTokenGenerationFunctions(template));
    expect(fns).toHaveLength(1);
    expect(fns[0].Properties).toMatchObject({
      Runtime: "python3.12",
      Handler: "index.handler",
      Environment: { Variables: { MCP_CLIENT_ID_PARAM } },
    });
  });

  test("pre-token-generation Lambda has 256 MB (cold start well within Cognito's 5 s limit)", () => {
    // At 128 MB the cold start (boto3 client + SSM read) took 2.1 to 2.6 s.
    const fns = Object.values(preTokenGenerationFunctions(template));
    expect(fns[0].Properties.MemorySize).toBe(256);
  });

  test("pre-token-generation Lambda logs to its own log group: 1 month, deleted with the stack", () => {
    const [fnId] = Object.keys(preTokenGenerationFunctions(template));
    const fn = template.toJSON().Resources[fnId];
    const logGroupId = fn.Properties.LoggingConfig?.LogGroup?.Ref;
    expect(logGroupId).toBeDefined();
    const logGroup = template.toJSON().Resources[logGroupId];
    expect(logGroup.Type).toBe("AWS::Logs::LogGroup");
    expect(logGroup.Properties.RetentionInDays).toBe(30);
    expect(logGroup.DeletionPolicy).toBe("Delete");
  });

  test("Cognito (this user pool only) may invoke the pre-token-generation Lambda", () => {
    const [fnId] = Object.keys(preTokenGenerationFunctions(template));
    const [poolId] = Object.keys(
      template.findResources("AWS::Cognito::UserPool"),
    );
    template.hasResourceProperties("AWS::Lambda::Permission", {
      Action: "lambda:InvokeFunction",
      Principal: "cognito-idp.amazonaws.com",
      FunctionName: { "Fn::GetAtt": [fnId, "Arn"] },
      SourceArn: { "Fn::GetAtt": [poolId, "Arn"] },
    });
  });

  test("pre-token-generation Lambda can read the MCP client ID parameter", () => {
    const policies = template.findResources("AWS::IAM::Policy");
    const statements = Object.values(policies).flatMap(
      (p) => p.Properties.PolicyDocument.Statement,
    );
    const ssmRead = statements.filter((s) => s.Action === "ssm:GetParameter");
    expect(ssmRead).toHaveLength(1);
    expect(JSON.stringify(ssmRead[0].Resource)).toContain(
      `parameter${MCP_CLIENT_ID_PARAM}`,
    );
  });

  test("template has no dependency cycle (trigger, role policy, SSM parameter, MCP client)", () => {
    // CloudFormation rejects a cycle only at deploy time; synth doesn't.
    const resources = template.toJSON().Resources as Record<string, any>;
    const refs = (node: unknown, out: Set<string>): Set<string> => {
      if (Array.isArray(node)) node.forEach((n) => refs(n, out));
      else if (node && typeof node === "object") {
        for (const [k, v] of Object.entries(node)) {
          if (k === "Ref" && typeof v === "string" && v in resources)
            out.add(v);
          else if (k === "Fn::GetAtt" && Array.isArray(v) && v[0] in resources)
            out.add(v[0]);
          else refs(v, out);
        }
      }
      return out;
    };
    const edges = Object.fromEntries(
      Object.entries(resources).map(([id, r]) => {
        const deps = refs(r.Properties ?? {}, new Set<string>());
        [r.DependsOn ?? []].flat().forEach((d: string) => deps.add(d));
        return [id, [...deps]];
      }),
    );
    const state: Record<string, "visiting" | "done"> = {};
    const visit = (id: string, path: string[]): void => {
      if (state[id] === "done") return;
      if (state[id] === "visiting")
        throw new Error(`dependency cycle: ${[...path, id].join(" -> ")}`);
      state[id] = "visiting";
      edges[id].forEach((d) => visit(d, [...path, id]));
      state[id] = "done";
    };
    expect(() =>
      Object.keys(edges).forEach((id) => visit(id, [])),
    ).not.toThrow();
  });

  test("no pre-token-generation Lambda in OIDC mode", () => {
    const app = new cdk.App({ context: TEST_CONTEXT });
    const oidc = Template.fromStack(
      new IdpAuthenticationStack(app, "QuickOidc", {
        idpType: IdpType.OIDC,
        oidcSettings: { issuerUrl: "https://idp.example.com", clientId: "abc" },
      }),
    );
    expect(Object.keys(preTokenGenerationFunctions(oidc))).toHaveLength(0);
  });
});
