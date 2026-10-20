// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import * as cdk from "aws-cdk-lib";
import { Template, Match } from "aws-cdk-lib/assertions";
import { WafWebAcl } from "../../lib/constructs/waf-web-acl";
import { DEFAULT_RESOURCE_PREFIX, DEFAULT_ENV } from "../../lib/constants";

/**
 * WafWebAcl auto-provisions baseline protection: the AWS Common Rule Set plus a
 * per-IP rate-based rule. The rate rule is the only per-entity
 * throttle that runs before the auth layer, so it is asserted explicitly.
 */
const mkTemplate = (props: {
  scope: "REGIONAL" | "CLOUDFRONT";
  namePrefix: string;
  rateLimit?: number;
}) => {
  const app = new cdk.App({
    context: { resource_prefix: DEFAULT_RESOURCE_PREFIX, env: DEFAULT_ENV },
  });
  const stack = new cdk.Stack(app, "TestWafStack", {
    env: { account: "123456789012", region: "us-east-1" },
  });
  new WafWebAcl(stack, "Waf", props);
  return Template.fromStack(stack);
};

describe("WafWebAcl", () => {
  it("includes the AWS Common Rule Set", () => {
    const template = mkTemplate({ scope: "REGIONAL", namePrefix: "api" });
    template.hasResourceProperties("AWS::WAFv2::WebACL", {
      Rules: Match.arrayWith([
        Match.objectLike({
          Statement: {
            ManagedRuleGroupStatement: {
              VendorName: "AWS",
              Name: "AWSManagedRulesCommonRuleSet",
            },
          },
        }),
      ]),
    });
  });

  it("adds a per-IP rate-based rule with the default limit and block action", () => {
    const template = mkTemplate({ scope: "REGIONAL", namePrefix: "api" });
    template.hasResourceProperties("AWS::WAFv2::WebACL", {
      Rules: Match.arrayWith([
        Match.objectLike({
          Name: "RateLimitPerIp",
          Priority: 1,
          Action: { Block: {} },
          Statement: {
            RateBasedStatement: {
              AggregateKeyType: "IP",
              Limit: 2000,
            },
          },
        }),
      ]),
    });
  });

  it("honors a custom rate limit", () => {
    const template = mkTemplate({
      scope: "REGIONAL",
      namePrefix: "ui",
      rateLimit: 500,
    });
    template.hasResourceProperties("AWS::WAFv2::WebACL", {
      Rules: Match.arrayWith([
        Match.objectLike({
          Name: "RateLimitPerIp",
          Statement: {
            RateBasedStatement: {
              AggregateKeyType: "IP",
              Limit: 500,
            },
          },
        }),
      ]),
    });
  });

  it("omits the rate-based rule when rateLimit is 0 (caller brings their own)", () => {
    const template = mkTemplate({
      scope: "REGIONAL",
      namePrefix: "api",
      rateLimit: 0,
    });
    // Common Rule Set is still present; the rate rule is not.
    template.hasResourceProperties("AWS::WAFv2::WebACL", {
      Rules: Match.not(
        Match.arrayWith([Match.objectLike({ Name: "RateLimitPerIp" })]),
      ),
    });
  });
});
