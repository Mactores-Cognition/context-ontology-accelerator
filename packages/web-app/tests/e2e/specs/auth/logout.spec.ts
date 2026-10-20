// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { test } from "../../fixtures/test";
import { LoginPage } from "../../pages/LoginPage";
import { readE2EEnv, MISSING_ENV_REASON } from "../../fixtures/env";

/**
 * Verifies sign-out returns the user to the login gate.
 *
 * Deferred (fixme): sign-out redirects to Cognito `/logout` with a
 * `post_logout_redirect_uri` that must appear in the app client's allowed
 * sign-out URLs or Cognito returns `redirect_mismatch`. WebStack now sets
 * `LogoutURLs` to the deployed origin, so this should pass against a
 * deployment with a UI custom domain; it cannot pass on the plain-HTTP ALB
 * path, where Cognito only permits the localhost logout URL. Re-enable once
 * verified against an HTTPS environment.
 */
test.describe("auth: logout", () => {
  test.fixme("signs out and returns to the login gate", async ({ page }) => {
    const env = readE2EEnv();
    test.skip(!env, MISSING_ENV_REASON);

    const login = new LoginPage(page);
    await login.login(env!.username, env!.password);
    await login.logout();
    await login.expectLoggedOut();
  });
});
