"""
Web evidence ingestion service.

Fetches recent web sources and turns them into a structured markdown seed
document that can be fed into the existing MiroFish pipeline.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from html import unescape
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

import httpx

from ..config import Config
from ..utils.file_parser import split_text_into_chunks
from ..utils.llm_client import LLMClient
from ..utils.logger import get_logger
from ..utils.project_brief import build_project_brief_context, normalize_project_brief


logger = get_logger('mirofish.web_evidence')

SOCIAL_DOMAINS = (
    "x.com",
    "twitter.com",
    "facebook.com",
    "instagram.com",
    "tiktok.com",
    "youtube.com",
    "reddit.com",
    "t.me",
    "telegram.me",
)

OFFICIAL_HINTS = (
    ".gov",
    ".gob.",
    ".edu",
    "cne",
    "tce",
    "fiscalia",
    "contraloria",
    "municipio",
    "asamblea",
    "presidencia",
)


def normalize_web_sources(raw: Any) -> List[str]:
    """Normalize URL input from JSON, plain text or lists."""
    if raw is None:
        return []

    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            return []
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            parsed = re.split(r'[\n,]+', text)
    elif isinstance(raw, list):
        parsed = raw
    else:
        return []

    urls: List[str] = []
    seen = set()
    for item in parsed:
        candidate = str(item).strip()
        if not candidate:
            continue
        if not re.match(r'^https?://', candidate, flags=re.IGNORECASE):
            continue
        if candidate in seen:
            continue
        seen.add(candidate)
        urls.append(candidate)

    return urls[:Config.WEB_EVIDENCE_MAX_SOURCES]


class WebEvidenceIngestor:
    """Fetch and structure recent web evidence for graph seeding."""

    def __init__(self):
        self.timeout = httpx.Timeout(
            connect=float(Config.WEB_EVIDENCE_FETCH_TIMEOUT),
            read=float(Config.WEB_EVIDENCE_FETCH_TIMEOUT),
            write=float(Config.WEB_EVIDENCE_FETCH_TIMEOUT),
            pool=float(min(Config.WEB_EVIDENCE_FETCH_TIMEOUT, 10)),
        )

    def ingest(
        self,
        raw_sources: Any,
        *,
        notes: str = "",
        project_brief: Optional[Dict[str, Any]] = None,
        simulation_requirement: str = "",
    ) -> Dict[str, Any]:
        """Fetch sources and render a markdown evidence update."""
        urls = normalize_web_sources(raw_sources)
        normalized_notes = (notes or "").strip()
        normalized_brief = normalize_project_brief(project_brief)

        if not urls and not normalized_notes:
            return {
                "filename": "06_actualizacion_web.md",
                "markdown": "",
                "sources": [],
                "warnings": [],
                "analysis": {},
            }

        sources: List[Dict[str, Any]] = []
        warnings: List[str] = []

        for url in urls:
            try:
                sources.append(self._fetch_source(url))
            except Exception as exc:
                logger.warning("Failed to ingest %s: %s", url, exc)
                warnings.append(f"{url}: {exc}")

        analysis = self._analyze_sources(
            sources=sources,
            notes=normalized_notes,
            project_brief=normalized_brief,
            simulation_requirement=simulation_requirement,
        )
        markdown = self._render_markdown(
            sources=sources,
            warnings=warnings,
            notes=normalized_notes,
            project_brief=normalized_brief,
            simulation_requirement=simulation_requirement,
            analysis=analysis,
        )

        return {
            "filename": "06_actualizacion_web.md",
            "markdown": markdown,
            "sources": sources,
            "warnings": warnings,
            "analysis": analysis,
        }

    def _fetch_source(self, url: str) -> Dict[str, Any]:
        headers = {
            "User-Agent": Config.WEB_EVIDENCE_USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/pdf,text/plain,text/markdown;q=0.9,*/*;q=0.8",
        }

        with httpx.Client(
            follow_redirects=True,
            headers=headers,
            timeout=self.timeout,
        ) as client:
            response = client.get(url)
            response.raise_for_status()

        content_type = (response.headers.get("content-type") or "").lower()
        domain = urlparse(str(response.url)).netloc.lower()
        fetched_at = datetime.now(timezone.utc).isoformat()

        if "application/pdf" in content_type or str(response.url).lower().endswith(".pdf"):
            title, text = self._extract_pdf_content(response.content, url)
        elif any(token in content_type for token in ("text/plain", "text/markdown", "application/json")):
            title = self._guess_title_from_url(url)
            text = response.text
        else:
            title, text = self._extract_html_content(response.text, url)

        cleaned = self._clean_text(text)
        if not cleaned:
            raise ValueError("未能提取有效正文")

        excerpt = cleaned[:Config.WEB_EVIDENCE_MAX_SOURCE_CHARS].strip()
        word_count = len(excerpt.split())

        return {
            "url": str(response.url),
            "domain": domain,
            "title": title or self._guess_title_from_url(url),
            "content_type": content_type or "unknown",
            "fetched_at": fetched_at,
            "source_strength": self._infer_source_strength(domain),
            "excerpt": excerpt,
            "word_count": word_count,
        }

    def _extract_html_content(self, html: str, url: str) -> tuple[str, str]:
        title_match = re.search(r'<title[^>]*>(.*?)</title>', html, flags=re.IGNORECASE | re.DOTALL)
        title = self._clean_text(unescape(title_match.group(1))) if title_match else self._guess_title_from_url(url)

        content = re.sub(r'<!--.*?-->', ' ', html, flags=re.DOTALL)
        content = re.sub(
            r'<(script|style|noscript|svg|iframe|header|footer|nav)[^>]*>.*?</\1>',
            ' ',
            content,
            flags=re.IGNORECASE | re.DOTALL,
        )
        content = re.sub(r'<br\s*/?>', '\n', content, flags=re.IGNORECASE)
        content = re.sub(
            r'</(p|div|section|article|main|li|ul|ol|h1|h2|h3|h4|h5|h6|tr|table|blockquote)>',
            '\n',
            content,
            flags=re.IGNORECASE,
        )
        content = re.sub(r'<[^>]+>', ' ', content)
        content = unescape(content)
        cleaned = self._clean_text(content)

        paragraphs = []
        for paragraph in re.split(r'\n{2,}', cleaned):
            candidate = paragraph.strip()
            if len(candidate) < 45:
                continue
            lowered = candidate.lower()
            if lowered.startswith(("cookie", "subscribe", "sign in", "all rights reserved")):
                continue
            paragraphs.append(candidate)
            if len(paragraphs) >= 24:
                break

        return title, "\n\n".join(paragraphs) if paragraphs else cleaned

    def _extract_pdf_content(self, content: bytes, url: str) -> tuple[str, str]:
        try:
            import fitz  # PyMuPDF
        except ImportError as exc:
            raise RuntimeError("PyMuPDF 未安装，无法处理PDF来源") from exc

        text_parts = []
        title = self._guess_title_from_url(url)
        with fitz.open(stream=content, filetype="pdf") as doc:
            metadata_title = (doc.metadata or {}).get("title")
            if metadata_title:
                title = metadata_title.strip()
            for page in doc:
                page_text = page.get_text().strip()
                if page_text:
                    text_parts.append(page_text)

        return title, "\n\n".join(text_parts)

    def _analyze_sources(
        self,
        *,
        sources: List[Dict[str, Any]],
        notes: str,
        project_brief: Dict[str, str],
        simulation_requirement: str,
    ) -> Dict[str, Any]:
        """Ask the base LLM for a compact operational summary. Fallback safely."""
        if not sources and not notes:
            return {}

        source_blocks = []
        for index, source in enumerate(sources, start=1):
            excerpt = source["excerpt"][:1800]
            source_blocks.append(
                f"[Fuente {index}]\n"
                f"Título: {source['title']}\n"
                f"URL: {source['url']}\n"
                f"Dominio: {source['domain']}\n"
                f"Fuerza estimada: {source['source_strength']}\n"
                f"Texto:\n{excerpt}"
            )

        brief_context = build_project_brief_context(project_brief)
        prompt_parts = []
        if simulation_requirement:
            prompt_parts.append(f"Objetivo de simulación:\n{simulation_requirement}")
        if brief_context:
            prompt_parts.append(brief_context)
        if notes:
            prompt_parts.append(f"Notas manuales del usuario:\n{notes[:2000]}")
        if source_blocks:
            prompt_parts.append("Fuentes recopiladas:\n\n" + "\n\n".join(source_blocks))

        try:
            client = LLMClient(stage="evidence_refresh")
            response = client.chat_json(
                [
                    {
                        "role": "system",
                        "content": (
                            "Eres un analista de evidencia para simulaciones sociales y electorales. "
                            "Debes resumir solo lo soportado por las fuentes dadas, sin inventar. "
                            "Clasifica cada punto como hecho_confirmado, senal_media, rumor_o_no_confirmado o contexto. "
                            "Cuando cites soporte, usa solo numeros de fuente."
                        ),
                    },
                    {
                        "role": "user",
                        "content": (
                            "Devuelve JSON con esta forma exacta:\n"
                            "{\n"
                            '  "overview": "resumen breve",\n'
                            '  "evidence_items": [\n'
                            '    {"type": "hecho_confirmado|senal_media|rumor_o_no_confirmado|contexto", '
                            '"statement": "...", "actors": ["..."], "time_reference": "...", "source_ids": [1]}\n'
                            "  ],\n"
                            '  "scenario_implications": ["..."],\n'
                            '  "collection_gaps": ["..."]\n'
                            "}\n\n"
                            + "\n\n".join(prompt_parts)
                        ),
                    },
                ],
                temperature=0.1,
                max_tokens=1800,
            )
            if isinstance(response, dict):
                return response
        except Exception as exc:
            logger.warning("Web evidence LLM summary failed, using fallback: %s", exc)

        fallback_items = []
        for index, source in enumerate(sources[:5], start=1):
            fallback_items.append({
                "type": "contexto",
                "statement": source["title"],
                "actors": [source["domain"]],
                "time_reference": source["fetched_at"][:10],
                "source_ids": [index],
            })

        return {
            "overview": "Resumen automático no disponible; se adjuntan extractos limpios por fuente.",
            "evidence_items": fallback_items,
            "scenario_implications": [],
            "collection_gaps": [],
        }

    def _render_markdown(
        self,
        *,
        sources: List[Dict[str, Any]],
        warnings: List[str],
        notes: str,
        project_brief: Dict[str, str],
        simulation_requirement: str,
        analysis: Dict[str, Any],
    ) -> str:
        now_local = datetime.now().astimezone().isoformat()
        lines = [
            "# Actualización Web de Evidencia",
            "",
            "Este archivo complementa los documentos semilla con información reciente obtenida desde internet. "
            "No reemplaza los archivos base; sirve para refrescar señales, hechos recientes y cambios de contexto.",
            "",
            "## Metadatos",
            f"- Generado en: {now_local}",
            f"- Fuentes web procesadas: {len(sources)}",
            f"- Fuentes con error: {len(warnings)}",
        ]

        if simulation_requirement:
            lines.append(f"- Objetivo de simulación: {simulation_requirement}")

        geography = project_brief.get("geography")
        prediction_horizon = project_brief.get("prediction_horizon")
        if geography:
            lines.append(f"- Geografía prioritaria: {geography}")
        if prediction_horizon:
            lines.append(f"- Horizonte temporal: {prediction_horizon}")

        overview = (analysis or {}).get("overview")
        if overview:
            lines.extend([
                "",
                "## Resumen Operativo",
                overview.strip(),
            ])

        evidence_items = (analysis or {}).get("evidence_items") or []
        if evidence_items:
            lines.extend([
                "",
                "## Hechos, Señales y Rumores Clasificados",
            ])
            for item in evidence_items[:12]:
                statement = str(item.get("statement", "")).strip()
                if not statement:
                    continue
                evidence_type = str(item.get("type", "contexto")).strip()
                actors = ", ".join([str(actor).strip() for actor in item.get("actors", []) if str(actor).strip()])
                time_reference = str(item.get("time_reference", "")).strip()
                source_ids = ", ".join(str(source_id) for source_id in item.get("source_ids", []) if source_id)
                lines.append(f"- [{evidence_type}] {statement}")
                if actors:
                    lines.append(f"  Actores: {actors}")
                if time_reference:
                    lines.append(f"  Referencia temporal: {time_reference}")
                if source_ids:
                    lines.append(f"  Soporte: fuentes {source_ids}")

        scenario_implications = (analysis or {}).get("scenario_implications") or []
        if scenario_implications:
            lines.extend([
                "",
                "## Implicaciones para la Simulación",
            ])
            lines.extend([f"- {item}" for item in scenario_implications[:8] if str(item).strip()])

        collection_gaps = (analysis or {}).get("collection_gaps") or []
        if collection_gaps:
            lines.extend([
                "",
                "## Brechas de Recolección",
            ])
            lines.extend([f"- {item}" for item in collection_gaps[:8] if str(item).strip()])

        if notes:
            lines.extend([
                "",
                "## Notas Manuales del Usuario",
                notes.strip(),
            ])

        if sources:
            lines.extend([
                "",
                "## Fuentes Web Procesadas",
            ])
            for index, source in enumerate(sources, start=1):
                excerpt_chunks = split_text_into_chunks(
                    source["excerpt"],
                    chunk_size=min(Config.WEB_EVIDENCE_MAX_SOURCE_CHARS, 1600),
                    overlap=0,
                )
                excerpt = excerpt_chunks[0] if excerpt_chunks else source["excerpt"]
                lines.extend([
                    "",
                    f"### Fuente {index}: {source['title']}",
                    f"- URL: {source['url']}",
                    f"- Dominio: {source['domain']}",
                    f"- Capturado: {source['fetched_at']}",
                    f"- Fuerza estimada: {source['source_strength']}",
                    f"- Tipo de contenido: {source['content_type']}",
                    f"- Longitud extraída: {source['word_count']} palabras aproximadas",
                    "",
                    "#### Extracto limpio",
                    excerpt.strip(),
                ])

        if warnings:
            lines.extend([
                "",
                "## Fuentes no Procesadas",
            ])
            lines.extend([f"- {warning}" for warning in warnings])

        return "\n".join(lines).strip() + "\n"

    @staticmethod
    def _infer_source_strength(domain: str) -> str:
        lowered = domain.lower()
        if any(hint in lowered for hint in OFFICIAL_HINTS):
            return "fuerte"
        if any(social in lowered for social in SOCIAL_DOMAINS):
            return "debil"
        return "media"

    @staticmethod
    def _guess_title_from_url(url: str) -> str:
        parsed = urlparse(url)
        slug = parsed.path.rstrip('/').split('/')[-1] or parsed.netloc
        slug = re.sub(r'[-_]+', ' ', slug)
        slug = re.sub(r'\.[a-z0-9]{2,4}$', '', slug, flags=re.IGNORECASE)
        return slug.strip() or parsed.netloc or url

    @staticmethod
    def _clean_text(text: str) -> str:
        normalized = text.replace('\r\n', '\n').replace('\r', '\n')
        normalized = re.sub(r'[ \t]+', ' ', normalized)
        normalized = re.sub(r'\n[ \t]+', '\n', normalized)
        normalized = re.sub(r'\n{3,}', '\n\n', normalized)
        return normalized.strip()
