// @examples for com.amazon.semanticcontext.keysimport operations.
// Kept apart from the operation definitions, as in the other *-examples.smithy
// files; `apply` merges the @examples trait back onto each operation, so the
// doc-coverage gate in linters.smithy still holds.
$version: "2"

namespace com.amazon.semanticcontext.keysimport

apply GetKeysImportUploadUrl @examples([
    {
        title: "Get an upload URL for a keys CSV"
        input: { namespaceId: "3f2504e0-4f89-41d3-9a0c-0305e82c3301", sourceId: "src-7f3a9c1e", fileName: "erp-keys.csv" }
        output: { uploadUrl: "https://coa-dev-keys-import-111122223333.s3.amazonaws.com/keys-import/3f2504e0-4f89-41d3-9a0c-0305e82c3301/src-7f3a9c1e/5b1c9e2a/erp-keys.csv?X-Amz-Signature=...", s3Key: "keys-import/3f2504e0-4f89-41d3-9a0c-0305e82c3301/src-7f3a9c1e/5b1c9e2a/erp-keys.csv", expiresIn: 900 }
    }
])

apply PreviewKeysImport @examples([
    {
        title: "Preview a keys CSV with one unknown table"
        input: { namespaceId: "3f2504e0-4f89-41d3-9a0c-0305e82c3301", sourceId: "src-7f3a9c1e", s3Key: "keys-import/3f2504e0-4f89-41d3-9a0c-0305e82c3301/src-7f3a9c1e/5b1c9e2a/erp-keys.csv" }
        output: {
            counts: { rowsRead: 4, toAdd: 2, alreadyThere: 1, duplicates: 0, skipped: 0, errors: 1, tablesToUpdate: 2 }
            rowIssues: [
                {
                    line: 3
                    outcome: "ALREADY_THERE"
                }
                {
                    line: 5
                    outcome: "ERROR"
                    message: "table 'vendor' was not found in this source."
                }
            ]
            rowIssuesTruncated: false
        }
    }
])

apply StartKeysImport @examples([
    {
        title: "Start adding the keys of an uploaded CSV"
        input: { namespaceId: "3f2504e0-4f89-41d3-9a0c-0305e82c3301", sourceId: "src-7f3a9c1e", s3Key: "keys-import/3f2504e0-4f89-41d3-9a0c-0305e82c3301/src-7f3a9c1e/5b1c9e2a/erp-keys.csv" }
        output: { jobId: "20261104T153012Z-1a2b3c4d", status: "IN_PROGRESS" }
    }
])

apply GetKeysImportJob @examples([
    {
        title: "Get a finished keys-import run"
        input: { namespaceId: "3f2504e0-4f89-41d3-9a0c-0305e82c3301", sourceId: "src-7f3a9c1e", jobId: "20261104T153012Z-1a2b3c4d" }
        output: {
            job: {
                jobId: "20261104T153012Z-1a2b3c4d"
                status: "COMPLETED"
                fileName: "erp-keys.csv"
                startedAt: "2026-11-04T15:30:12Z"
                completedAt: "2026-11-04T15:31:40Z"
                startedBy: "steward@example.com"
                counts: { rowsRead: 4, toAdd: 2, alreadyThere: 1, duplicates: 0, skipped: 0, errors: 1, tablesToUpdate: 2 }
                tablesUpdated: 2
                tablesUnchanged: 0
                tablesFailed: 0
                keysAdded: 2
                tableFailures: []
                rowIssues: [
                    {
                        line: 3
                        outcome: "ALREADY_THERE"
                    }
                    {
                        line: 5
                        outcome: "ERROR"
                        message: "table 'vendor' was not found in this source."
                    }
                ]
                issuesTruncated: false
                errorReportUrl: "https://coa-dev-keys-import-111122223333.s3.amazonaws.com/keys-import/3f2504e0-4f89-41d3-9a0c-0305e82c3301/src-7f3a9c1e/reports/20261104T153012Z-1a2b3c4d.csv?X-Amz-Signature=..."
            }
        }
    }
])

apply ListKeysImportJobs @examples([
    {
        title: "List a source's keys-import runs"
        input: { namespaceId: "3f2504e0-4f89-41d3-9a0c-0305e82c3301", sourceId: "src-7f3a9c1e" }
        output: {
            items: [
                {
                    jobId: "20261104T153012Z-1a2b3c4d"
                    status: "COMPLETED"
                    fileName: "erp-keys.csv"
                    startedAt: "2026-11-04T15:30:12Z"
                    completedAt: "2026-11-04T15:31:40Z"
                    counts: { rowsRead: 4, toAdd: 2, alreadyThere: 1, duplicates: 0, skipped: 0, errors: 1, tablesToUpdate: 2 }
                    tablesUpdated: 2
                    keysAdded: 2
                }
            ]
        }
    }
])
