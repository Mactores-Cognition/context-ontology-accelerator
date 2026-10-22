# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Router for document-based ontology induction."""

import asyncio
import uuid
from datetime import datetime

from coa_common.constants import DEFAULT_LLM_MODEL_ID
from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile

from coa_ontology.inducer.schemas import JobResponse, JobStatus
from coa_ontology.inducer.services.document_pipeline import DocumentPipeline
from coa_ontology.inducer.services.embedding_generator import EmbeddingGeneratorClient
from coa_ontology.inducer.services.llm import LLMClient
from coa_ontology.inducer.services.ontology_catalog import OntologyCatalogClient

router = APIRouter()

_jobs: dict[str, JobResponse] = {}


def _build_pipeline(config: dict) -> DocumentPipeline:
    return DocumentPipeline(
        llm=LLMClient(
            model_id=config.get("llm_model_id", DEFAULT_LLM_MODEL_ID),
            region=config.get("llm_region", "us-east-1"),
        ),
        embedding_generator=EmbeddingGeneratorClient(config["embedding_generator_url"]),
        ontology_catalog=OntologyCatalogClient(config["ontology_catalog_url"]),
    )


async def _run_doc_induction(job_id: str, text: str, uri_prefix: str, label: str, confidence: float, config: dict):
    job = _jobs[job_id]
    try:
        pipeline = _build_pipeline(config)
        job.status = JobStatus.GENERATING_EMBEDDINGS
        report, graph = await pipeline.run(
            document_text=text,
            uri_prefix=uri_prefix,
            label=label,
            confidence_threshold=confidence,
            source_label=label,
        )
        report.job_id = job_id
        job.report = report
        job.status = JobStatus.COMPLETED
    except Exception as e:
        job.status = JobStatus.FAILED
        job.error = str(e)


@router.post("/", response_model=JobResponse, status_code=202)
async def induce_from_document(
    request: Request,
    file: UploadFile = File(...),  # noqa: B008 — FastAPI File() in defaults is the standard idiom
    uri_prefix: str = Form("http://example.org/doc-induced#"),
    label: str = Form("Document-Induced Ontology"),
    confidence_threshold: float = Form(0.80),
):
    """Induce ontology from an uploaded document."""
    text = (await file.read()).decode("utf-8", errors="replace")
    job_id = str(uuid.uuid4())
    _jobs[job_id] = JobResponse(job_id=job_id, status=JobStatus.PENDING, created_at=datetime.utcnow())
    config = request.app.state.config
    asyncio.create_task(_run_doc_induction(job_id, text, uri_prefix, label, confidence_threshold, config))
    return _jobs[job_id]


@router.post("/text", response_model=JobResponse, status_code=202)
async def induce_from_text(request: Request):
    """Induce ontology from raw text in request body."""
    body = await request.json()
    text = body["text"]
    uri_prefix = body.get("uri_prefix", "http://example.org/doc-induced#")
    label = body.get("label", "Document-Induced Ontology")
    confidence = body.get("confidence_threshold", 0.80)

    job_id = str(uuid.uuid4())
    _jobs[job_id] = JobResponse(job_id=job_id, status=JobStatus.PENDING, created_at=datetime.utcnow())
    config = request.app.state.config
    asyncio.create_task(_run_doc_induction(job_id, text, uri_prefix, label, confidence, config))
    return _jobs[job_id]


@router.get("/jobs/{job_id}", response_model=JobResponse)
async def get_job(job_id: str):
    """Return the status of a previously started document-induction job.

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
