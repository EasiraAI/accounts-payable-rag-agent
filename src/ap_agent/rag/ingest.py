"""Corpus ingestion: documents on disk to citable chunks.

## Chunking unit

One chunk per second-level Markdown heading. This is not a generic choice; it is the
citation unit the corpus itself uses. Every policy cross-reference in these documents takes
the form "FIN-POL-003 §4", and every heading is numbered to match. Aligning chunks to
sections means a retrieved chunk maps exactly onto a reference a finance reviewer would
write by hand, and a citation can be verified by opening the document and reading one
section.

The alternatives were considered and rejected:

- **Fixed-size windows with overlap** (the common default) would split a tolerance table
  from the sentence that qualifies it, and would produce citations like "characters
  1200-1800", which no reviewer can check.
- **Whole documents** would be cheap to cite but would put the entire authority matrix into
  context for a question about freight tolerance, which both wastes context and invites the
  model to mix unrelated thresholds.
- **Sentence-level chunks** would be precise but would strip the heading that gives a
  threshold its scope, and a bare sentence such as "the limit is AUD 100 or 2%" is
  dangerously ambiguous without the section that says it applies to services.

Sections in this corpus are short, between roughly 40 and 200 words, so no chunk needs
splitting. A guard raises if that ever stops being true rather than silently truncating.

## Metadata

Front matter travels with every chunk. The three fields that carry control weight are
``document_id``, ``version`` and ``status``: they are what let retrieval demote the
superseded authority matrix and label the adversarial supplier notice, which is the part of
the pipeline that actually handles the corpus traps.
"""

from __future__ import annotations

import hashlib
import re
from datetime import date
from pathlib import Path
from typing import Final

import frontmatter
from pydantic import BaseModel, ConfigDict, Field

from ap_agent.domain.enums import DocumentStatus

#: A section heading, e.g. "## 2. Tolerances" or "## Purpose".
_HEADING = re.compile(r"^##\s+(?P<heading>.+?)\s*$", re.MULTILINE)

#: A leading section number inside a heading, e.g. the "2" in "2. Tolerances".
_SECTION_NUMBER = re.compile(r"^(?P<number>\d+(?:\.\d+)*)\.?\s+(?P<title>.*)$")

#: Chunks longer than this are a signal that the corpus structure changed and the chunking
#: assumption needs revisiting. Chosen as roughly 400 tokens of English prose.
MAX_CHUNK_CHARACTERS: Final = 2_400

#: Document-type taxonomy used for retrieval filtering.
DOC_TYPE_POLICY: Final = "policy"
DOC_TYPE_POLICY_SUPERSEDED: Final = "policy_superseded"
DOC_TYPE_EXTERNAL_UNVERIFIED: Final = "external_unverified"
DOC_TYPE_INTERNAL_OTHER: Final = "internal_other"


class Chunk(BaseModel):
    """One citable passage with the full provenance of its document."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    chunk_id: str
    document_id: str
    title: str
    version: str
    status: DocumentStatus
    classification: str
    owner: str = ""
    jurisdiction: str = ""
    effective_date: date | None = None
    superseded_date: date | None = None
    tags: list[str] = Field(default_factory=list)
    doc_type: str
    section: str
    heading: str
    ordinal: int
    text: str
    source_path: str

    @property
    def citation_reference(self) -> str:
        base = f"{self.document_id} {self.section}".strip()
        if self.status is not DocumentStatus.CURRENT:
            return f"{base} [{self.status.value}]"
        return base


class IngestedCorpus(BaseModel):
    """The chunked corpus plus the hash that identifies the source it came from."""

    model_config = ConfigDict(extra="forbid")

    chunks: list[Chunk]
    corpus_hash: str
    document_count: int
    source_dir: str

    def by_document(self) -> dict[str, list[Chunk]]:
        grouped: dict[str, list[Chunk]] = {}
        for chunk in self.chunks:
            grouped.setdefault(chunk.document_id, []).append(chunk)
        return grouped


def _classify_doc_type(document_id: str, status: DocumentStatus, classification: str) -> str:
    """Assign a retrieval filter class.

    Derived from status and classification rather than from the filename, because a
    filename is not a control. An externally supplied document is never classified as
    policy whatever it calls itself: ADV-001 titles itself an urgent payment instruction
    and asserts that a finance director approved it, and no amount of self-description
    should promote it.
    """
    if status is DocumentStatus.UNTRUSTED:
        return DOC_TYPE_EXTERNAL_UNVERIFIED
    if status is DocumentStatus.SUPERSEDED:
        return DOC_TYPE_POLICY_SUPERSEDED
    if document_id.upper().startswith("FIN-POL") and "external" not in classification.lower():
        return DOC_TYPE_POLICY
    return DOC_TYPE_INTERNAL_OTHER


def _section_label(heading: str, ordinal: int) -> tuple[str, str]:
    """Split a heading into a section label and its title.

    "2. Tolerances" becomes ("§2", "Tolerances"), matching how the corpus cites itself.
    An unnumbered heading falls back to its ordinal so that every chunk is still citable.
    """
    match = _SECTION_NUMBER.match(heading)
    if match:
        return f"§{match.group('number')}", match.group("title").strip()
    return f"§{ordinal}", heading.strip()


def _parse_date(value: object) -> date | None:
    if isinstance(value, date):
        return value
    if isinstance(value, str) and value:
        try:
            return date.fromisoformat(value)
        except ValueError:
            return None
    return None


def chunk_document(path: Path) -> list[Chunk]:
    """Parse one Markdown file into section chunks."""
    parsed = frontmatter.loads(path.read_text(encoding="utf-8"))
    metadata = parsed.metadata
    document_id = str(metadata.get("document_id") or path.stem).strip()
    status_raw = str(metadata.get("status") or "current").strip().lower()
    try:
        status = DocumentStatus(status_raw)
    except ValueError:
        # An unrecognised status is treated as untrusted rather than current. Defaulting an
        # unknown provenance to "authoritative" is the failure mode this system exists to
        # avoid.
        status = DocumentStatus.UNTRUSTED
    classification = str(metadata.get("classification") or "unknown").strip()
    title = str(metadata.get("title") or document_id).strip()
    version = str(metadata.get("version") or "unknown").strip()
    tags_raw = metadata.get("tags") or []
    tags = [str(tag) for tag in tags_raw] if isinstance(tags_raw, list) else []

    body = parsed.content
    doc_type = _classify_doc_type(document_id, status, classification)

    headings = list(_HEADING.finditer(body))
    chunks: list[Chunk] = []

    if not headings:
        # A document with no second-level headings becomes a single chunk. ADV-001 and
        # ADV-002 are shaped this way, which is itself informative: an unstructured document
        # asserting policy is unlike every genuine policy in the corpus.
        text = body.strip()
        if text:
            chunks.append(
                _build_chunk(
                    document_id=document_id,
                    title=title,
                    version=version,
                    status=status,
                    classification=classification,
                    metadata=metadata,
                    tags=tags,
                    doc_type=doc_type,
                    section="§0",
                    heading=title,
                    ordinal=0,
                    text=text,
                    path=path,
                )
            )
        return chunks

    # Text before the first "##" is the document's own heading and preamble. It is kept as
    # chunk zero so that a query matching only the title still retrieves something citable.
    preamble = body[: headings[0].start()].strip()
    preamble = re.sub(r"^#\s+.*$", "", preamble, count=1, flags=re.MULTILINE).strip()
    if preamble:
        chunks.append(
            _build_chunk(
                document_id=document_id,
                title=title,
                version=version,
                status=status,
                classification=classification,
                metadata=metadata,
                tags=tags,
                doc_type=doc_type,
                section="§0",
                heading=f"{title} (preamble)",
                ordinal=0,
                text=preamble,
                path=path,
            )
        )

    for ordinal, match in enumerate(headings, start=1):
        end = headings[ordinal].start() if ordinal < len(headings) else len(body)
        section_body = body[match.end() : end].strip()
        heading_text = match.group("heading").strip()
        section, heading_title = _section_label(heading_text, ordinal)
        # The heading is prepended to the chunk text so that a query naming the section topic
        # matches lexically even when the body uses different words, and so the chunk reads
        # correctly when quoted in a citation.
        text = f"{heading_title}\n\n{section_body}".strip() if section_body else heading_title
        if len(text) > MAX_CHUNK_CHARACTERS:
            raise ValueError(
                f"{path.name} section {section} is {len(text)} characters, above the "
                f"{MAX_CHUNK_CHARACTERS} limit. Section-level chunking assumes short "
                "sections; revisit the chunking strategy rather than truncating."
            )
        chunks.append(
            _build_chunk(
                document_id=document_id,
                title=title,
                version=version,
                status=status,
                classification=classification,
                metadata=metadata,
                tags=tags,
                doc_type=doc_type,
                section=section,
                heading=heading_title,
                ordinal=ordinal,
                text=text,
                path=path,
            )
        )
    return chunks


def _build_chunk(
    *,
    document_id: str,
    title: str,
    version: str,
    status: DocumentStatus,
    classification: str,
    metadata: dict[str, object],
    tags: list[str],
    doc_type: str,
    section: str,
    heading: str,
    ordinal: int,
    text: str,
    path: Path,
) -> Chunk:
    return Chunk(
        chunk_id=f"{document_id}#{ordinal}",
        document_id=document_id,
        title=title,
        version=version,
        status=status,
        classification=classification,
        owner=str(metadata.get("owner") or ""),
        jurisdiction=str(metadata.get("jurisdiction") or ""),
        effective_date=_parse_date(metadata.get("effective_date")),
        superseded_date=_parse_date(metadata.get("superseded_date")),
        tags=tags,
        doc_type=doc_type,
        section=section,
        heading=heading,
        ordinal=ordinal,
        text=text,
        source_path=path.name,
    )


def ingest_corpus(corpus_dir: Path) -> IngestedCorpus:
    """Chunk every Markdown document in a directory.

    Files are processed in sorted order and the corpus hash covers each file's name and
    bytes, so the hash changes if and only if the source changes. That is what lets the
    index skip a rebuild safely and what makes an index attributable to a corpus state
    during an audit.
    """
    if not corpus_dir.is_dir():
        raise FileNotFoundError(f"corpus directory not found: {corpus_dir}")

    paths = sorted(path for path in corpus_dir.glob("*.md") if path.is_file())
    if not paths:
        raise FileNotFoundError(f"no Markdown documents in {corpus_dir}")

    digest = hashlib.sha256()
    chunks: list[Chunk] = []
    for path in paths:
        digest.update(path.name.encode("utf-8"))
        digest.update(path.read_bytes())
        chunks.extend(chunk_document(path))

    return IngestedCorpus(
        chunks=chunks,
        corpus_hash=digest.hexdigest(),
        document_count=len(paths),
        source_dir=str(corpus_dir),
    )
