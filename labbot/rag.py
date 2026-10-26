"""RAG sobre reglamentos, manuales y procedimientos.

- Ingesta: cada documento Markdown tiene metadatos (id, versión, confianza) y se divide
  en un fragmento por artículo/sección. El identificador del fragmento es la cita:
  REG-LAB-001#art3, PROC-LAB-002#sec2, etc.
- Base de datos vectorial: ChromaDB persistente (var/chroma), distancia coseno.
- Embeddings: 'hashing' (local, sin descargas: n-gramas de palabras y caracteres) u
  'ollama' (p. ej. nomic-embed-text). Se calculan fuera de Chroma y se le pasan ya hechos.
- Recuperación: top-k por similitud. Los fragmentos con instrucciones dirigidas al
  asistente se ponen en cuarentena (guard.scan) y no llegan al LLM.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import asdict, dataclass, field
from functools import lru_cache
from pathlib import Path

import numpy as np

from . import config, guard

CITATION_RE = re.compile(r"\[([A-Z]{2,5}-[A-Z]{2,5}-\d{3}#[a-z]+\d+)\]")
HEADING_RE = re.compile(r"^##\s+(Artículo|Sección)\s+(\d+)\.\s*(.+)$", re.IGNORECASE)

STOPWORDS = set("""a al algo como con de del el ella ellos en entre era es esa ese eso esta este esto la las le les lo los
mas me mi mis muy no nos o para pero por que se sea ser si sin sobre su sus te tiene tu un una uno unos y ya debo
puedo quiero cual cuales cuando donde hay""".split())


@dataclass
class Chunk:
    chunk_id: str
    doc_id: str
    doc_title: str
    version: str
    confianza: str
    heading: str
    text: str
    score: float = 0.0
    cuarentena: bool = False
    patrones: list[str] = field(default_factory=list)

    def public(self) -> dict:
        data = asdict(self)
        if self.cuarentena:
            data["text"] = "[Contenido retenido: el fragmento contiene instrucciones dirigidas al asistente y no se usa.]"
        return data


# --- parsing ---------------------------------------------------------------
def parse_document(path: Path) -> list[Chunk]:
    raw = path.read_text(encoding="utf-8")
    meta: dict[str, str] = {}
    body = raw
    if raw.startswith("---"):
        _, header, body = raw.split("---", 2)
        for line in header.strip().splitlines():
            if ":" in line:
                k, v = line.split(":", 1)
                meta[k.strip()] = v.strip()
    chunks: list[Chunk] = []
    current: tuple[str, str] | None = None
    lines: list[str] = []

    def flush():
        if current and lines:
            cid, heading = current
            chunks.append(Chunk(
                chunk_id=f"{meta['id']}#{cid}", doc_id=meta["id"], doc_title=meta.get("titulo", meta["id"]),
                version=meta.get("version", ""), confianza=meta.get("confianza", "oficial"),
                heading=heading, text=" ".join(l.strip() for l in lines if l.strip()),
            ))

    for line in body.splitlines():
        m = HEADING_RE.match(line.strip())
        if m:
            flush()
            prefix = "art" if m.group(1).lower().startswith("art") else "sec"
            current = (f"{prefix}{m.group(2)}", f"{m.group(1)} {m.group(2)}. {m.group(3).strip()}")
            lines = []
        elif current:
            lines.append(line)
    flush()
    return chunks


def load_corpus(docs_dir: Path | None = None) -> list[Chunk]:
    chunks: list[Chunk] = []
    for path in sorted((docs_dir or config.docs_dir()).glob("*.md")):
        chunks.extend(parse_document(path))
    return chunks


# --- embeddings ------------------------------------------------------------
def _normalize(text: str) -> str:
    text = unicodedata.normalize("NFKD", text.lower())
    text = "".join(c for c in text if not unicodedata.combining(c))
    return " ".join(w for w in re.findall(r"[a-z0-9]+", text) if w not in STOPWORDS)


class HashingEmbedder:
    name = "hashing-ngrams-4096"

    def __init__(self):
        from sklearn.feature_extraction.text import HashingVectorizer
        self.words = HashingVectorizer(analyzer="word", ngram_range=(1, 2), n_features=2048, alternate_sign=False, norm="l2")
        self.chars = HashingVectorizer(analyzer="char_wb", ngram_range=(4, 5), n_features=2048, alternate_sign=False, norm="l2")

    def embed(self, texts: list[str]) -> list[list[float]]:
        norm = [_normalize(t) for t in texts]
        mat = np.hstack([self.words.transform(norm).toarray(), 0.6 * self.chars.transform(norm).toarray()])
        mat /= np.linalg.norm(mat, axis=1, keepdims=True) + 1e-12
        return mat.astype(np.float32).tolist()


class OllamaEmbedder:
    def __init__(self, model: str):
        self.model = model
        self.name = f"ollama-{model}"

    def embed(self, texts: list[str]) -> list[list[float]]:
        import httpx
        resp = httpx.post(f"{config.ollama_url()}/api/embed", json={"model": self.model, "input": texts}, timeout=120)
        resp.raise_for_status()
        return resp.json()["embeddings"]


@lru_cache(maxsize=1)
def get_embedder():
    if config.embed_provider() == "ollama":
        return OllamaEmbedder(config.embed_model())
    return HashingEmbedder()


# --- almacén vectorial ---------------------------------------------------------
def _collection():
    import chromadb
    from chromadb.config import Settings

    client = chromadb.PersistentClient(path=str(config.chroma_dir()), settings=Settings(anonymized_telemetry=False))
    return client.get_or_create_collection(
        name="documentos_laboratorio", embedding_function=None, metadata={"hnsw:space": "cosine"}
    )


def ingest(docs_dir: Path | None = None) -> dict:
    chunks = load_corpus(docs_dir)
    col = _collection()
    existing = col.get()["ids"]
    if existing:
        col.delete(ids=existing)
    embedder = get_embedder()
    col.add(
        ids=[c.chunk_id for c in chunks],
        embeddings=embedder.embed([f"{c.doc_title}. {c.heading}. {c.text}" for c in chunks]),
        documents=[c.text for c in chunks],
        metadatas=[{"doc_id": c.doc_id, "doc_title": c.doc_title, "version": c.version, "confianza": c.confianza,
                    "heading": c.heading, "embedder": embedder.name} for c in chunks],
    )
    _cached_collection.cache_clear()
    return {"fragmentos": len(chunks), "documentos": len({c.doc_id for c in chunks}), "embedder": embedder.name}


@lru_cache(maxsize=1)
def _cached_collection():
    return _collection()


def retrieve(query: str, k: int = 4) -> list[Chunk]:
    col = _cached_collection()
    if col.count() == 0:
        ingest()
        col = _cached_collection()
    res = col.query(query_embeddings=get_embedder().embed([query]), n_results=min(k, col.count()))
    chunks = []
    for cid, doc, meta, dist in zip(res["ids"][0], res["documents"][0], res["metadatas"][0], res["distances"][0]):
        patterns = guard.scan(doc)
        chunks.append(Chunk(
            chunk_id=cid, doc_id=meta["doc_id"], doc_title=meta["doc_title"], version=meta["version"],
            confianza=meta["confianza"], heading=meta["heading"], text=doc, score=round(1 - float(dist), 4),
            cuarentena=bool(patterns), patrones=patterns,
        ))
    return chunks


def extract_citations(text: str) -> list[str]:
    seen = []
    for cid in CITATION_RE.findall(text or ""):
        if cid not in seen:
            seen.append(cid)
    return seen
