"""Embeddings dashboard — vector DB inspection and similarity search."""

from __future__ import annotations

import math

import structlog
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from app.db import get_conn
from app.pipeline.context_builder import find_similar_by_id

log = structlog.get_logger(__name__)

router = APIRouter(prefix="/embeddings")

_PER_PAGE = 50


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
async def _entity_lookups(paperless):
    """Build {id: name} dicts for correspondents and document types."""
    try:
        correspondents = await paperless.list_correspondents()
        doctypes = await paperless.list_document_types()
        corr_lookup = {c.id: c.name for c in correspondents}
        dt_lookup = {d.id: d.name for d in doctypes}
    except Exception as exc:
        log.error("failed to fetch entity lists", error=str(exc))
        corr_lookup, dt_lookup = {}, {}
    return corr_lookup, dt_lookup


def _query_embeddings(conn, *, query: str = "", page: int = 1):
    """Paginated query against doc_embedding_meta. Returns (rows, total)."""
    if query:
        where = "WHERE title LIKE ?"
        params: tuple = (f"%{query}%",)
    else:
        where = ""
        params = ()

    total = conn.execute(
        f"SELECT COUNT(*) AS c FROM doc_embedding_meta {where}", params
    ).fetchone()["c"]

    offset = (page - 1) * _PER_PAGE
    rows = conn.execute(
        f"""
        SELECT document_id, title, correspondent, doctype, indexed_at
          FROM doc_embedding_meta
         {where}
         ORDER BY indexed_at DESC
         LIMIT ? OFFSET ?
        """,
        (*params, _PER_PAGE, offset),
    ).fetchall()

    return rows, total


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
@router.get("")
async def embeddings_page(request: Request, q: str = "", page: int = 1):
    paperless = request.app.state.paperless
    corr_lookup, dt_lookup = await _entity_lookups(paperless)

    with get_conn() as conn:
        rows, total = _query_embeddings(conn, query=q, page=page)

    total_pages = max(1, math.ceil(total / _PER_PAGE))
    documents = [
        {
            "document_id": r["document_id"],
            "title": r["title"] or f"Document #{r['document_id']}",
            "correspondent_name": corr_lookup.get(r["correspondent"]),
            "doctype_name": dt_lookup.get(r["doctype"]),
            "indexed_at": r["indexed_at"],
        }
        for r in rows
    ]

    paperless_url = paperless.base_url if paperless else ""

    return request.app.state.templates.TemplateResponse(
        request,
        "embeddings.html",
        {
            "total_embedded": total,
            "documents": documents,
            "page": page,
            "total_pages": total_pages,
            "query": q,
            "paperless_url": paperless_url,
        },
    )


@router.get("/search")
async def embeddings_search(request: Request, q: str = "", page: int = 1):
    """HTMX partial — returns table body + pagination only."""
    paperless = request.app.state.paperless
    corr_lookup, dt_lookup = await _entity_lookups(paperless)

    with get_conn() as conn:
        rows, total = _query_embeddings(conn, query=q, page=page)

    total_pages = max(1, math.ceil(total / _PER_PAGE))
    documents = [
        {
            "document_id": r["document_id"],
            "title": r["title"] or f"Document #{r['document_id']}",
            "correspondent_name": corr_lookup.get(r["correspondent"]),
            "doctype_name": dt_lookup.get(r["doctype"]),
            "indexed_at": r["indexed_at"],
        }
        for r in rows
    ]

    paperless_url = paperless.base_url if paperless else ""

    tmpl = request.app.state.templates.get_template("partials/embeddings_table.html")
    return HTMLResponse(
        tmpl.render(
            documents=documents,
            page=page,
            total_pages=total_pages,
            total_embedded=total,
            query=q,
            paperless_url=paperless_url,
        )
    )


@router.get("/similar/{document_id}")
async def similar_documents(request: Request, document_id: int, limit: int = 10):
    """HTMX partial — KNN results for a given document."""
    results = find_similar_by_id(document_id, limit=limit)

    if not results:
        return HTMLResponse(
            '<p class="text-sm text-gray-500 py-4">'
            f"No embedding found for document #{document_id}, or no similar documents."
            "</p>"
        )

    # Enrich with metadata from doc_embedding_meta
    doc_ids = [doc_id for doc_id, _ in results]
    placeholders = ",".join("?" * len(doc_ids))
    with get_conn() as conn:
        meta_rows = conn.execute(
            f"SELECT document_id, title FROM doc_embedding_meta WHERE document_id IN ({placeholders})",
            doc_ids,
        ).fetchall()
    meta_lookup = {r["document_id"]: r["title"] for r in meta_rows}

    similar = [
        {
            "document_id": doc_id,
            "title": meta_lookup.get(doc_id, f"Document #{doc_id}"),
            "distance": round(dist, 4),
        }
        for doc_id, dist in results
    ]

    paperless = request.app.state.paperless
    paperless_url = paperless.base_url if paperless else ""

    tmpl = request.app.state.templates.get_template("partials/similar_results.html")
    return HTMLResponse(
        tmpl.render(
            similar=similar,
            source_id=document_id,
            paperless_url=paperless_url,
        )
    )
