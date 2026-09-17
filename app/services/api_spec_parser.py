"""
API Spec Parser Service

Uses LLM to parse markdown API documentation and extract structured endpoint definitions.
"""
import json
import logging
from typing import Dict, List

from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

PARSE_SYSTEM_PROMPT = """You are an API documentation parser. Given markdown API documentation, extract a structured list of endpoints.

For each endpoint, provide:
- name: A human-readable name (e.g. "Get User Data")
- slug: A snake_case identifier (e.g. "get_user_data")
- method: HTTP method (GET, POST, PUT, PATCH, DELETE)
- path: The URL path relative to the base URL (e.g. "/users/{identifier}")
- description: A brief description of what the endpoint does
- parameters_schema: JSON Schema for path and query parameters (if any)
- request_body_schema: JSON Schema for the request body (if any)
- when_to_use: A natural language description of when an AI agent should use this endpoint

Also try to identify:
- base_url_suggestion: The base URL if mentioned in the docs
- auth_type_suggestion: The authentication type (api_key_header, bearer_token, basic_auth, custom_header, none)

Respond ONLY with valid JSON in this exact format:
{
  "base_url_suggestion": "https://api.example.com" or null,
  "auth_type_suggestion": "api_key_header" or null,
  "endpoints": [
    {
      "name": "Get User",
      "slug": "get_user",
      "method": "GET",
      "path": "/users/{identifier}",
      "description": "Get user details by identifier",
      "parameters_schema": {"type": "object", "properties": {"identifier": {"type": "string", "description": "User identifier"}}},
      "request_body_schema": null,
      "when_to_use": "Use this when you need to look up a user's profile or subscription details"
    }
  ]
}"""


class ApiSpecParser:
    """Parses markdown API documentation using LLM."""

    def __init__(self, db: Session, project_id: int):
        self.db = db
        self.project_id = project_id

    def parse_markdown_spec(self, markdown_text: str) -> Dict:
        """
        Parse markdown API documentation into structured endpoint definitions.

        Args:
            markdown_text: Raw markdown documentation

        Returns:
            Dict with base_url_suggestion, auth_type_suggestion, and endpoints list
        """
        from app.services.chatbot.llm_key_resolver import resolve_llm, create_chat_llm

        try:
            llm_cfg = resolve_llm(self.db, self.project_id, purpose="spec_parse")
        except ValueError as e:
            logger.error(f"Failed to resolve LLM for spec parsing: {e}")
            raise ValueError("No LLM API key configured. Add an LLM key in Credentials to use AI parsing.")

        try:
            llm = create_chat_llm(llm_cfg, max_tokens=4000)

            response = llm.invoke([
                {"role": "system", "content": PARSE_SYSTEM_PROMPT},
                {"role": "user", "content": f"Parse the following API documentation:\n\n{markdown_text[:8000]}"},
            ])

            result_text = response.content.strip()

            # Extract JSON from response (handle markdown code blocks)
            if "```json" in result_text:
                result_text = result_text.split("```json")[1].split("```")[0].strip()
            elif "```" in result_text:
                result_text = result_text.split("```")[1].split("```")[0].strip()

            parsed = json.loads(result_text)

            # Validate structure
            if "endpoints" not in parsed:
                parsed = {"base_url_suggestion": None, "auth_type_suggestion": None, "endpoints": []}

            return parsed

        except json.JSONDecodeError as e:
            logger.error(f"Failed to parse LLM response as JSON: {e}")
            raise ValueError("LLM returned invalid JSON. Try again or simplify the documentation.")

        except Exception as e:
            logger.error(f"Spec parsing failed: {e}")
            raise ValueError(f"Failed to parse API documentation: {str(e)}")
