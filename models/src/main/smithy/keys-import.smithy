// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0
// Keys import — add primary keys and foreign keys to a DATABASE source's tables
// from a CSV file. A separate add-on (plan: work/add-bulk-import.txt, section 2):
// it only adds keys and saves them through the existing per-table key handler
// (UpdateSourceTableKeys), which it calls but never changes.
//
// Flow: GetKeysImportUploadUrl -> browser PUTs the CSV to S3 -> PreviewKeysImport
// (checks, writes nothing) -> StartKeysImport (202, background worker) ->
// GetKeysImportJob / ListKeysImportJobs.
//
// The service definition lives in control-plane.smithy (ControlPlaneService).
$version: "2"

namespace com.amazon.semanticcontext.keysimport

use com.amazon.semanticcontext.common#AccessDeniedError
use com.amazon.semanticcontext.common#ConflictError
use com.amazon.semanticcontext.common#NotFoundError
use com.amazon.semanticcontext.common#PaginatedInput
use com.amazon.semanticcontext.common#PaginatedOutput
use com.amazon.semanticcontext.common#Uuid
use com.amazon.semanticcontext.common#ValidationError

// ── Shapes ────────────────────────────────────────────────────────────────────
/// Name of the CSV file being uploaded: a bare file name ending in .csv, with no
/// path separators.
@length(min: 5, max: 255)
@pattern("^[^/\\\\]+\\.[cC][sS][vV]$")
string KeysImportFileName

/// S3 key of an uploaded keys-import CSV, as returned by GetKeysImportUploadUrl.
/// The server only accepts keys it issued for the same namespace and source.
@length(min: 1, max: 1024)
string KeysImportS3Key

/// Identifier of a keys-import job: its start time (UTC) and a random suffix,
/// e.g. 20261104T153012Z-1a2b3c4d. Sorts by start time.
@pattern("^[0-9]{8}T[0-9]{6}Z-[0-9a-f]{8}$")
string KeysImportJobId

/// What happens to one CSV row.
enum KeysImportRowOutcome {
    /// The row adds a key.
    ADD

    /// The table already has this key; nothing to add.
    ALREADY_THERE

    /// The row repeats an earlier row of the file.
    DUPLICATE

    /// The row is valid but not applied (the table was rejected, or it already
    /// has a different primary key, which is kept).
    SKIPPED

    /// The row failed a check (unknown table or column, ambiguous name, bad format).
    ERROR
}

/// State of a keys-import job.
enum KeysImportJobStatus {
    /// The background worker is adding keys.
    IN_PROGRESS

    /// The run finished. Individual tables may still have failed; see tablesFailed.
    COMPLETED

    /// The run could not be carried out (e.g. the file could not be read).
    FAILED
}

/// A CSV row worth showing to the steward: every row that is not ADD, and ADD
/// rows that carry a warning.
structure KeysImportRowIssue {
    /// File line number (the header is line 1).
    @required
    line: Integer

    @required
    outcome: KeysImportRowOutcome

    /// Why the row was not added, or what is wrong with it.
    message: String

    /// A note on a row that is added anyway (e.g. the column already has a
    /// different foreign key; both are kept).
    warning: String
}

/// Rows worth showing to the steward, in file order.
list KeysImportRowIssueList {
    member: KeysImportRowIssue
}

/// Row counts of a checked CSV file.
structure KeysImportCounts {
    /// Key rows read from the file, including rows with errors.
    @required
    rowsRead: Integer

    /// Rows that add a key.
    @required
    toAdd: Integer

    /// Rows whose key the table already has.
    @required
    alreadyThere: Integer

    /// Rows repeating an earlier row of the file.
    @required
    duplicates: Integer

    /// Valid rows not applied (rejected table, different primary key already set).
    @required
    skipped: Integer

    /// Rows that failed a check.
    @required
    errors: Integer

    /// Tables that will get at least one key.
    @required
    tablesToUpdate: Integer
}

/// A table whose keys could not be saved by the background worker.
structure KeysImportTableFailure {
    /// Table identifier (database.table).
    @required
    tableId: String

    /// Why the keys could not be saved.
    @required
    message: String
}

/// Tables whose keys could not be saved.
list KeysImportTableFailureList {
    member: KeysImportTableFailure
}

/// One keys-import run and its results.
structure KeysImportJob {
    @required
    jobId: KeysImportJobId

    @required
    status: KeysImportJobStatus

    /// Name of the uploaded CSV file.
    @required
    fileName: String

    /// When the run started.
    @required
    startedAt: Timestamp

    /// When the run finished; absent while IN_PROGRESS.
    completedAt: Timestamp

    /// Who started the run (email, or subject when no email).
    startedBy: String

    /// Row counts of the file, as checked when the run started.
    @required
    counts: KeysImportCounts

    /// Tables that got at least one key.
    tablesUpdated: Integer

    /// Tables with nothing left to add when the worker reached them.
    tablesUnchanged: Integer

    /// Tables whose keys could not be saved.
    tablesFailed: Integer

    /// Keys added (each foreign key, plus one per primary key).
    keysAdded: Integer

    /// Tables whose keys could not be saved (at most 100; see errorReportUrl for all).
    tableFailures: KeysImportTableFailureList

    /// Rows worth showing to the steward (at most 100; see errorReportUrl for all).
    rowIssues: KeysImportRowIssueList

    /// True when rowIssues or tableFailures were cut to 100 entries.
    issuesTruncated: Boolean

    /// Pre-signed URL of a CSV with every row issue and table failure; present
    /// once the run has finished and there is at least one.
    errorReportUrl: String

    /// Why the run FAILED as a whole.
    errorMessage: String
}

/// Keys-import runs, newest first.
list KeysImportJobList {
    member: KeysImportJob
}

// ── Operations ────────────────────────────────────────────────────────────────
/// Get a pre-signed S3 PUT URL for uploading a keys-import CSV for a DATABASE
/// source. Upload the file there, then call PreviewKeysImport with the returned
/// s3Key.
@http(method: "POST", uri: "/namespaces/{namespaceId}/sources/{sourceId}/keys-import/upload-url")
operation GetKeysImportUploadUrl {
    input := {
        @required
        @httpLabel
        namespaceId: Uuid

        /// Identifier of the DATABASE source whose tables get the keys.
        @required
        @httpLabel
        sourceId: String

        @required
        fileName: KeysImportFileName
    }

    output := {
        /// Pre-signed S3 PUT URL. Upload the CSV here with Content-Type: text/csv.
        @required
        uploadUrl: String

        @required
        s3Key: KeysImportS3Key

        /// Seconds until the pre-signed URL expires.
        @required
        expiresIn: Integer
    }

    errors: [
        ValidationError
        NotFoundError
        AccessDeniedError
    ]
}

/// Check an uploaded keys-import CSV against the source's tables and report what
/// would be added. Writes nothing.
@http(method: "POST", uri: "/namespaces/{namespaceId}/sources/{sourceId}/keys-import/preview")
operation PreviewKeysImport {
    input := {
        @required
        @httpLabel
        namespaceId: Uuid

        /// Identifier of the DATABASE source whose tables get the keys.
        @required
        @httpLabel
        sourceId: String

        @required
        s3Key: KeysImportS3Key
    }

    output := {
        @required
        counts: KeysImportCounts

        /// Rows worth showing to the steward (at most 1000).
        @required
        rowIssues: KeysImportRowIssueList

        /// True when rowIssues was cut to 1000 entries.
        @required
        rowIssuesTruncated: Boolean
    }

    errors: [
        ValidationError
        NotFoundError
        ConflictError
        AccessDeniedError
    ]
}

/// Start adding the keys of an uploaded CSV to the source's tables. Checks the
/// file again, returns 202 with a job id, and adds the keys in the background.
/// Existing keys are never changed or removed. Poll GetKeysImportJob for progress.
@http(method: "POST", uri: "/namespaces/{namespaceId}/sources/{sourceId}/keys-import", code: 202)
operation StartKeysImport {
    input := {
        @required
        @httpLabel
        namespaceId: Uuid

        /// Identifier of the DATABASE source whose tables get the keys.
        @required
        @httpLabel
        sourceId: String

        @required
        s3Key: KeysImportS3Key
    }

    output := {
        @required
        jobId: KeysImportJobId

        @required
        status: KeysImportJobStatus
    }

    errors: [
        ValidationError
        NotFoundError
        ConflictError
        AccessDeniedError
    ]
}

/// Get one keys-import run: status, counts, failures and the error report.
@readonly
@http(method: "GET", uri: "/namespaces/{namespaceId}/sources/{sourceId}/keys-import/{jobId}")
operation GetKeysImportJob {
    input := {
        @required
        @httpLabel
        namespaceId: Uuid

        /// Identifier of the DATABASE source the run belongs to.
        @required
        @httpLabel
        sourceId: String

        @required
        @httpLabel
        jobId: KeysImportJobId
    }

    output := {
        @required
        job: KeysImportJob
    }

    errors: [
        NotFoundError
        AccessDeniedError
    ]
}

/// List a source's keys-import runs, newest first.
@readonly
@http(method: "GET", uri: "/namespaces/{namespaceId}/sources/{sourceId}/keys-import")
@paginated(inputToken: "nextToken", outputToken: "nextToken", pageSize: "maxResults", items: "items")
operation ListKeysImportJobs {
    input := with [PaginatedInput] {
        @required
        @httpLabel
        namespaceId: Uuid

        /// Identifier of the DATABASE source whose runs to list.
        @required
        @httpLabel
        sourceId: String

        /// Pagination token from a previous response.
        @httpQuery("nextToken")
        nextToken: String

        /// Maximum number of runs to return per page.
        @httpQuery("maxResults")
        maxResults: Integer
    }

    output := with [PaginatedOutput] {
        /// Runs, newest first.
        @required
        items: KeysImportJobList
    }

    errors: [
        NotFoundError
        AccessDeniedError
    ]
}
