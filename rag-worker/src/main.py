import asyncio
import logging
from temporalio.client import Client
from temporalio.worker import Worker

from src.config import settings
from src.milvus_store import missing_collection_fields
from src.workflows.index_document import IndexDocumentWorkflow
from src.activities import register_index_execution, index_document, mark_document_failed

logging.basicConfig(level=logging.INFO)

async def main() -> None:
    client = await Client.connect(settings.temporal_host,namespace=settings.temporal_namespace)

    # A collection created before knowledge_base_id existed keeps its old
    # schema forever (init_milvus_collection only builds one when the
    # collection is absent), so chunks written under it are invisible to
    # search. Say so at boot instead of indexing into a store nobody can
    # query. Not fatal: Milvus may still be starting, and a worker that
    # refuses to run indexes nothing at all.
    try:
        missing = await asyncio.to_thread(missing_collection_fields)
        if missing:
            logging.error(
                "Milvus collection is missing %s — drop it and re-index every "
                "document, or search will keep skipping existing chunks.",
                ", ".join(missing),
            )
    except Exception:
        logging.warning("Could not verify the Milvus collection schema", exc_info=True)


    worker = Worker(
        client,
        task_queue=settings.task_queue,
        workflows=[IndexDocumentWorkflow],
        activities=[register_index_execution, index_document, mark_document_failed],
    )
    
    logging.info(f"Starting rag-worker on task queue '{settings.task_queue}'")
    await worker.run()

if __name__ == "__main__":
    asyncio.run(main())
