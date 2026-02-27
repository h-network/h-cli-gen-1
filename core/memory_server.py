"""h-cli Memory Server — read-only semantic search over curated Q&A pairs.

Exposes a single `memory_search` tool via FastMCP SSE on port 8084.
Connects to Qdrant (API-key protected) and uses fastembed for embeddings.
"""

import os

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

COLLECTION = os.environ.get("QDRANT_COLLECTION", "hcli_memory")
EMBEDDING_MODEL = "sentence-transformers/all-MiniLM-L6-v2"
EMBEDDING_DIM = 384

mcp = FastMCP("h-cli-memory", host="0.0.0.0", port=8084)

# Lazy-init globals (set in _init())
_qdrant: QdrantClient | None = None
_embedder: TextEmbedding | None = None


def _init():
    """Initialize Qdrant client, embedding model, and ensure collection exists."""
    global _qdrant, _embedder

    if not QDRANT_API_KEY:
        raise RuntimeError("QDRANT_API_KEY not set")

    try:
        _qdrant = QdrantClient(host=QDRANT_HOST, port=QDRANT_PORT, api_key=QDRANT_API_KEY, https=False, check_compatibility=False)
        logger.info("Connected to Qdrant at %s:%d", QDRANT_HOST, QDRANT_PORT)

        # Ensure collection exists
        collections = [c.name for c in _qdrant.get_collections().collections]
        if COLLECTION not in collections:
            _qdrant.create_collection(
                collection_name=COLLECTION,
                vectors_config=VectorParams(size=EMBEDDING_DIM, distance=Distance.COSINE),
            )
            logger.info("Created collection '%s'", COLLECTION)
        else:
            logger.info("Collection '%s' already exists", COLLECTION)

        _embedder = TextEmbedding(model_name=EMBEDDING_MODEL)
        logger.info("Embedding model '%s' loaded", EMBEDDING_MODEL)
    except Exception:
        logger.exception("_init() failed, cleaning up")
        if _qdrant is not None:
            _qdrant.close()
        _qdrant = None
        _embedder = None
        raise


@mcp.tool()
def memory_search(query: str, limit: int = 5) -> str:
    """Search curated Q&A memory for relevant past knowledge.

    Use this to find answers from previous conversations that were
    curated into long-term memory. Returns the most relevant entries.

    Args:
        query: Natural language search query.
        limit: Maximum number of results (default 5, max 20).
    """
    if _qdrant is None or _embedder is None:
        return "Error: memory server not initialized"

    limit = max(1, min(limit, 20))

    try:
        embedding = list(_embedder.embed([query]))[0].tolist()

        response = _qdrant.query_points(
            collection_name=COLLECTION,
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
