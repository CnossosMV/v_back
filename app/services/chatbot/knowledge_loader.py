"""
Knowledge Loader Service

Handles loading and processing documents from various sources:
- Markdown files (.md)
- PDF files (.pdf)
- Text files (.txt)
- URLs (web scraping)
- Images (for metadata extraction)
"""

import os
import hashlib
from typing import List, Dict, Optional, Tuple
from pathlib import Path
import logging

from langchain.document_loaders import (
    TextLoader,
    PyPDFLoader,
    UnstructuredMarkdownLoader,
)
from langchain.text_splitter import RecursiveCharacterTextSplitter, MarkdownHeaderTextSplitter
from langchain.schema import Document
from bs4 import BeautifulSoup
import requests

logger = logging.getLogger(__name__)


class KnowledgeLoader:
    """Service for loading and processing knowledge base documents."""

    def __init__(self, chunk_size: int = 1000, chunk_overlap: int = 200):
        """
        Initialize the knowledge loader.

        Args:
            chunk_size: Size of text chunks for embeddings
            chunk_overlap: Overlap between consecutive chunks
        """
        self.chunk_size = chunk_size
        self.chunk_overlap = chunk_overlap
        self.text_splitter = RecursiveCharacterTextSplitter(
            chunk_size=chunk_size,
            chunk_overlap=chunk_overlap,
            length_function=len,
        )

    def load_file(self, file_path: str) -> Tuple[str, List[Document], Dict]:
        """
        Load a document from a file path.

        Args:
            file_path: Path to the file

        Returns:
            Tuple of (content_hash, documents, metadata)
        """
        file_path_obj = Path(file_path)

        if not file_path_obj.exists():
            raise FileNotFoundError(f"File not found: {file_path}")

        file_type = file_path_obj.suffix.lower()

        try:
            if file_type == ".md":
                return self._load_markdown(file_path)
            elif file_type == ".pdf":
                return self._load_pdf(file_path)
            elif file_type in [".txt", ".text"]:
                return self._load_text(file_path)
            else:
                raise ValueError(f"Unsupported file type: {file_type}")
        except Exception as e:
            logger.error(f"Error loading file {file_path}: {str(e)}")
            raise

    def _load_markdown(self, file_path: str) -> Tuple[str, List[Document], Dict]:
        """Load and process a markdown file."""
        # Read the raw markdown content
        with open(file_path, 'r', encoding='utf-8') as f:
            markdown_content = f.read()

        # Compute hash of original content
        content_hash = self._compute_hash(markdown_content)

        # Split by markdown headers first (this creates chunks per section)
        headers_to_split_on = [
            ("#", "Header 1"),
            ("##", "Header 2"),
            ("###", "Header 3"),
        ]

        markdown_splitter = MarkdownHeaderTextSplitter(
            headers_to_split_on=headers_to_split_on,
            strip_headers=False  # Keep headers in the content for context
        )

        header_chunks = markdown_splitter.split_text(markdown_content)

        # If header splitting resulted in chunks, use them
        # Otherwise fall back to loading the whole document
        if header_chunks and len(header_chunks) > 1:
            chunks = header_chunks
            logger.info(f"Split markdown into {len(chunks)} chunks by headers")
        else:
            # Fallback: use UnstructuredMarkdownLoader for regular splitting
            loader = UnstructuredMarkdownLoader(file_path)
            documents = loader.load()
            chunks = self.text_splitter.split_documents(documents)
            logger.info(f"Split markdown into {len(chunks)} chunks by character count")

        # Add source metadata to all chunks
        for chunk in chunks:
            chunk.metadata["source"] = file_path

        # Extract metadata (images, headings, etc.)
        metadata = self._extract_markdown_metadata(file_path, markdown_content)

        return content_hash, chunks, metadata

    def _load_pdf(self, file_path: str) -> Tuple[str, List[Document], Dict]:
        """Load and process a PDF file."""
        loader = PyPDFLoader(file_path)
        documents = loader.load()

        # Extract content and compute hash
        content = "\n\n".join([doc.page_content for doc in documents])
        content_hash = self._compute_hash(content)

        # Split into chunks
        chunks = self.text_splitter.split_documents(documents)

        # Extract metadata
        metadata = {
            "page_count": len(documents),
            "file_type": "pdf",
        }

        return content_hash, chunks, metadata

    def _load_text(self, file_path: str) -> Tuple[str, List[Document], Dict]:
        """Load and process a text file."""
        loader = TextLoader(file_path)
        documents = loader.load()

        # Extract content and compute hash
        content = "\n\n".join([doc.page_content for doc in documents])
        content_hash = self._compute_hash(content)

        # Split into chunks
        chunks = self.text_splitter.split_documents(documents)

        metadata = {"file_type": "text"}

        return content_hash, chunks, metadata

    def load_url(self, url: str, scrape_depth: int = 1) -> Tuple[str, List[Document], Dict]:
        """
        Load content from a URL.

        Args:
            url: URL to scrape
            scrape_depth: How deep to follow links (1 = just the page)

        Returns:
            Tuple of (content_hash, documents, metadata)
        """
        try:
            response = requests.get(url, timeout=30)
            response.raise_for_status()

            soup = BeautifulSoup(response.text, 'html.parser')

            # Remove script and style elements
            for script in soup(["script", "style"]):
                script.decompose()

            # Get text content
            text = soup.get_text()

            # Clean up whitespace
            lines = (line.strip() for line in text.splitlines())
            chunks_text = (phrase.strip() for line in lines for phrase in line.split("  "))
            text = '\n'.join(chunk for chunk in chunks_text if chunk)

            # Compute hash
            content_hash = self._compute_hash(text)

            # Create document
            doc = Document(
                page_content=text,
                metadata={"source": url, "type": "url"}
            )

            # Split into chunks
            chunks = self.text_splitter.split_documents([doc])

            # Extract metadata
            metadata = {
                "title": soup.title.string if soup.title else "",
                "url": url,
                "links": [a.get('href') for a in soup.find_all('a', href=True)][:10],  # First 10 links
                "images": [img.get('src') for img in soup.find_all('img', src=True)][:5],  # First 5 images
            }

            return content_hash, chunks, metadata

        except Exception as e:
            logger.error(f"Error loading URL {url}: {str(e)}")
            raise

    def _extract_markdown_metadata(self, file_path: str, content: str) -> Dict:
        """Extract metadata from markdown content."""
        metadata = {
            "file_type": "markdown",
            "headings": [],
            "images": [],
        }

        # Read file to extract images
        with open(file_path, 'r', encoding='utf-8') as f:
            lines = f.readlines()

        for line in lines:
            # Extract headings
            if line.startswith('#'):
                heading_level = len(line) - len(line.lstrip('#'))
                heading_text = line.lstrip('#').strip()
                metadata["headings"].append({
                    "level": heading_level,
                    "text": heading_text
                })

            # Extract image references
            if '![' in line and '](' in line:
                start = line.find('](') + 2
                end = line.find(')', start)
                if start > 1 and end > start:
                    img_path = line[start:end]
                    metadata["images"].append(img_path)

        return metadata

    def _compute_hash(self, content: str) -> str:
        """Compute SHA256 hash of content for deduplication."""
        return hashlib.sha256(content.encode('utf-8')).hexdigest()

    def save_uploaded_file(
        self,
        file_content: bytes,
        filename: str,
        chatbot_id: int,
        knowledge_base_path: str
    ) -> str:
        """
        Save an uploaded file to the knowledge base directory.

        Args:
            file_content: File content as bytes
            filename: Original filename
            chatbot_id: ID of the chatbot
            knowledge_base_path: Base path for knowledge bases

        Returns:
            Path to the saved file
        """
        # Create chatbot-specific directory
        chatbot_dir = Path(knowledge_base_path) / str(chatbot_id)
        chatbot_dir.mkdir(parents=True, exist_ok=True)

        # Save file
        file_path = chatbot_dir / filename
        with open(file_path, 'wb') as f:
            f.write(file_content)

        logger.info(f"Saved file {filename} for chatbot {chatbot_id}")
        return str(file_path)

    def find_related_images(
        self,
        query: str,
        knowledge_base_path: str,
        chatbot_id: int,
        max_images: int = 3
    ) -> List[str]:
        """
        Find images related to a query in the knowledge base.

        Args:
            query: Search query
            knowledge_base_path: Base path for knowledge bases
            chatbot_id: ID of the chatbot
            max_images: Maximum number of images to return

        Returns:
            List of image paths
        """
        # knowledge_base_path already includes the chatbot_id
        chatbot_dir = Path(knowledge_base_path)

        print(f"\n=== IMAGE MATCHING DEBUG ===")
        print(f"Finding images for query: '{query}' in chatbot {chatbot_id}")
        print(f"Knowledge base path: {knowledge_base_path}")
        print(f"Chatbot directory: {chatbot_dir}")

        if not chatbot_dir.exists():
            logger.warning(f"Chatbot directory does not exist: {chatbot_dir}")
            return []

        # Find all images
        image_extensions = ['.jpg', '.jpeg', '.png', '.gif', '.webp']
        images = []

        for ext in image_extensions:
            images.extend(chatbot_dir.glob(f"**/*{ext}"))

        print(f"Found {len(images)} total images: {[img.name for img in images]}")

        # Smart matching based on filename
        query_lower = query.lower()

        # Remove common stop words (Portuguese and English)
        stop_words = {'como', 'o', 'a', 'de', 'para', 'em', 'um', 'uma', 'mais', 'the', 'a', 'an', 'is', 'are', 'how', 'to'}
        query_words = [word for word in query_lower.split() if word not in stop_words and len(word) > 2]

        print(f"Query after stop words removal: {query_words}")

        matched_images = []
        scored_images = []

        for img in images:
            img_name = img.stem.lower().replace('_', ' ').replace('-', ' ')
            score = 0

            # Check each query word
            for word in query_words:
                # Only check meaningful words (5+ chars)
                if len(word) < 5:
                    continue

                # Check for exact word match in filename
                if word in img_name:
                    score += 3  # Exact match
                else:
                    # Check for prefix matches (must start at word boundary)
                    for img_word in img_name.split():
                        # Both words must be at least 5 chars and share a 5-char prefix
                        if len(img_word) >= 5 and word[:5] == img_word[:5]:
                            score += 2  # Strong prefix match
                        # Partial: 4-char prefix match for longer words (8+ chars)
                        elif len(word) >= 8 and len(img_word) >= 8 and word[:4] == img_word[:4]:
                            score += 1  # Weak prefix match

            if score > 0:
                relative_path = img.relative_to(chatbot_dir)
                scored_images.append((str(relative_path), score))

        # Sort by score and get top matches
        scored_images.sort(key=lambda x: x[1], reverse=True)
        matched_images = [img for img, score in scored_images[:max_images]]

        print(f"Scored images: {scored_images}")
        print(f"Matched images after scoring: {matched_images}")

        # Only return images if they have a good match score (at least 1)
        # Don't return all images as fallback - if no match, return empty list
        if matched_images:
            print(f"✅ Returning {len(matched_images)} matched images: {matched_images}")
        else:
            print(f"❌ No relevant images found for this query")

        print(f"=== END IMAGE MATCHING ===\n")
        return matched_images
