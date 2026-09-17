"""
Post for Me API Client
Low-level HTTP client for interacting with Post for Me API
Documentation: https://api.postforme.dev/docs
"""
import httpx
from typing import Optional, Dict, Any, List
import logging

logger = logging.getLogger(__name__)


class PostForMeClient:
    """
    HTTP client for Post for Me API
    Handles authentication and all API requests
    """

    BASE_URL = "https://api.postforme.dev/v1"
    TIMEOUT = 30.0  # seconds

    def __init__(self, api_key: str):
        """
        Initialize client with API key

        Args:
            api_key: Post for Me API key
        """
        self.api_key = api_key
        self.headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json"
        }

    async def _request(
        self,
        method: str,
        endpoint: str,
        params: Optional[Dict] = None,
        json_data: Optional[Dict] = None
    ) -> Dict[str, Any]:
        """
        Make HTTP request to Post for Me API

        Args:
            method: HTTP method (GET, POST, PUT, DELETE, PATCH)
            endpoint: API endpoint (e.g., "/social-accounts")
            params: Query parameters
            json_data: JSON body data

        Returns:
            Response JSON data

        Raises:
            httpx.HTTPStatusError: If request fails
        """
        url = f"{self.BASE_URL}{endpoint}"

        logger.info(f"PostForMe API Request: {method} {endpoint}")

        async with httpx.AsyncClient() as client:
            try:
                response = await client.request(
                    method=method,
                    url=url,
                    headers=self.headers,
                    params=params,
                    json=json_data,
                    timeout=self.TIMEOUT
                )
                response.raise_for_status()

                # Handle empty responses (e.g., DELETE)
                if response.status_code == 204 or not response.content:
                    return {}

                return response.json()

            except httpx.HTTPStatusError as e:
                logger.error(f"PostForMe API Error: {e.response.status_code} - {e.response.text}")
                raise
            except Exception as e:
                logger.error(f"PostForMe API Request failed: {str(e)}")
                raise

    # ==========================================
    # Social Accounts
    # ==========================================

    async def get_social_accounts(
        self,
        platform: Optional[str] = None,
        page: int = 1,
        limit: int = 50
    ) -> Dict[str, Any]:
        """
        List connected social accounts

        Args:
            platform: Filter by platform (facebook, instagram, twitter, etc.)
            page: Page number
            limit: Results per page (max 100)

        Returns:
            {
                "data": [...],
                "pagination": {...}
            }
        """
        params = {"page": page, "limit": limit}
        if platform:
            params["platform"] = platform

        return await self._request("GET", "/social-accounts", params=params)

    async def get_social_account(self, account_id: str) -> Dict[str, Any]:
        """
        Get specific social account details

        Args:
            account_id: Post for Me account ID

        Returns:
            Account details
        """
        return await self._request("GET", f"/social-accounts/{account_id}")

    async def create_social_account(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """
        Add or update social account

        Args:
            data: Account data

        Returns:
            Created account
        """
        return await self._request("POST", "/social-accounts", json_data=data)

    async def update_social_account(
        self,
        account_id: str,
        data: Dict[str, Any]
    ) -> Dict[str, Any]:
        """
        Update social account

        Args:
            account_id: Post for Me account ID
            data: Update data

        Returns:
            Updated account
        """
        return await self._request("PATCH", f"/social-accounts/{account_id}", json_data=data)

    async def disconnect_social_account(self, account_id: str) -> Dict[str, Any]:
        """
        Disconnect social account

        Args:
            account_id: Post for Me account ID

        Returns:
            Success response
        """
        return await self._request("POST", f"/social-accounts/{account_id}/disconnect")

    async def generate_auth_url(
        self,
        platform: str,
        redirect_url: str
    ) -> Dict[str, Any]:
        """
        Generate OAuth URL for account connection

        Args:
            platform: Platform name (facebook, instagram, twitter, etc.)
            redirect_url: Callback URL after OAuth

        Returns:
            {
                "authUrl": "https://..."
            }
        """
        data = {
            "platform": platform,
            "redirectUrl": redirect_url
        }
        return await self._request("POST", "/social-accounts/auth-url", json_data=data)

    # ==========================================
    # Social Posts
    # ==========================================

    async def get_social_posts(
        self,
        platform: Optional[str] = None,
        status: Optional[str] = None,
        external_id: Optional[str] = None,
        page: int = 1,
        limit: int = 50
    ) -> Dict[str, Any]:
        """
        List social posts

        Args:
            platform: Filter by platform
            status: Filter by status (draft, scheduled, published, failed)
            external_id: Filter by external ID
            page: Page number
            limit: Results per page (max 100)

        Returns:
            {
                "data": [...],
                "pagination": {...}
            }
        """
        params = {"page": page, "limit": limit}
        if platform:
            params["platform"] = platform
        if status:
            params["status"] = status
        if external_id:
            params["externalId"] = external_id

        return await self._request("GET", "/social-posts", params=params)

    async def get_social_post(self, post_id: str) -> Dict[str, Any]:
        """
        Get specific post details

        Args:
            post_id: Post for Me post ID

        Returns:
            Post details
        """
        return await self._request("GET", f"/social-posts/{post_id}")

    async def create_social_post(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """
        Create new social post

        Args:
            data: Post data
                {
                    "socialAccountIds": ["account-1", "account-2"],
                    "content": {
                        "default": {"text": "...", "mediaIds": [...]},
                        "instagram": {"text": "..."}  # Platform overrides
                    },
                    "scheduledTime": "2025-10-31T10:00:00Z",  # Optional
                    "externalId": "my-ref-123"  # Optional
                }

        Returns:
            Created post
        """
        return await self._request("POST", "/social-posts", json_data=data)

    async def update_social_post(
        self,
        post_id: str,
        data: Dict[str, Any]
    ) -> Dict[str, Any]:
        """
        Update existing post

        Args:
            post_id: Post for Me post ID
            data: Update data

        Returns:
            Updated post
        """
        return await self._request("PUT", f"/social-posts/{post_id}", json_data=data)

    async def delete_social_post(self, post_id: str) -> Dict[str, Any]:
        """
        Delete post

        Args:
            post_id: Post for Me post ID

        Returns:
            Empty dict on success
        """
        return await self._request("DELETE", f"/social-posts/{post_id}")

    # ==========================================
    # Post Results
    # ==========================================

    async def get_post_results(
        self,
        social_post_id: Optional[str] = None,
        platform: Optional[str] = None,
        status: Optional[str] = None,
        page: int = 1,
        limit: int = 50
    ) -> Dict[str, Any]:
        """
        Get post publication results

        Args:
            social_post_id: Filter by post ID
            platform: Filter by platform
            status: Filter by status (pending, published, failed)
            page: Page number
            limit: Results per page (max 100)

        Returns:
            {
                "data": [...],
                "pagination": {...}
            }
        """
        params = {"page": page, "limit": limit}
        if social_post_id:
            params["socialPostId"] = social_post_id
        if platform:
            params["platform"] = platform
        if status:
            params["status"] = status

        return await self._request("GET", "/social-post-results", params=params)

    async def get_post_result(self, result_id: str) -> Dict[str, Any]:
        """
        Get specific post result

        Args:
            result_id: Post for Me result ID

        Returns:
            Result details
        """
        return await self._request("GET", f"/social-post-results/{result_id}")

    # ==========================================
    # Media
    # ==========================================

    async def create_upload_url(self, file_name: str) -> Dict[str, Any]:
        """
        Generate signed URL for media upload

        Args:
            file_name: Name of file to upload

        Returns:
            {
                "id": "media-id",
                "uploadUrl": "https://...",
                "expiresAt": "2025-10-31T10:00:00Z"
            }
        """
        data = {"fileName": file_name}
        return await self._request("POST", "/media/create-upload-url", json_data=data)

    # ==========================================
    # Post Previews
    # ==========================================

    async def create_post_preview(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """
        Generate post preview

        Args:
            data: Preview data
                {
                    "socialAccountId": "account-1",
                    "content": {"text": "...", "mediaIds": [...]}
                }

        Returns:
            Preview image data
        """
        return await self._request("POST", "/social-post-previews", json_data=data)

    # ==========================================
    # Webhooks
    # ==========================================

    async def get_webhooks(self, page: int = 1, limit: int = 50) -> Dict[str, Any]:
        """
        List webhooks

        Args:
            page: Page number
            limit: Results per page (max 100)

        Returns:
            {
                "data": [...],
                "pagination": {...}
            }
        """
        params = {"page": page, "limit": limit}
        return await self._request("GET", "/webhooks", params=params)

    async def get_webhook(self, webhook_id: str) -> Dict[str, Any]:
        """
        Get webhook details

        Args:
            webhook_id: Webhook ID

        Returns:
            Webhook details
        """
        return await self._request("GET", f"/webhooks/{webhook_id}")

    async def create_webhook(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """
        Create webhook

        Args:
            data: Webhook data
                {
                    "url": "https://...",
                    "events": ["social-post.created", "social-post.updated"]
                }

        Returns:
            Created webhook
        """
        return await self._request("POST", "/webhooks", json_data=data)

    async def update_webhook(
        self,
        webhook_id: str,
        data: Dict[str, Any]
    ) -> Dict[str, Any]:
        """
        Update webhook

        Args:
            webhook_id: Webhook ID
            data: Update data

        Returns:
            Updated webhook
        """
        return await self._request("PATCH", f"/webhooks/{webhook_id}", json_data=data)

    async def delete_webhook(self, webhook_id: str) -> Dict[str, Any]:
        """
        Delete webhook

        Args:
            webhook_id: Webhook ID

        Returns:
            Empty dict on success
        """
        return await self._request("DELETE", f"/webhooks/{webhook_id}")
