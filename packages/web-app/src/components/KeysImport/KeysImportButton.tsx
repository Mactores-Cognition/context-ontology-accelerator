// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

// Keys import (add-on): "Import keys (CSV)" button and dialog for a DATABASE
// source. Plan: work/add-bulk-import.txt, section 2. Flow: choose a CSV ->
// Check file (upload to S3 + preview, writes nothing) -> Apply (background
// run) -> progress -> result. Existing keys are never changed or removed.

import React, { useEffect, useRef, useState } from "react";
import Alert from "@cloudscape-design/components/alert";
import Box from "@cloudscape-design/components/box";
import Button from "@cloudscape-design/components/button";
import FileUpload from "@cloudscape-design/components/file-upload";
import FormField from "@cloudscape-design/components/form-field";
import Header from "@cloudscape-design/components/header";
import KeyValuePairs from "@cloudscape-design/components/key-value-pairs";
import Link from "@cloudscape-design/components/link";
import Modal from "@cloudscape-design/components/modal";
import SpaceBetween from "@cloudscape-design/components/space-between";
import StatusIndicator from "@cloudscape-design/components/status-indicator";
import Table from "@cloudscape-design/components/table";
import type {
  KeysImportCounts,
  KeysImportJob,
  KeysImportRowIssue,
  KeysImportTableFailure,
} from "@coa/control-plane-client";
import {
  useCheckKeysFile,
  useKeysImportJob,
  useKeysImportJobs,
  useStartKeysImport,
} from "../../api-hooks/use-keys-import";

export interface KeysImportButtonProps {
  namespaceId: string;
  sourceId: string;
  /** Disable the button (e.g. while the source is scanning or being approved). */
  disabled?: boolean;
  /** Called once when a run finishes, so the page can reload the tables. */
  onImported?: () => void;
  /** How often to check a running import (ms). */
  pollIntervalMs?: number;
}

const OUTCOME_LABEL: Record<string, string> = {
  ADD: "Add",
  ALREADY_THERE: "Already there",
  DUPLICATE: "Duplicate",
  SKIPPED: "Skipped",
  ERROR: "Error",
};

const STATUS_LABEL: Record<string, string> = {
  IN_PROGRESS: "In progress",
  COMPLETED: "Completed",
  FAILED: "Failed",
};

const STATUS_TYPE: Record<string, "in-progress" | "success" | "error"> = {
  IN_PROGRESS: "in-progress",
  COMPLETED: "success",
  FAILED: "error",
};

function plural(n: number, word: string): string {
  return `${n} ${word}${n === 1 ? "" : "s"}`;
}

/** "Added 2 keys to 2 tables." plus failures, if any. */
export function resultSummary(job: KeysImportJob): string {
  const added = `Added ${plural(job.keysAdded ?? 0, "key")} to ${plural(job.tablesUpdated ?? 0, "table")}.`;
  const failed = job.tablesFailed ?? 0;
  return failed > 0
    ? `${added} ${plural(failed, "table")} could not be saved.`
    : added;
}

function Counts({ counts }: { counts: KeysImportCounts }) {
  return (
    <KeyValuePairs
      columns={4}
      items={[
        { label: "Rows read", value: counts.rowsRead },
        { label: "To add", value: counts.toAdd },
        { label: "Already there", value: counts.alreadyThere },
        { label: "Duplicates", value: counts.duplicates },
        { label: "Skipped", value: counts.skipped },
        { label: "Errors", value: counts.errors },
        { label: "Tables to update", value: counts.tablesToUpdate },
      ]}
    />
  );
}

function RowIssues({
  issues,
  truncated,
}: {
  issues: KeysImportRowIssue[];
  truncated: boolean;
}) {
  if (issues.length === 0) return null;
  return (
    <Table
      variant="embedded"
      header={
        <Header
          variant="h3"
          description={
            truncated
              ? "Only the first rows are shown; the full list is in the error report after Apply."
              : undefined
          }
        >
          Rows to check
        </Header>
      }
      columnDefinitions={[
        { id: "line", header: "Line", cell: (i) => i.line },
        {
          id: "outcome",
          header: "Outcome",
          cell: (i) => OUTCOME_LABEL[i.outcome ?? ""] ?? i.outcome,
        },
        {
          id: "details",
          header: "Details",
          cell: (i) => i.message || i.warning || "",
        },
      ]}
      items={issues}
    />
  );
}

function TableFailures({ failures }: { failures: KeysImportTableFailure[] }) {
  if (failures.length === 0) return null;
  return (
    <Table
      variant="embedded"
      header={<Header variant="h3">Tables not saved</Header>}
      columnDefinitions={[
        { id: "table", header: "Table", cell: (f) => f.tableId },
        { id: "message", header: "Reason", cell: (f) => f.message },
      ]}
      items={failures}
    />
  );
}

function PastImports({ jobs }: { jobs: KeysImportJob[] }) {
  if (jobs.length === 0) return null;
  return (
    <Table
      variant="embedded"
      header={<Header variant="h3">Past imports</Header>}
      columnDefinitions={[
        {
          id: "started",
          header: "Started",
          cell: (j) => (j.startedAt ? j.startedAt.toLocaleString() : ""),
        },
        { id: "file", header: "File", cell: (j) => j.fileName },
        {
          id: "status",
          header: "Status",
          cell: (j) => (
            <StatusIndicator type={STATUS_TYPE[j.status ?? ""] ?? "info"}>
              {STATUS_LABEL[j.status ?? ""] ?? j.status}
            </StatusIndicator>
          ),
        },
        { id: "keys", header: "Keys added", cell: (j) => j.keysAdded ?? "" },
        { id: "by", header: "Started by", cell: (j) => j.startedBy ?? "" },
      ]}
      items={jobs}
    />
  );
}

function JobResult({ job }: { job: KeysImportJob }) {
  if (job.status === "IN_PROGRESS") {
    return (
      <StatusIndicator type="in-progress">
        Adding keys. This can take a few minutes for a large file.
      </StatusIndicator>
    );
  }
  if (job.status === "FAILED") {
    return (
      <Alert type="error" header="The import did not run">
        {job.errorMessage}
      </Alert>
    );
  }
  return (
    <SpaceBetween size="s">
      <Alert type={(job.tablesFailed ?? 0) > 0 ? "warning" : "success"}>
        {resultSummary(job)}
      </Alert>
      {job.errorReportUrl && (
        <Link href={job.errorReportUrl} external>
          Download the error report
        </Link>
      )}
      <TableFailures failures={job.tableFailures ?? []} />
    </SpaceBetween>
  );
}

type KeysImportDialogProps = Omit<KeysImportButtonProps, "disabled"> & {
  onClose: () => void;
};

/** The dialog and all its API hooks; created only while it is open. */
function KeysImportDialog({
  namespaceId,
  sourceId,
  onImported,
  pollIntervalMs = 3000,
  onClose,
}: KeysImportDialogProps) {
  const [files, setFiles] = useState<File[]>([]);
  const [jobId, setJobId] = useState<string | null>(null);
  const reported = useRef<string | null>(null);

  const check = useCheckKeysFile(namespaceId, sourceId);
  const start = useStartKeysImport(namespaceId, sourceId);
  const jobs = useKeysImportJobs(namespaceId, sourceId, !jobId);
  const job = useKeysImportJob(namespaceId, sourceId, jobId, pollIntervalMs);
  const current = job.data?.job;

  useEffect(() => {
    if (
      current &&
      current.status !== "IN_PROGRESS" &&
      reported.current !== current.jobId
    ) {
      reported.current = current.jobId ?? null;
      if (current.status === "COMPLETED") onImported?.();
    }
  }, [current, onImported]);

  const reset = () => {
    setFiles([]);
    setJobId(null);
    check.reset();
    start.reset();
  };
  // Closing unmounts the dialog, which discards its state.
  const close = onClose;

  const checked = check.data;
  const counts = checked?.preview.counts;
  const canApply = !!checked && (counts?.toAdd ?? 0) > 0 && !jobId;

  return (
    <Modal
      visible
      onDismiss={close}
      size="large"
      header="Import keys from a CSV file"
      footer={
        <Box float="right">
          <SpaceBetween direction="horizontal" size="xs">
            <Button variant="link" onClick={close}>
              Close
            </Button>
            {!jobId && !checked && (
              <Button
                onClick={() => files[0] && check.mutate(files[0])}
                disabled={files.length === 0}
                loading={check.isPending}
              >
                Check file
              </Button>
            )}
            {checked && !jobId && (
              <Button
                variant="primary"
                disabled={!canApply}
                loading={start.isPending}
                onClick={() =>
                  start.mutate(checked.s3Key, {
                    onSuccess: (out) => setJobId(out.jobId ?? null),
                  })
                }
              >
                Apply
              </Button>
            )}
            {current && current.status !== "IN_PROGRESS" && (
              <Button onClick={reset}>Import another file</Button>
            )}
          </SpaceBetween>
        </Box>
      }
    >
      <SpaceBetween size="m">
        {!jobId && (
          <FormField
            label="Keys file (CSV)"
            description="One row per key: type,source_table,column,target_table,target_column. Keys the tables already have are kept; the file only adds keys."
          >
            <FileUpload
              value={files}
              onChange={({ detail }) => {
                setFiles(detail.value);
                check.reset();
              }}
              accept=".csv"
              i18nStrings={{
                uploadButtonText: () => "Choose file",
                dropzoneText: () => "Drop file to upload",
                removeFileAriaLabel: (i) => `Remove file ${i + 1}`,
                limitShowFewer: "Show fewer files",
                limitShowMore: "Show more files",
                errorIconAriaLabel: "Error",
              }}
              constraintText="CSV UTF-8, up to 20 MB and 50,000 rows."
            />
          </FormField>
        )}

        {check.isError && (
          <Alert type="error" header="The file could not be checked">
            {check.error.message}
          </Alert>
        )}
        {start.isError && (
          <Alert type="error" header="The import could not start">
            {start.error.message}
          </Alert>
        )}

        {checked && counts && !jobId && (
          <SpaceBetween size="s">
            <Counts counts={counts} />
            {counts.toAdd === 0 && (
              <Alert type="info">
                This file adds no keys: every row is already there, skipped, a
                duplicate or an error.
              </Alert>
            )}
            <RowIssues
              issues={checked.preview.rowIssues ?? []}
              truncated={!!checked.preview.rowIssuesTruncated}
            />
          </SpaceBetween>
        )}

        {jobId && !current && (
          <StatusIndicator type="loading">Starting the import</StatusIndicator>
        )}
        {current && <JobResult job={current} />}
        {job.isError && <Alert type="error">{job.error.message}</Alert>}

        {!jobId && !checked && <PastImports jobs={jobs.data?.items ?? []} />}
      </SpaceBetween>
    </Modal>
  );
}

/**
 * "Import keys (CSV)" button. Uses no API hooks itself: the dialog (and every
 * API call) exists only while it is open, so the source page is unchanged
 * while it is closed (a closed Cloudscape Modal would still render a hidden
 * role="dialog").
 */
export function KeysImportButton({
  disabled,
  ...dialogProps
}: KeysImportButtonProps) {
  const [visible, setVisible] = useState(false);
  return (
    <>
      <Button onClick={() => setVisible(true)} disabled={disabled}>
        Import keys (CSV)
      </Button>
      {visible && (
        <KeysImportDialog {...dialogProps} onClose={() => setVisible(false)} />
      )}
    </>
  );
}
