// Keys import (add-on): the dialog, opened from the source page's "Custom actions"
// menu (CustomActionsMenu).
// Expected results: work/add-bulk-import.txt, section 3.6 (U1-U11).

import {
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { CustomActionsMenu } from "@components/CustomActions/CustomActionsMenu";
import type { KeysImportActionProps } from "./KeysImportButton";

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

const S3_KEY = "keys-import/ns-1/src-1/abcd1234/keys.csv";
const COUNTS = {
  rowsRead: 5,
  toAdd: 2,
  alreadyThere: 1,
  duplicates: 0,
  skipped: 0,
  errors: 2,
  tablesToUpdate: 2,
};
const PREVIEW = {
  counts: COUNTS,
  rowIssues: [
    { line: 2, outcome: "ALREADY_THERE" },
    {
      line: 5,
      outcome: "ERROR",
      message: "table 'vendor' was not found in this source.",
    },
  ],
  rowIssuesTruncated: false,
};
const DONE_JOB = {
  jobId: "20261104T153012Z-1a2b3c4d",
  status: "COMPLETED",
  fileName: "keys.csv",
  startedAt: new Date("2026-11-04T15:30:12Z"),
  counts: COUNTS,
  tablesUpdated: 2,
  tablesFailed: 0,
  keysAdded: 2,
  tableFailures: [],
  errorReportUrl: "https://bucket.s3.amazonaws.com/report.csv?sig",
};

type Input = Record<string, unknown>;
type Handlers = Partial<Record<string, (input: Input) => unknown>>;
type SentCommand = { constructor: { commandName: string }; input: Input };

function route(handlers: Handlers) {
  const calls: Record<string, Input[]> = {};
  send.mockImplementation(async (cmd: SentCommand) => {
    const name = cmd.constructor.commandName;
    (calls[name] ??= []).push(cmd.input);
    const handler = handlers[name];
    if (!handler) throw new Error(`unexpected ${name}`);
    return handler(cmd.input);
  });
  return calls;
}

const defaultHandlers: Handlers = {
  ListKeysImportJobs: () => ({ items: [] }),
  GetKeysImportUploadUrl: () => ({
    uploadUrl: "https://bucket.s3.amazonaws.com/put?sig",
    s3Key: S3_KEY,
    expiresIn: 900,
  }),
  PreviewKeysImport: () => PREVIEW,
  StartKeysImport: () => ({ jobId: DONE_JOB.jobId, status: "IN_PROGRESS" }),
  GetKeysImportJob: () => ({ job: DONE_JOB }),
};

const user = userEvent.setup({ delay: null });
const fetchMock = vi.fn();

function renderButton(props: Partial<KeysImportActionProps> = {}) {
  const onImported = vi.fn();
  render(
    <QueryClientProvider
      client={
        new QueryClient({ defaultOptions: { queries: { retry: false } } })
      }
    >
      <CustomActionsMenu
        namespaceId="ns-1"
        sourceId="src-1"
        keysImport={{ onImported, pollIntervalMs: 10, ...props }}
      />
    </QueryClientProvider>,
  );
  return { onImported };
}

/** Opens the import dialog the way a user does: Custom actions > Import keys (CSV). */
async function openDialog() {
  await user.click(screen.getByRole("button", { name: "Custom actions" }));
  await user.click(
    await screen.findByRole("menuitem", { name: /Import keys \(CSV\)/ }),
  );
  return screen.findByRole("dialog");
}

async function openAndCheck(
  text = "type,source_table,column,target_table,target_column\n",
) {
  const dialog = await openDialog();
  const input = dialog.querySelector('input[type="file"]') as HTMLInputElement;
  // Same workaround as ConnectSource.test.tsx: Cloudscape assigns `input.files`,
  // which happy-dom exposes as getter-only (and user.upload hits the same wall).
  Object.defineProperty(input, "files", {
    configurable: true,
    writable: true,
    value: [new File([text], "keys.csv", { type: "text/csv" })],
  });
  fireEvent.change(input);
  await user.click(within(dialog).getByRole("button", { name: "Check file" }));
  return dialog;
}

beforeEach(() => {
  send.mockReset();
  fetchMock.mockReset();
  fetchMock.mockResolvedValue({ ok: true, status: 200 });
  vi.stubGlobal("fetch", fetchMock);
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("Keys import dialog", () => {
  it("U1 the menu item calls no API until chosen; greyed out when asked", async () => {
    route(defaultHandlers);
    renderButton({ disabled: true });
    await user.click(screen.getByRole("button", { name: "Custom actions" }));
    expect(
      await screen.findByRole("menuitem", { name: /Import keys \(CSV\)/ }),
    ).toHaveAttribute("aria-disabled", "true");
    expect(send).not.toHaveBeenCalled();
  });

  it("U2 lists past imports when opened", async () => {
    route({
      ...defaultHandlers,
      ListKeysImportJobs: () => ({ items: [DONE_JOB] }),
    });
    renderButton();
    const dialog = await openDialog();
    expect(await within(dialog).findByText("keys.csv")).toBeInTheDocument();
    expect(within(dialog).getByText("Completed")).toBeInTheDocument();
  });

  it("U3 uploads the file to S3 and shows the preview counts and row issues", async () => {
    const calls = route(defaultHandlers);
    renderButton();
    const dialog = await openAndCheck(
      "type,source_table,column,target_table,target_column\nPK,t,a,,\n",
    );

    expect(calls.GetKeysImportUploadUrl).toEqual([
      { namespaceId: "ns-1", sourceId: "src-1", fileName: "keys.csv" },
    ]);
    expect(fetchMock).toHaveBeenCalledWith(
      "https://bucket.s3.amazonaws.com/put?sig",
      expect.objectContaining({
        method: "PUT",
        headers: { "Content-Type": "text/csv" },
      }),
    );
    expect(await within(dialog).findByText("To add")).toBeInTheDocument();
    expect(calls.PreviewKeysImport).toEqual([
      { namespaceId: "ns-1", sourceId: "src-1", s3Key: S3_KEY },
    ]);
    expect(
      within(dialog).getByText("table 'vendor' was not found in this source."),
    ).toBeInTheDocument();
    expect(within(dialog).getByRole("button", { name: "Apply" })).toBeEnabled();
  });

  it("U4 Apply is disabled when the file adds no keys", async () => {
    route({
      ...defaultHandlers,
      PreviewKeysImport: () => ({
        ...PREVIEW,
        counts: { ...COUNTS, toAdd: 0, tablesToUpdate: 0 },
      }),
    });
    renderButton();
    const dialog = await openAndCheck();
    expect(
      await within(dialog).findByText(/adds no keys/i),
    ).toBeInTheDocument();
    expect(
      within(dialog).getByRole("button", { name: "Apply" }),
    ).toBeDisabled();
  });

  it("U5 Apply starts the run, follows it to the end and shows the result", async () => {
    let polls = 0;
    const calls = route({
      ...defaultHandlers,
      GetKeysImportJob: () =>
        ++polls < 2
          ? {
              job: { ...DONE_JOB, status: "IN_PROGRESS", keysAdded: undefined },
            }
          : { job: DONE_JOB },
    });
    const { onImported } = renderButton();
    const dialog = await openAndCheck();
    await user.click(
      await within(dialog).findByRole("button", { name: "Apply" }),
    );

    expect(calls.StartKeysImport).toEqual([
      { namespaceId: "ns-1", sourceId: "src-1", s3Key: S3_KEY },
    ]);
    expect(
      await within(dialog).findByText("Added 2 keys to 2 tables."),
    ).toBeInTheDocument();
    expect(
      within(dialog).getByRole("link", { name: /download the error report/i }),
    ).toHaveAttribute("href", DONE_JOB.errorReportUrl);
    expect(onImported).toHaveBeenCalledTimes(1);
  });

  it("U6 a failed S3 upload is shown and nothing is previewed", async () => {
    const calls = route(defaultHandlers);
    fetchMock.mockResolvedValue({ ok: false, status: 403 });
    renderButton();
    const dialog = await openAndCheck();
    expect(
      await within(dialog).findByText(/upload failed.*403/i),
    ).toBeInTheDocument();
    expect(calls.PreviewKeysImport).toBeUndefined();
  });

  it("U7 an API error (e.g. a bad file) is shown with its message", async () => {
    route({
      ...defaultHandlers,
      PreviewKeysImport: () => {
        throw new Error(
          "The header row is missing required column(s): target_column.",
        );
      },
    });
    renderButton();
    const dialog = await openAndCheck();
    expect(
      await within(dialog).findByText(/missing required column/),
    ).toBeInTheDocument();
    expect(within(dialog).queryByRole("button", { name: "Apply" })).toBeNull();
  });

  it("U8 a run that failed as a whole shows the reason", async () => {
    route({
      ...defaultHandlers,
      GetKeysImportJob: () => ({
        job: {
          ...DONE_JOB,
          status: "FAILED",
          errorMessage:
            "The source is REJECTED; keys cannot be added to it now.",
        },
      }),
    });
    renderButton();
    const dialog = await openAndCheck();
    await user.click(
      await within(dialog).findByRole("button", { name: "Apply" }),
    );
    expect(
      await within(dialog).findByText(/source is REJECTED/),
    ).toBeInTheDocument();
  });

  it("U9 tables that could not be saved are listed", async () => {
    route({
      ...defaultHandlers,
      GetKeysImportJob: () => ({
        job: {
          ...DONE_JOB,
          tablesUpdated: 1,
          tablesFailed: 1,
          keysAdded: 1,
          tableFailures: [
            { tableId: "sales.supplier", message: "HTTP 400: bad key" },
          ],
        },
      }),
    });
    renderButton();
    const dialog = await openAndCheck();
    await user.click(
      await within(dialog).findByRole("button", { name: "Apply" }),
    );
    expect(
      await within(dialog).findByText("sales.supplier"),
    ).toBeInTheDocument();
    expect(within(dialog).getByText("HTTP 400: bad key")).toBeInTheDocument();
    await waitFor(() =>
      expect(
        within(dialog).getByText(
          "Added 1 key to 1 table. 1 table could not be saved.",
        ),
      ).toBeInTheDocument(),
    );
  });

  it("U10 a failed past-imports list shows a warning; Check file still works", async () => {
    route({
      ...defaultHandlers,
      ListKeysImportJobs: () => {
        throw new Error("Internal server error");
      },
    });
    renderButton();
    const dialog = await openDialog();
    expect(
      await within(dialog).findByText("Past imports could not be loaded"),
    ).toBeInTheDocument();
    expect(
      within(dialog).getByText("Internal server error"),
    ).toBeInTheDocument();
    expect(within(dialog).queryByText("No imports yet")).toBeNull();
    // Opening again starts a fresh dialog; the check itself still runs.
    await user.click(within(dialog).getByRole("button", { name: "Close" }));
    const checked = await openAndCheck();
    expect(await within(checked).findByText("To add")).toBeInTheDocument();
  });

  it("U11 an empty past-imports list says so", async () => {
    route(defaultHandlers);
    renderButton();
    const dialog = await openDialog();
    expect(
      await within(dialog).findByText("No imports yet"),
    ).toBeInTheDocument();
    expect(
      within(dialog).queryByText("Past imports could not be loaded"),
    ).toBeNull();
  });
});
