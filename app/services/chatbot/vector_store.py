"""
Vector Store Service

Manages ChromaDB vector storage for document embeddings and similarity search.
"""

import os
from typing import List, Dict, Optional
from pathlib import Path
import logging

from langchain.vectorstores import Chroma
from langchain.schema import Document

logger = logging.getLogger(__name__)


class VectorStoreService:
    """Service for managing vector storage with ChromaDB."""

    def __init__(self, vector_store_path: str, embeddings):
        """
        Initialize the vector store service.

        Args:
            vector_store_path: Base path for vector store databases
            embeddings: LangChain embeddings object (from create_embeddings())
        """
        self.vector_store_path = vector_store_path
        self.embeddings = embeddings

        # Create base directory if it doesn't exist
        Path(vector_store_path).mkdir(parents=True, exist_ok=True)

    def get_or_create_vector_store(self, chatbot_id: int) -> Chroma:
        """
        Get or create a vector store for a specific chatbot.

        Args:
            chatbot_id: ID of the chatbot

        Returns:
            Chroma vector store instance
        """
        persist_directory = os.path.join(self.vector_store_path, str(chatbot_id))

        # Create directory if it doesn't exist
        Path(persist_directory).mkdir(parents=True, exist_ok=True)

        try:
            # Try to load existing vector store
            vectorstore = Chroma(
                persist_directory=persist_directory,
                embedding_function=self.embeddings,
                collection_name=f"chatbot_{chatbot_id}"
            )
            logger.info(f"Loaded vector store for chatbot {chatbot_id}")
        except Exception as e:
            logger.warning(f"Could not load vector store for chatbot {chatbot_id}: {e}")
            # Create new vector store
            vectorstore = Chroma(
                persist_directory=persist_directory,
                embedding_function=self.embeddings,
                collection_name=f"chatbot_{chatbot_id}"
            )
            logger.info(f"Created new vector store for chatbot {chatbot_id}")

        return vectorstore

    def add_documents(
        self,
        chatbot_id: int,
        documents: List[Document],
        document_id: Optional[int] = None
    ) -> int:
        """
        Add documents to the vector store.

        Args:
            chatbot_id: ID of the chatbot
            documents: List of LangChain Document objects
            document_id: Optional document ID to associate with chunks

        Returns:
            Number of documents added
        """
        vectorstore = self.get_or_create_vector_store(chatbot_id)

        # Add document ID to metadata if provided
        if document_id:
            for doc in documents:
                doc.metadata["document_id"] = document_id

        # Add documents
        vectorstore.add_documents(documents)
        vectorstore.persist()

        logger.info(f"Added {len(documents)} document chunks for chatbot {chatbot_id}")
        return len(documents)

    def search_similar(
        self,
        chatbot_id: int,
        query: str,
        k: int = 5,
        filter: Optional[Dict] = None
    ) -> List[Document]:
        """
        Search for similar documents using semantic search.

        Args:
            chatbot_id: ID of the chatbot
            query: Search query
            k: Number of results to return
            filter: Optional metadata filter

        Returns:
            List of similar documents with scores
        """
        vectorstore = self.get_or_create_vector_store(chatbot_id)

        # Perform similarity search
        if filter:
            results = vectorstore.similarity_search(query, k=k, filter=filter)
        else:
            results = vectorstore.similarity_search(query, k=k)

        logger.info(f"Found {len(results)} similar documents for query: {query[:50]}...")
        return results

    def search_with_scores(
        self,
        chatbot_id: int,
        query: str,
        k: int = 5,
        filter: Optional[Dict] = None
    ) -> List[tuple]:
        """
        Search for similar documents with relevance scores.

        Args:
            chatbot_id: ID of the chatbot
            query: Search query
            k: Number of results to return
            filter: Optional metadata filter

        Returns:
            List of tuples (document, score)
        """
        vectorstore = self.get_or_create_vector_store(chatbot_id)

        # Perform similarity search with scores
        if filter:
            results = vectorstore.similarity_search_with_score(query, k=k, filter=filter)
        else:
            results = vectorstore.similarity_search_with_score(query, k=k)

        logger.info(f"Found {len(results)} similar documents with scores for query: {query[:50]}...")
        return results

    def delete_by_document_id(self, chatbot_id: int, document_id: int) -> bool:
        """
        Delete all chunks associated with a document.

        Args:
            chatbot_id: ID of the chatbot
            document_id: ID of the document to delete

        Returns:
            True if successful
        """
        vectorstore = self.get_or_create_vector_store(chatbot_id)

        try:
            # Get collection
            collection = vectorstore._collection

            # Delete by metadata filter
            collection.delete(where={"document_id": document_id})
            vectorstore.persist()

            logger.info(f"Deleted chunks for document {document_id} from chatbot {chatbot_id}")
            return True
        except Exception as e:
            logger.error(f"Error deleting document {document_id}: {e}")
            return False

    @staticmethod
    def delete_vector_store_static(vector_store_path: str, chatbot_id: int) -> bool:
        """Delete entire vector store for a chatbot (no embeddings needed)."""
        persist_directory = os.path.join(vector_store_path, str(chatbot_id))
        try:
            import shutil
            if os.path.exists(persist_directory):
                shutil.rmtree(persist_directory)
                logger.info(f"Deleted vector store for chatbot {chatbot_id}")
                return True
            return False
        except Exception as e:
            logger.error(f"Error deleting vector store for chatbot {chatbot_id}: {e}")
            return False

    def delete_vector_store(self, chatbot_id: int) -> bool:
        """Delete entire vector store for a chatbot."""
        return self.delete_vector_store_static(self.vector_store_path, chatbot_id)

    @staticmethod
    def get_vector_store_stats_static(vector_store_path: str, chatbot_id: int) -> Dict:
        """Get vector store stats without needing embeddings."""
        persist_directory = os.path.join(vector_store_path, str(chatbot_id))
        try:
            if not os.path.exists(persist_directory):
                return {"total_chunks": 0, "chatbot_id": chatbot_id}
            import chromadb
            client = chromadb.PersistentClient(path=persist_directory)
            collection = client.get_collection(f"chatbot_{chatbot_id}")
            return {"total_chunks": collection.count(), "chatbot_id": chatbot_id}
        except Exception as e:
            logger.error(f"Error getting stats for chatbot {chatbot_id}: {e}")
            return {"total_chunks": 0, "chatbot_id": chatbot_id, "error": str(e)}

    def get_vector_store_stats(self, chatbot_id: int) -> Dict:
        """Get statistics about a vector store."""
        try:
            vectorstore = self.get_or_create_vector_store(chatbot_id)
            collection = vectorstore._collection

            stats = {
                "total_chunks": collection.count(),
                "chatbot_id": chatbot_id,
            }

            return stats
        except Exception as e:
            logger.error(f"Error getting stats for chatbot {chatbot_id}: {e}")
            return {"total_chunks": 0, "chatbot_id": chatbot_id, "error": str(e)}
