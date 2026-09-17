"""
LangChain RAG Service

Core service for retrieval-augmented generation using LangChain and OpenAI GPT-4o-mini.
"""

import os
import time
from typing import List, Dict, Optional, Tuple
import logging

from langchain.chains import ConversationalRetrievalChain
from langchain.memory import ConversationBufferMemory
from langchain.prompts import (
    ChatPromptTemplate,
    SystemMessagePromptTemplate,
    HumanMessagePromptTemplate,
    MessagesPlaceholder,
)
from langchain.schema import HumanMessage, AIMessage

from .vector_store import VectorStoreService
from .knowledge_loader import KnowledgeLoader

logger = logging.getLogger(__name__)


class LangChainService:
    """Service for RAG-based chat using LangChain."""

    def __init__(
        self,
        openai_api_key: str,
        vector_store_service: VectorStoreService,
        knowledge_loader: KnowledgeLoader,
        model_name: str = "gpt-4o-mini",
        temperature: float = 0.7,
        max_tokens: int = 2000,
    ):
        """
        Initialize the LangChain service.

        Args:
            openai_api_key: OpenAI API key (used for backward compatibility)
            vector_store_service: Vector store service instance
            knowledge_loader: Knowledge loader instance
            model_name: OpenAI model to use
            temperature: LLM temperature (0-1)
            max_tokens: Maximum tokens in response
        """
        self.openai_api_key = openai_api_key
        self.vector_store_service = vector_store_service
        self.knowledge_loader = knowledge_loader
        self.model_name = model_name
        self.temperature = temperature
        self.max_tokens = max_tokens

        # Initialize LLM using factory (supports multi-provider)
        from .llm_key_resolver import LLMConfig, create_chat_llm, infer_provider
        provider = infer_provider(model_name)
        cfg = LLMConfig(api_key=openai_api_key, provider=provider, model=model_name, temperature=temperature)
        self.llm = create_chat_llm(cfg, max_tokens=max_tokens)

    def generate_response(
        self,
        chatbot_id: int,
        query: str,
        conversation_history: List[Dict[str, str]],
        system_prompt: Optional[str] = None,
        knowledge_base_path: Optional[str] = None,
    ) -> Tuple[str, List[str], Dict]:
        """
        Generate a response using RAG.

        Args:
            chatbot_id: ID of the chatbot
            query: User's question/message
            conversation_history: List of previous messages
            system_prompt: Custom system prompt for the chatbot
            knowledge_base_path: Path to knowledge base for image retrieval

        Returns:
            Tuple of (response_text, image_paths, metadata)
        """
        start_time = time.time()

        try:
            # Step 1: Retrieve relevant documents from vector store
            retrieved_docs = self.vector_store_service.search_with_scores(
                chatbot_id=chatbot_id,
                query=query,
                k=5  # Get top 5 most relevant chunks
            )

            # Step 2: Build context from retrieved documents
            context = self._build_context(retrieved_docs)

            # Step 3: Find related images (use ONLY original query to avoid false matches)
            images = []
            if knowledge_base_path:
                # Only use the user's original query, not the retrieved context
                # This prevents matching images from irrelevant context
                images = self.knowledge_loader.find_related_images(
                    query=query,
                    knowledge_base_path=knowledge_base_path,
                    chatbot_id=chatbot_id,
                    max_images=3
                )

            # Step 4: Build the prompt
            prompt = self._build_prompt(
                query=query,
                context=context,
                conversation_history=conversation_history,
                system_prompt=system_prompt,
                has_images=len(images) > 0
            )

            # Step 5: Generate response with LLM
            response = self.llm.invoke(prompt)
            response_text = response.content

            # Step 6: Extract metadata
            processing_time = time.time() - start_time
            metadata = {
                "retrieved_documents": [
                    {
                        "content": doc.page_content[:200],  # First 200 chars
                        "score": float(score),
                        "source": doc.metadata.get("source", "unknown")
                    }
                    for doc, score in retrieved_docs
                ],
                "tokens_used": response.response_metadata.get("token_usage", {}),
                "processing_time": processing_time,
                "model": self.model_name,
            }

            logger.info(f"Generated response for chatbot {chatbot_id} in {processing_time:.2f}s")

            return response_text, images, metadata

        except Exception as e:
            logger.error(f"Error generating response: {str(e)}")
            raise

    def _build_context(self, retrieved_docs: List[Tuple]) -> str:
        """
        Build context string from retrieved documents.

        Args:
            retrieved_docs: List of (document, score) tuples

        Returns:
            Formatted context string
        """
        if not retrieved_docs:
            return "No relevant information found in the knowledge base."

        context_parts = []
        for i, (doc, score) in enumerate(retrieved_docs, 1):
            source = doc.metadata.get("source", "Unknown")
            context_parts.append(
                f"[Source {i}] (Relevance: {score:.2f})\n{doc.page_content}\n"
            )

        return "\n---\n".join(context_parts)

    def _build_prompt(
        self,
        query: str,
        context: str,
        conversation_history: List[Dict[str, str]],
        system_prompt: Optional[str] = None,
        has_images: bool = False
    ) -> List:
        """
        Build the prompt for the LLM.

        Args:
            query: User's question
            context: Retrieved context from knowledge base
            conversation_history: Previous conversation messages
            system_prompt: Custom system prompt
            has_images: Whether images were found

        Returns:
            List of messages for the LLM
        """
        # Default system prompt
        default_system_prompt = """You are a helpful AI assistant with access to a knowledge base.
Use the provided context to answer the user's question accurately and helpfully.

Guidelines:
1. Answer based primarily on the provided context
2. If the context doesn't contain enough information, say so clearly
3. Be concise but thorough
4. Use a friendly, professional tone
5. DO NOT include image markdown syntax (![alt](image.jpg)) in your response - images are handled automatically
6. If relevant images are available, you can mention them naturally (e.g., "I've included some helpful screenshots")
7. Format your response with markdown when appropriate (bold, lists, etc.)"""

        system_message = system_prompt or default_system_prompt

        # Add image instruction if applicable
        if has_images:
            system_message += "\n\nNote: Relevant images have been found and will be displayed automatically with your response. Do not include image markdown syntax."

        # Build messages
        messages = [
            {"role": "system", "content": system_message}
        ]

        # Add conversation history (last 10 messages)
        for msg in conversation_history[-10:]:
            messages.append({
                "role": msg["role"],
                "content": msg["content"]
            })

        # Add current query with context
        user_message = f"""Context from knowledge base:
{context}

Question: {query}

Please provide a helpful answer based on the context above."""

        messages.append({"role": "user", "content": user_message})

        return messages

    def generate_simple_response(
        self,
        query: str,
        system_prompt: Optional[str] = None
    ) -> str:
        """
        Generate a simple response without RAG (no knowledge base).

        Args:
            query: User's question
            system_prompt: Custom system prompt

        Returns:
            Response text
        """
        try:
            messages = [
                {"role": "system", "content": system_prompt or "You are a helpful AI assistant."},
                {"role": "user", "content": query}
            ]

            response = self.llm.invoke(messages)
            return response.content

        except Exception as e:
            logger.error(f"Error generating simple response: {str(e)}")
            raise

    def generate_response_with_tools(
        self,
        chatbot_id: int,
        query: str,
        conversation_history: List[Dict[str, str]],
        system_prompt: Optional[str] = None,
        knowledge_base_path: Optional[str] = None,
        tools: Optional[List[Dict]] = None,
        tool_executor_fn=None,
    ) -> Tuple[str, List[str], Dict]:
        """
        Generate a response with optional tool calling.

        When tools are available, uses OpenAI function calling. If the LLM
        requests a tool call, executes it via tool_executor_fn and feeds
        the result back for a final response.

        Args:
            chatbot_id: ID for vector store lookup
            query: User's question/message
            conversation_history: Previous messages
            system_prompt: Custom system prompt
            knowledge_base_path: Path for image retrieval
            tools: List of tool definitions [{name, description, parameters}]
            tool_executor_fn: Callback fn(tool_name, arguments) -> result dict

        Returns:
            Tuple of (response_text, image_paths, metadata)
        """
        import json
        start_time = time.time()

        if not tools:
            # No tools — use standard RAG response
            return self.generate_response(
                chatbot_id, query, conversation_history,
                system_prompt, knowledge_base_path,
            )

        try:
            # Step 1: Retrieve relevant documents
            retrieved_docs = self.vector_store_service.search_with_scores(
                chatbot_id=chatbot_id, query=query, k=5,
            )
            context = self._build_context(retrieved_docs)

            images = []
            if knowledge_base_path:
                images = self.knowledge_loader.find_related_images(
                    query=query,
                    knowledge_base_path=knowledge_base_path,
                    chatbot_id=chatbot_id,
                    max_images=3,
                )

            # Step 2: Build messages
            prompt = self._build_prompt(
                query=query, context=context,
                conversation_history=conversation_history,
                system_prompt=system_prompt, has_images=len(images) > 0,
            )

            # Step 3: Convert tools to OpenAI function format
            openai_tools = []
            for tool_def in tools:
                openai_tools.append({
                    "type": "function",
                    "function": {
                        "name": tool_def["name"],
                        "description": tool_def.get("description", ""),
                        "parameters": tool_def.get("parameters", {
                            "type": "object",
                            "properties": {},
                        }),
                    },
                })

            # Step 4: Call LLM with tools
            llm_with_tools = self.llm.bind(tools=openai_tools)
            response = llm_with_tools.invoke(prompt)

            tool_calls_metadata = []

            # Step 5: Process tool calls if any
            if hasattr(response, 'tool_calls') and response.tool_calls:
                for tool_call in response.tool_calls:
                    tool_name = tool_call.get("name", "")
                    tool_args = tool_call.get("args", {})

                    tool_result = None
                    if tool_executor_fn:
                        tool_result = tool_executor_fn(tool_name, tool_args)

                    tool_calls_metadata.append({
                        "name": tool_name,
                        "arguments": tool_args,
                        "result": tool_result,
                    })

                    # Feed tool result back to LLM
                    tool_result_content = json.dumps(
                        tool_result.get("result", {}) if tool_result else {"error": "Tool not available"}
                    )

                    prompt.append({
                        "role": "assistant",
                        "content": response.content or "",
                        "tool_calls": [tool_call],
                    })
                    prompt.append({
                        "role": "tool",
                        "content": tool_result_content,
                        "tool_call_id": tool_call.get("id", ""),
                    })

                # Get final response after tool results
                response = self.llm.invoke(prompt)

            response_text = response.content

            # Build metadata
            processing_time = time.time() - start_time
            metadata = {
                "retrieved_documents": [
                    {
                        "content": doc.page_content[:200],
                        "score": float(score),
                        "source": doc.metadata.get("source", "unknown"),
                    }
                    for doc, score in retrieved_docs
                ],
                "tokens_used": response.response_metadata.get("token_usage", {}),
                "processing_time": processing_time,
                "model": self.model_name,
                "tools_called": tool_calls_metadata,
            }

            return response_text, images, metadata

        except Exception as e:
            logger.error(f"Error generating response with tools: {str(e)}")
            raise

    def update_model_config(
        self,
        model_name: Optional[str] = None,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None
    ):
        """
        Update LLM configuration.

        Args:
            model_name: New model name
            temperature: New temperature
            max_tokens: New max tokens
        """
        if model_name:
            self.model_name = model_name
        if temperature is not None:
            self.temperature = temperature
        if max_tokens:
            self.max_tokens = max_tokens

        # Reinitialize LLM with new config
        from .llm_key_resolver import LLMConfig, create_chat_llm, infer_provider
        provider = infer_provider(self.model_name)
        cfg = LLMConfig(api_key=self.openai_api_key, provider=provider, model=self.model_name, temperature=self.temperature)
        self.llm = create_chat_llm(cfg, max_tokens=self.max_tokens)

        logger.info(f"Updated LLM config: model={self.model_name}, temp={self.temperature}, max_tokens={self.max_tokens}")
