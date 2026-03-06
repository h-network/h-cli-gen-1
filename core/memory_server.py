"""h-cli Memory Server — semantic search over multiple Qdrant collections.

Exposes a `memory_search` tool via FastMCP SSE on port 8084.
Connects to Qdrant (API-key protected) and uses fastembed for embeddings.
Supports multiple collections loaded from JSONL files on disk.
"""

import hashlib
import json
import os
import pathlib

from fastembed import TextEmbedding
from mcp.server.fastmcp import FastMCP
from qdrant_client import QdrantClient
from qdrant_client.models import Distance, PointStruct, VectorParams

from hcli_logging import get_logger

logger = get_logger(__name__, service="memory")

QDRANT_HOST = os.environ.get("QDRANT_HOST", "h-cli-qdrant")
try:
    QDRANT_PORT = int(os.environ.get("QDRANT_PORT", "6333"))
except (ValueError, TypeError):
    logger.warning("Invalid QDRANT_PORT, falling back to 6333")
    QDRANT_PORT = 6333
QDRANT_API_KEY = os.environ.get("QDRANT_API_KEY", "")

# Legacy single-collection config (backward compatible)
LEGACY_COLLECTION = os.environ.get("QDRANT_COLLECTION", "hcli_memory")
# Multi-collection config
QDRANT_COLLECTIONS_RAW = os.environ.get("QDRANT_COLLECTIONS", "")
# Directory where collection JSONL files live
COLLECTIONS_DIR = pathlib.Path(os.environ.get("COLLECTIONS_DIR", "/app/data/collections"))

EMBEDDING_MODEL = "sentence-transformers/all-MiniLM-L6-v2"
EMBEDDING_DIM = 384
LOAD_MARKER = ".loaded"

mcp = FastMCP("h-cli-memory", host="0.0.0.0", port=8084)

# Lazy-init globals (set in _init())
_qdrant: QdrantClient | None = None
_embedder: TextEmbedding | None = None
_available_collections: set[str] = set()
_default_collection: str = ""


def _get_collection_names() -> list[str]:
    """Build merged list of collection names from legacy + multi-collection config."""
    names = []
    if LEGACY_COLLECTION:
        names.append(LEGACY_COLLECTION)
    if QDRANT_COLLECTIONS_RAW:
        for name in QDRANT_COLLECTIONS_RAW.split(","):
            name = name.strip()
            if name and name not in names:
                names.append(name)
    return names


def _content_hash(question: str, answer: str, source: str) -> str:
    """Deterministic UUID from content for idempotent upserts."""
    content = f"{question}|{answer}|{source}"
    h = hashlib.sha256(content.encode()).hexdigest()[:32]
    return f"{h[:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:32]}"


def _load_collection(name: str) -> None:
    """Load JSONL files from data/collections/{name}/ into Qdrant."""
    if _qdrant is None:
        return

    collection_dir = COLLECTIONS_DIR / name
    if not collection_dir.is_dir():
        logger.info("No data directory for collection '%s' at %s, skipping load", name, collection_dir)
        return

    jsonl_files = sorted(collection_dir.glob("*.jsonl"))
    if not jsonl_files:
        logger.info("No JSONL files in %s, skipping load", collection_dir)
        return

    # Check if any JSONL file is newer than the load marker
    marker_path = collection_dir / LOAD_MARKER
    if marker_path.exists():
        marker_mtime = marker_path.stat().st_mtime
        newest_file = max(f.stat().st_mtime for f in jsonl_files)
        if newest_file <= marker_mtime:
            logger.info("Collection '%s' is up to date (no new files), skipping load", name)
            return

    # Read all points from JSONL files
    points = []
    vector_dim = None
    needs_embedding = []

    for jsonl_file in jsonl_files:
        logger.info("Reading %s for collection '%s'", jsonl_file.name, name)
        with open(jsonl_file) as f:
            for line_num, line in enumerate(f, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError as e:
                    logger.warning("Skipping invalid JSON at %s:%d: %s", jsonl_file.name, line_num, e)
                    continue

                question = record.get("question", "")
                answer = record.get("answer", "")
                source = record.get("source", "")
                vector = record.get("vector")

                point_id = _content_hash(question, answer, source)
                payload = {"question": question, "answer": answer, "source": source}

                if vector is not None:
                    if vector_dim is None:
                        vector_dim = len(vector)
                    points.append(PointStruct(id=point_id, vector=vector, payload=payload))
                else:
                    # Queue for embedding
                    needs_embedding.append((point_id, question, payload))

    # Embed any records without pre-computed vectors
    if needs_embedding and _embedder is not None:
        logger.info("Embedding %d records for collection '%s' using %s", len(needs_embedding), name, EMBEDDING_MODEL)
        texts = [q for _, q, _ in needs_embedding]
        vectors = list(_embedder.embed(texts))
        for (point_id, _, payload), vec in zip(needs_embedding, vectors):
            vec_list = vec.tolist()
            if vector_dim is None:
                vector_dim = len(vec_list)
            points.append(PointStruct(id=point_id, vector=vec_list, payload=payload))

    if not points:
        logger.info("No valid points found for collection '%s'", name)
        return

    if vector_dim is None:
        logger.warning("Could not determine vector dimensions for collection '%s'", name)
        return

    # Ensure collection exists with correct dimensions
    existing = [c.name for c in _qdrant.get_collections().collections]
    if name not in existing:
        _qdrant.create_collection(
            collection_name=name,
            vectors_config=VectorParams(size=vector_dim, distance=Distance.COSINE),
        )
        logger.info("Created collection '%s' (dim=%d)", name, vector_dim)
    else:
        logger.info("Collection '%s' already exists, upserting points", name)

    # Upsert in batches of 100
    batch_size = 100
    for i in range(0, len(points), batch_size):
        batch = points[i:i + batch_size]
        _qdrant.upsert(collection_name=name, points=batch)

    logger.info("Loaded %d points into collection '%s'", len(points), name)

    # Write load marker
    marker_path.write_text(f"{len(points)} points loaded from {len(jsonl_files)} files\n")


def _init():
    """Initialize Qdrant client, embedding model, load collections."""
    global _qdrant, _embedder, _available_collections, _default_collection

    if not QDRANT_API_KEY:
        raise RuntimeError("QDRANT_API_KEY not set")

    try:
        _qdrant = QdrantClient(host=QDRANT_HOST, port=QDRANT_PORT, api_key=QDRANT_API_KEY, https=False, check_compatibility=False)
        logger.info("Connected to Qdrant at %s:%d", QDRANT_HOST, QDRANT_PORT)

        _embedder = TextEmbedding(model_name=EMBEDDING_MODEL)
        logger.info("Embedding model '%s' loaded", EMBEDDING_MODEL)

        # Build collection list
        collection_names = _get_collection_names()
        logger.info("Configured collections: %s", collection_names)

        # Ensure legacy collection exists (backward compatible)
        existing = [c.name for c in _qdrant.get_collections().collections]
        if LEGACY_COLLECTION and LEGACY_COLLECTION not in existing:
            _qdrant.create_collection(
                collection_name=LEGACY_COLLECTION,
                vectors_config=VectorParams(size=EMBEDDING_DIM, distance=Distance.COSINE),
            )
            logger.info("Created legacy collection '%s'", LEGACY_COLLECTION)

        # Load data for each configured collection
        for name in collection_names:
            try:
                _load_collection(name)
            except Exception:
                logger.exception("Failed to load collection '%s', skipping", name)

        # Track available collections
        _available_collections = set(c.name for c in _qdrant.get_collections().collections)

        # Resolve default collection: first from QDRANT_COLLECTIONS, then legacy, then hcli_memory
        if QDRANT_COLLECTIONS_RAW:
            first = QDRANT_COLLECTIONS_RAW.split(",")[0].strip()
            _default_collection = first if first else LEGACY_COLLECTION
        else:
            _default_collection = LEGACY_COLLECTION
        logger.info("Default collection: '%s'", _default_collection)
        logger.info("Available collections: %s", sorted(_available_collections))

    except Exception:
        logger.exception("_init() failed, cleaning up")
        if _qdrant is not None:
            _qdrant.close()
        _qdrant = None
        _embedder = None
        _available_collections = set()
        _default_collection = ""
        raise


@mcp.tool()
def memory_search(query: str, collection: str = "", limit: int = 5) -> str:
    """Search curated Q&A memory for relevant past knowledge.

    Use this to find answers from previous conversations that were
    curated into long-term memory. Returns the most relevant entries.

    Args:
        query: Natural language search query.
        collection: Collection to search. Leave empty for default collection.
        limit: Maximum number of results (default 5, max 20).
    """
    if _qdrant is None or _embedder is None:
        return "Error: memory server not initialized"

    target = collection.strip() if collection else _default_collection

    if target not in _available_collections:
        available = sorted(_available_collections)
        return f"Error: collection '{target}' not found. Available: {', '.join(available)}"

    limit = max(1, min(limit, 20))

    try:
        embedding = list(_embedder.embed([query]))[0].tolist()

        response = _qdrant.query_points(
            collection_name=target,
            query=embedding,
            limit=limit,
        )

        if not response.points:
            return "No relevant memories found."

        entries = []
        for i, hit in enumerate(response.points, 1):
            payload = hit.payload or {}
            score = f"{hit.score:.3f}"
            question = payload.get("question", "")
            answer = payload.get("answer", "")
            source = payload.get("source", "")

            entry = f"### Result {i} (score: {score})"
            if question:
                entry += f"\n**Q:** {question}"
            if answer:
                entry += f"\n**A:** {answer}"
            if source:
                entry += f"\n*Source: {source}*"
            entries.append(entry)

        return "\n\n".join(entries)

    except Exception as e:
        logger.exception("memory_search failed")
        return "Error: memory search unavailable"


if __name__ == "__main__":
    logger.info("Initializing memory server...")
    try:
        _init()
    except Exception:
        logger.warning("Memory init failed — server will start but memory_search will return 'not initialized'")
    logger.info("Starting memory MCP server on 0.0.0.0:8084")
    mcp.run(transport="sse")
