# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Router for batch extraction and alignment operations."""

import asyncio
import uuid
from datetime import datetime

from coa_common.constants import DEFAULT_LLM_MODEL_ID
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from coa_ontology.inducer.schemas import JobResponse, JobStatus

router = APIRouter()
_jobs: dict[str, JobResponse] = {}


class BatchRequest(BaseModel):
    """Request body for kicking off batch extraction across source directories."""

    source_dirs: list[str]
    output_dir: str = "/tmp/batch-output"
    workers: int = 4
    file_glob: str = "**/*.md"
    min_size: int = 1024


class AlignRequest(BaseModel):
    """Request body for kicking off cross-repository ontology alignment."""

    min_repos: int = 2
    workers: int = 4
    max_pairs: int | None = None
    uri_prefix: str = "http://example.org/unified#"


@router.post("/extract", response_model=JobResponse, status_code=202)
async def batch_extract(body: BatchRequest, request: Request):
    """Kick off batch extraction across directories."""
    job_id = str(uuid.uuid4())
    _jobs[job_id] = JobResponse(job_id=job_id, status=JobStatus.PENDING, created_at=datetime.utcnow())

    async def _run():
        from coa_ontology.inducer.services.batch_pipeline import BatchConfig, run_batch
        from coa_ontology.inducer.services.llm import LLMClient

        job = _jobs[job_id]
        try:
            job.status = JobStatus.GENERATING_EMBEDDINGS
            config = BatchConfig(
                source_dirs=body.source_dirs,
                output_dir=body.output_dir,
                workers=body.workers,
                file_glob=body.file_glob,
                min_size=body.min_size,
            )
            llm = LLMClient(
                model_id=request.app.state.config.get("llm_model_id", DEFAULT_LLM_MODEL_ID),
                region=request.app.state.config.get("llm_region", "us-east-1"),
            )
            results = run_batch(config, llm)
            sum(1 for r in results if r.ok)
            job.status = JobStatus.COMPLETED
            job.error = None
        except Exception as e:
            job.status = JobStatus.FAILED
            job.error = str(e)

    asyncio.create_task(_run())
    return _jobs[job_id]


@router.post("/align", response_model=JobResponse, status_code=202)
async def run_alignment(body: AlignRequest, request: Request):
    """Kick off context-aware alignment."""
    job_id = str(uuid.uuid4())
    _jobs[job_id] = JobResponse(job_id=job_id, status=JobStatus.PENDING, created_at=datetime.utcnow())

    async def _run():
        from coa_ontology.inducer.services.alignment_pipeline import (
            AlignmentConfig,
        )
        from coa_ontology.inducer.services.llm import LLMClient

        job = _jobs[job_id]
        try:
            job.status = JobStatus.MATCHING
            # TODO: inject real graph client from config
            LLMClient(
                model_id=request.app.state.config.get("llm_model_id", DEFAULT_LLM_MODEL_ID),
                region=request.app.state.config.get("llm_region", "us-east-1"),
            )
            AlignmentConfig(min_repos=body.min_repos, workers=body.workers, max_pairs=body.max_pairs)
            # pipeline = AlignmentPipeline(graph_client, llm)
            # results = pipeline.run(config)
            # graph = build_unified_ontology(graph_client, results, body.uri_prefix)
            job.status = JobStatus.COMPLETED
        except Exception as e:
            job.status = JobStatus.FAILED
            job.error = str(e)

    asyncio.create_task(_run())
    return _jobs[job_id]


@router.get("/jobs/{job_id}", response_model=JobResponse)
async def get_job(job_id: str):
    """Return the status of a previously submitted batch or alignment job.

    Args:
        job_id: Identifier returned when the job was created.

    Returns:
        The tracked :class:`JobResponse` for the job.

    Raises:
        HTTPException: 404 if no job with the given id exists.
    """
    job = _jobs.get(job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    return job
