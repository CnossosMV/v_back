"""
Knowledge Base Router

Handles uploading and managing knowledge base documents for chatbots.
"""

import os
from typing import List
from fastapi import APIRouter, Depends, HTTPException, status, UploadFile, File
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import Chatbot, KnowledgeDocument
from app.schemas import (
    KnowledgeDocumentResponse,
    KnowledgeDocumentCreate,
    URLUploadRequest,
    BatchUploadResponse,
)
from app.routers.auth import get_current_user

# Services
from app.services.chatbot.knowledge_loader import KnowledgeLoader
from app.services.chatbot.vector_store import VectorStoreService
from app.services.chatbot.llm_key_resolver import resolve_embeddings, create_embeddings

router = APIRouter(tags=["knowledge_base"])


def get_knowledge_services(db: Session, project_id: int):
    """Initialize knowledge base services using project-level LLM config."""
    try:
        emb_cfg = resolve_embeddings(db, project_id)
    except ValueError as e:
        raise HTTPException(status_code=500, detail=str(e))

    embeddings = create_embeddings(emb_cfg)
    vector_store_path = os.getenv("CHATBOT_VECTOR_STORE_PATH", "./vector_stores")
    knowledge_base_path = os.getenv("CHATBOT_KNOWLEDGE_BASE_PATH", "./knowledge_bases")

    knowledge_loader = KnowledgeLoader()
    vector_store = VectorStoreService(vector_store_path, embeddings)

    return knowledge_loader, vector_store, knowledge_base_path, emb_cfg


@router.post("/chatbots/{chatbot_id}/knowledge/upload", response_model=KnowledgeDocumentResponse)
async def upload_knowledge_file(
    chatbot_id: int,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    current_user = Depends(get_current_user)
):
    """
    Upload a file to the chatbot's knowledge base.

    Supported formats: .md, .txt, .pdf
    """
    # Get chatbot
    chatbot = db.query(Chatbot).filter(Chatbot.id == chatbot_id).first()
    if not chatbot:
        raise HTTPException(status_code=404, detail="Chatbot not found")

    # Validate file type
    allowed_doc_extensions = ['.md', '.txt', '.pdf', '.text']
    allowed_image_extensions = ['.jpg', '.jpeg', '.png', '.gif', '.webp']
    allowed_extensions = allowed_doc_extensions + allowed_image_extensions
    file_ext = os.path.splitext(file.filename)[1].lower()

    if file_ext not in allowed_extensions:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported file type. Allowed: {', '.join(allowed_extensions)}"
        )

    is_image = file_ext in allowed_image_extensions

    # Initialize services
    try:
        knowledge_loader, vector_store, knowledge_base_path, emb_cfg = get_knowledge_services(db, chatbot.project_id)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    try:
        # Read file content
        file_content = await file.read()

        # Save file
        file_path = knowledge_loader.save_uploaded_file(
            file_content=file_content,
            filename=file.filename,
            chatbot_id=chatbot_id,
            knowledge_base_path=knowledge_base_path
        )

        # Handle images differently - no vector processing
        if is_image:
            # Compute hash for deduplication
            import hashlib
            content_hash = hashlib.sha256(file_content).hexdigest()

            # Create database record for image
            doc = KnowledgeDocument(
                chatbot_id=chatbot_id,
                source_type="file",
                file_path=file_path,
                file_name=file.filename,
                file_type=file_ext.lstrip('.'),
                content_hash=content_hash,
                chunk_count=0,
                embedding_model=None,
                processed=True,
                document_metadata={"type": "image", "size": len(file_content)}
            )

            db.add(doc)
            db.commit()
            db.refresh(doc)

            return KnowledgeDocumentResponse.from_orm(doc)

        # Process text documents
        content_hash, chunks, metadata = knowledge_loader.load_file(file_path)

        # Create database record
        doc = KnowledgeDocument(
            chatbot_id=chatbot_id,
            source_type="file",
            file_path=file_path,
            file_name=file.filename,
            file_type=file_ext.lstrip('.'),
            content_hash=content_hash,
            chunk_count=len(chunks),
            embedding_model=emb_cfg.model,
            processed=False,
            document_metadata=metadata
        )

        db.add(doc)
        db.commit()
        db.refresh(doc)

        # Add to vector store
        chunk_count = vector_store.add_documents(
            chatbot_id=chatbot_id,
            documents=chunks,
            document_id=doc.id
        )

        # Update processed status
        doc.processed = True
        doc.chunk_count = chunk_count
        db.commit()
        db.refresh(doc)

        return KnowledgeDocumentResponse.from_orm(doc)

    except Exception as e:
        # Update error status if doc was created
        if 'doc' in locals():
            doc.processed = False
            doc.processing_error = str(e)
            db.commit()

        raise HTTPException(
            status_code=500,
            detail=f"Error processing file: {str(e)}"
        )


@router.post("/chatbots/{chatbot_id}/knowledge/url", response_model=KnowledgeDocumentResponse)
async def add_url_to_knowledge(
    chatbot_id: int,
    url_request: URLUploadRequest,
    db: Session = Depends(get_db),
    current_user = Depends(get_current_user)
):
    """
    Scrape a URL and add it to the chatbot's knowledge base.
    """
    # Get chatbot
    chatbot = db.query(Chatbot).filter(Chatbot.id == chatbot_id).first()
    if not chatbot:
        raise HTTPException(status_code=404, detail="Chatbot not found")

    # Initialize services
    try:
        knowledge_loader, vector_store, knowledge_base_path, emb_cfg = get_knowledge_services(db, chatbot.project_id)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    try:
        # Scrape URL
        content_hash, chunks, metadata = knowledge_loader.load_url(
            url=url_request.url,
            scrape_depth=url_request.scrape_depth
        )

        # Merge user metadata
        metadata.update(url_request.metadata or {})

        # Create database record
        doc = KnowledgeDocument(
            chatbot_id=chatbot_id,
            source_type="url",
            source_url=url_request.url,
            file_name=metadata.get("title", url_request.url),
            file_type="html",
            content_hash=content_hash,
            chunk_count=len(chunks),
            embedding_model=emb_cfg.model,
            processed=False,
            document_metadata=metadata
        )

        db.add(doc)
        db.commit()
        db.refresh(doc)

        # Add to vector store
        chunk_count = vector_store.add_documents(
            chatbot_id=chatbot_id,
            documents=chunks,
            document_id=doc.id
        )

        # Update processed status
        doc.processed = True
        doc.chunk_count = chunk_count
        db.commit()
        db.refresh(doc)

        return KnowledgeDocumentResponse.from_orm(doc)

    except Exception as e:
        # Update error status if doc was created
        if 'doc' in locals():
            doc.processed = False
            doc.processing_error = str(e)
            db.commit()

        raise HTTPException(
            status_code=500,
            detail=f"Error processing URL: {str(e)}"
        )


@router.post("/chatbots/{chatbot_id}/knowledge/batch-upload", response_model=BatchUploadResponse)
async def batch_upload_files(
    chatbot_id: int,
    files: List[UploadFile] = File(...),
    db: Session = Depends(get_db),
    current_user = Depends(get_current_user)
):
    """
    Upload multiple files at once to the chatbot's knowledge base.
    """
    # Get chatbot
    chatbot = db.query(Chatbot).filter(Chatbot.id == chatbot_id).first()
    if not chatbot:
        raise HTTPException(status_code=404, detail="Chatbot not found")

    # Initialize services
    try:
        knowledge_loader, vector_store, knowledge_base_path, emb_cfg = get_knowledge_services(db, chatbot.project_id)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    success_count = 0
    failed_count = 0
    documents = []
    errors = []

    allowed_doc_extensions = ['.md', '.txt', '.pdf', '.text']
    allowed_image_extensions = ['.jpg', '.jpeg', '.png', '.gif', '.webp']
    allowed_extensions = allowed_doc_extensions + allowed_image_extensions

    for file in files:
        file_ext = os.path.splitext(file.filename)[1].lower()

        if file_ext not in allowed_extensions:
            failed_count += 1
            errors.append({
                "filename": file.filename,
                "error": f"Unsupported file type: {file_ext}"
            })
            continue

        try:
            # Read file content
            file_content = await file.read()

            # Save file
            file_path = knowledge_loader.save_uploaded_file(
                file_content=file_content,
                filename=file.filename,
                chatbot_id=chatbot_id,
                knowledge_base_path=knowledge_base_path
            )

            is_image = file_ext in allowed_image_extensions

            # Handle images differently - no vector processing
            if is_image:
                import hashlib
                content_hash = hashlib.sha256(file_content).hexdigest()

                doc = KnowledgeDocument(
                    chatbot_id=chatbot_id,
                    source_type="file",
                    file_path=file_path,
                    file_name=file.filename,
                    file_type=file_ext.lstrip('.'),
                    content_hash=content_hash,
                    chunk_count=0,
                    embedding_model=None,
                    processed=True,
                    document_metadata={"type": "image", "size": len(file_content)}
                )

                db.add(doc)
                db.commit()
                db.refresh(doc)
            else:
                # Process text documents
                content_hash, chunks, metadata = knowledge_loader.load_file(file_path)

                # Create database record
                doc = KnowledgeDocument(
                    chatbot_id=chatbot_id,
                    source_type="file",
                    file_path=file_path,
                    file_name=file.filename,
                    file_type=file_ext.lstrip('.'),
                    content_hash=content_hash,
                    chunk_count=len(chunks),
                    embedding_model=emb_cfg.model,
                    processed=False,
                    document_metadata=metadata
                )

                db.add(doc)
                db.commit()
                db.refresh(doc)

                # Add to vector store
                chunk_count = vector_store.add_documents(
                    chatbot_id=chatbot_id,
                    documents=chunks,
                    document_id=doc.id
                )

                # Update processed status
                doc.processed = True
                doc.chunk_count = chunk_count
                db.commit()
                db.refresh(doc)

            success_count += 1
            documents.append(KnowledgeDocumentResponse.from_orm(doc))

        except Exception as e:
            failed_count += 1
            errors.append({
                "filename": file.filename,
                "error": str(e)
            })

            # Update error status if doc was created
            if 'doc' in locals():
                doc.processed = False
                doc.processing_error = str(e)
                db.commit()

    return BatchUploadResponse(
        success=success_count,
        failed=failed_count,
        total=len(files),
        documents=documents,
        errors=errors if errors else None
    )


@router.delete("/knowledge/{document_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_knowledge_document(
    document_id: int,
    db: Session = Depends(get_db),
    current_user = Depends(get_current_user)
):
    """Delete a knowledge document from the database and vector store."""
    doc = db.query(KnowledgeDocument).filter(KnowledgeDocument.id == document_id).first()

    if not doc:
        raise HTTPException(status_code=404, detail="Document not found")

    # Get chatbot for project_id
    chatbot = db.query(Chatbot).filter(Chatbot.id == doc.chatbot_id).first()
    project_id = chatbot.project_id if chatbot else 0

    # Initialize services
    try:
        knowledge_loader, vector_store, knowledge_base_path, emb_cfg = get_knowledge_services(db, project_id)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    # Delete from vector store
    vector_store.delete_by_document_id(
        chatbot_id=doc.chatbot_id,
        document_id=document_id
    )

    # Delete file if it exists
    if doc.file_path and os.path.exists(doc.file_path):
        try:
            os.remove(doc.file_path)
        except Exception as e:
            # Log but don't fail
            pass

    # Delete from database
    db.delete(doc)
    db.commit()

    return None


@router.post("/knowledge/{document_id}/reprocess", response_model=KnowledgeDocumentResponse)
async def reprocess_document(
    document_id: int,
    db: Session = Depends(get_db),
    current_user = Depends(get_current_user)
):
    """Reprocess a document (useful if processing failed)."""
    doc = db.query(KnowledgeDocument).filter(KnowledgeDocument.id == document_id).first()

    if not doc:
        raise HTTPException(status_code=404, detail="Document not found")

    # Get chatbot for project_id
    chatbot = db.query(Chatbot).filter(Chatbot.id == doc.chatbot_id).first()
    project_id = chatbot.project_id if chatbot else 0

    # Initialize services
    try:
        knowledge_loader, vector_store, knowledge_base_path, emb_cfg = get_knowledge_services(db, project_id)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    try:
        # Delete old vector embeddings
        vector_store.delete_by_document_id(
            chatbot_id=doc.chatbot_id,
            document_id=document_id
        )

        # Reprocess based on source type
        if doc.source_type == "file" and doc.file_path:
            content_hash, chunks, metadata = knowledge_loader.load_file(doc.file_path)
        elif doc.source_type == "url" and doc.source_url:
            content_hash, chunks, metadata = knowledge_loader.load_url(doc.source_url)
        else:
            raise ValueError("Invalid document source")

        # Update metadata
        doc.content_hash = content_hash
        doc.document_metadata = metadata
        doc.processed = False
        doc.processing_error = None

        # Add to vector store
        chunk_count = vector_store.add_documents(
            chatbot_id=doc.chatbot_id,
            documents=chunks,
            document_id=doc.id
        )

        # Update status
        doc.processed = True
        doc.chunk_count = chunk_count
        db.commit()
        db.refresh(doc)

        return KnowledgeDocumentResponse.from_orm(doc)

    except Exception as e:
        doc.processed = False
        doc.processing_error = str(e)
        db.commit()

        raise HTTPException(
            status_code=500,
            detail=f"Error reprocessing document: {str(e)}"
        )


@router.get("/chatbots/{chatbot_id}/images/{filename:path}")
async def serve_image(
    chatbot_id: int,
    filename: str,
    db: Session = Depends(get_db)
):
    """
    Serve an image file from the chatbot's knowledge base.
    Supports images in subdirectories (e.g., testes/image.jpg).
    Note: This endpoint is public to allow <img> tags to load images.
    """
    # Get chatbot to verify it exists
    chatbot = db.query(Chatbot).filter(Chatbot.id == chatbot_id).first()
    if not chatbot:
        raise HTTPException(status_code=404, detail="Chatbot not found")

    # Get knowledge base path
    knowledge_base_path = os.getenv("CHATBOT_KNOWLEDGE_BASE_PATH", "./knowledge_bases")

    # Build file path (support subdirectories)
    # filename can be like "image.jpg" or "testes/image.jpg"
    chatbot_dir = os.path.join(knowledge_base_path, str(chatbot_id))
    file_path = os.path.join(chatbot_dir, filename.replace('\\', '/'))

    # Security check: ensure file is within chatbot directory
    file_path = os.path.abspath(file_path)
    chatbot_dir = os.path.abspath(chatbot_dir)
    if not file_path.startswith(chatbot_dir):
        raise HTTPException(status_code=403, detail="Access denied")

    # Check if file exists and is an image
    if not os.path.exists(file_path):
        raise HTTPException(status_code=404, detail="Image not found")

    # Determine media type
    ext = os.path.splitext(file_path)[1].lower()
    media_types = {
        '.jpg': 'image/jpeg',
        '.jpeg': 'image/jpeg',
        '.png': 'image/png',
        '.gif': 'image/gif',
        '.webp': 'image/webp',
    }
    media_type = media_types.get(ext, 'application/octet-stream')

    return FileResponse(file_path, media_type=media_type)
