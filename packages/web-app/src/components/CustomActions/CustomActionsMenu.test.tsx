// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

// "Custom actions" menu on the source page (add-on actions that are not part of
// standard COA). Expected results: work/add-bulk-import.txt, section 3.6 (M1-M4).

import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { describe, it, expect, vi, beforeEach } from "vitest";
import {
  CustomActionsMenu,
  KEYS_IMPORT_DISABLED_REASON,
} from "./CustomActionsMenu";

vi.mock("@coa/control-plane-client", () => {
  const command = (name: string) =>
    class {
      static commandName = name;
      input: Record<string, unknown>;
      constructor(input: Record<string, unknown>) {
        this.input = input;
      }
    };
  return {
    GetKeysImportUploadUrlCommand: command("GetKeysImportUploadUrl"),
    PreviewKeysImportCommand: command("PreviewKeysImport"),
    StartKeysImportCommand: command("StartKeysImport"),
    GetKeysImportJobCommand: command("GetKeysImportJob"),
    ListKeysImportJobsCommand: command("ListKeysImportJobs"),
  };
});

const send = vi.hoisted(() => vi.fn());
vi.mock("@components/ControlPlaneClientProvider", () => ({
  useControlPlaneClient: () => ({ send }),
}));

const user = userEvent.setup({ delay: null });

function renderMenu(keysImportDisabled = false) {
  render(
    <QueryClientProvider
      client={
        new QueryClient({ defaultOptions: { queries: { retry: false } } })
      }
    >
      <CustomActionsMenu
        namespaceId="ns-1"
        sourceId="src-1"
        keysImport={{ disabled: keysImportDisabled }}
      />
    </QueryClientProvider>,
  );
}

async function openMenu() {
  await user.click(screen.getByRole("button", { name: "Custom actions" }));
}

beforeEach(() => {
  send.mockReset();
  send.mockResolvedValue({ items: [] });
});

describe("CustomActionsMenu", () => {
  it("M1 shows a 'Custom actions' menu with 'Import keys (CSV)'; no API call until chosen", async () => {
    renderMenu();
    await openMenu();
    const item = await screen.findByRole("menuitem", {
      name: /Import keys \(CSV\)/,
    });
    expect(item).not.toHaveAttribute("aria-disabled", "true");
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(send).not.toHaveBeenCalled();
  });

  it("M2 a disabled item is greyed out and carries a short reason (shown as a tooltip)", async () => {
    renderMenu(true);
    await openMenu();
    const item = await screen.findByRole("menuitem", {
      name: /Import keys \(CSV\)/,
    });
    expect(item).toHaveAttribute("aria-disabled", "true");
    expect(KEYS_IMPORT_DISABLED_REASON.length).toBeLessThanOrEqual(60);
    expect(document.body).toHaveTextContent(KEYS_IMPORT_DISABLED_REASON);
    await user.click(item);
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("M3 choosing 'Import keys (CSV)' opens the import dialog", async () => {
    renderMenu();
    await openMenu();
    await user.click(
      await screen.findByRole("menuitem", { name: /Import keys \(CSV\)/ }),
    );
    const dialog = await screen.findByRole("dialog");
    expect(
      within(dialog).getByText("Import keys from a CSV file"),
    ).toBeInTheDocument();
  });

  it("M4 closing the dialog leaves only the menu", async () => {
    renderMenu();
    await openMenu();
    await user.click(
      await screen.findByRole("menuitem", { name: /Import keys \(CSV\)/ }),
    );
    const dialog = await screen.findByRole("dialog");
    await user.click(within(dialog).getByRole("button", { name: "Close" }));
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(
      screen.getByRole("button", { name: "Custom actions" }),
    ).toBeInTheDocument();
  });
});
