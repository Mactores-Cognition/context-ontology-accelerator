// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

// Keys import (add-on): hooks for the keys-import routes (keys-import.smithy).
// Imported directly by the KeysImport component rather than through
// api-hooks/index.ts, so removing the feature touches no shared file.

import { useMutation, useQuery } from "@tanstack/react-query";
import {
  GetKeysImportJobCommand,
  GetKeysImportUploadUrlCommand,
  ListKeysImportJobsCommand,
  PreviewKeysImportCommand,
  StartKeysImportCommand,
} from "@coa/control-plane-client";
import type {
  GetKeysImportJobOutput,
  ListKeysImportJobsOutput,
  PreviewKeysImportOutput,
  StartKeysImportOutput,
} from "@coa/control-plane-client";
import { useControlPlaneClient } from "@components/ControlPlaneClientProvider";

export interface CheckedFile {
  /** S3 key of the uploaded CSV, passed to Start. */
  s3Key: string;
  preview: PreviewKeysImportOutput;
}

/** Upload a CSV through a pre-signed URL, then preview it (writes nothing). */
export function useCheckKeysFile(namespaceId: string, sourceId: string) {
  const client = useControlPlaneClient();
  return useMutation<CheckedFile, Error, File>({
    mutationFn: async (file: File) => {
      const { uploadUrl, s3Key } = await client.send(
        new GetKeysImportUploadUrlCommand({
          namespaceId,
          sourceId,
          fileName: file.name,
        }),
      );
      if (!uploadUrl || !s3Key) {
        throw new Error("No upload URL was returned.");
      }
      // The CSV goes straight to S3 (the bucket's CORS allows the web app).
      const upload = await fetch(uploadUrl, {
        method: "PUT",
        body: file,
        headers: { "Content-Type": "text/csv" },
      });
      if (!upload.ok) {
        throw new Error(`File upload failed (HTTP ${upload.status}).`);
      }
      const preview = await client.send(
        new PreviewKeysImportCommand({ namespaceId, sourceId, s3Key }),
      );
      return { s3Key, preview };
    },
  });
}

/** Start adding the keys of a checked file (202; runs in the background). */
export function useStartKeysImport(namespaceId: string, sourceId: string) {
  const client = useControlPlaneClient();
  return useMutation<StartKeysImportOutput, Error, string>({
    mutationFn: (s3Key: string) =>
      client.send(new StartKeysImportCommand({ namespaceId, sourceId, s3Key })),
  });
}

/** One run; polled every `pollIntervalMs` while it is IN_PROGRESS. */
export function useKeysImportJob(
  namespaceId: string,
  sourceId: string,
  jobId: string | null,
  pollIntervalMs: number,
) {
  const client = useControlPlaneClient();
  return useQuery<GetKeysImportJobOutput, Error>({
    queryKey: ["keysImportJob", namespaceId, sourceId, jobId],
    queryFn: () =>
      client.send(
        new GetKeysImportJobCommand({
          namespaceId,
          sourceId,
          jobId: jobId ?? "",
        }),
      ),
    enabled: !!jobId,
    refetchInterval: (query) =>
      query.state.data?.job?.status === "IN_PROGRESS" ? pollIntervalMs : false,
  });
}

/** A source's past runs, newest first; only fetched while `enabled`. */
export function useKeysImportJobs(
  namespaceId: string,
  sourceId: string,
  enabled: boolean,
) {
  const client = useControlPlaneClient();
  return useQuery<ListKeysImportJobsOutput, Error>({
    queryKey: ["keysImportJobs", namespaceId, sourceId],
    queryFn: () =>
      client.send(
        new ListKeysImportJobsCommand({
          namespaceId,
          sourceId,
          maxResults: 10,
        }),
      ),
    enabled,
  });
}
