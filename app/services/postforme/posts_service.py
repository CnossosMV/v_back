"""
Post for Me Posts Service
High-level service for managing social media posts with database synchronization
"""
from typing import Optional, Dict, Any, List
from sqlalchemy.orm import Session
from datetime import datetime
import logging

from app.models import (
    PostForMePost,
    PostForMePostResult,
    PostForMeSocialAccount,
    User,
    Project
)
from .client import PostForMeClient

logger = logging.getLogger(__name__)


class PostsService:
    """
    High-level service for managing social media posts
    Handles database synchronization and business logic
    """

    def __init__(self, db: Session, api_key: str):
        """
        Initialize service

        Args:
            db: Database session
            api_key: Post for Me API key
        """
        self.db = db
        self.client = PostForMeClient(api_key)

    async def create_post(
        self,
        project_id: int,
        user_id: int,
        account_ids: List[str],
        content: Dict[str, Any],
        scheduled_time: Optional[datetime] = None,
        external_id: Optional[str] = None,
        tags: Optional[List[str]] = None
    ) -> PostForMePost:
        """
        Create a social media post

        Args:
            project_id: Project ID
            user_id: User creating the post
            account_ids: List of Post for Me social account IDs
            content: Post content structure
                {
                    "default": {"text": "...", "mediaIds": [...]},
                    "instagram": {"text": "..."}  # Platform overrides
                }
            scheduled_time: When to publish (None = publish now)
            external_id: External reference ID (e.g., from workflow)
            tags: Optional tags for organization

        Returns:
            PostForMePost instance
        """
        logger.info(f"Creating post for project {project_id} with {len(account_ids)} accounts")

        # Prepare API payload
        api_payload = {
            "socialAccountIds": account_ids,
            "content": content
        }

        if scheduled_time:
            api_payload["scheduledTime"] = scheduled_time.isoformat()

        if external_id:
            api_payload["externalId"] = external_id

        # Create post via API
        try:
            api_response = await self.client.create_social_post(api_payload)
            logger.info(f"Post created in Post for Me API: {api_response.get('id')}")
        except Exception as e:
            logger.error(f"Failed to create post in Post for Me API: {str(e)}")
            raise

        # Save to database
        db_post = PostForMePost(
            project_id=project_id,
            postforme_post_id=api_response.get("id"),
            content=content,
            target_account_ids=account_ids,
            status='scheduled' if scheduled_time else 'publishing',
            scheduled_time=scheduled_time,
            external_id=external_id,
            tags=tags or [],
            created_by=user_id
        )

        self.db.add(db_post)
        self.db.commit()
        self.db.refresh(db_post)

        logger.info(f"Post saved to database: {db_post.id}")

        return db_post

    async def get_post_with_results(self, post_id: int) -> Dict[str, Any]:
        """
        Get post with all platform-specific results

        Args:
            post_id: Database post ID

        Returns:
            Dictionary with post and results
        """
        post = self.db.query(PostForMePost).filter(PostForMePost.id == post_id).first()
        if not post:
            raise ValueError(f"Post {post_id} not found")

        # Fetch latest results from API if post has been submitted
        if post.postforme_post_id:
            try:
                api_results = await self.client.get_post_results(
                    social_post_id=post.postforme_post_id,
                    limit=100
                )

                # Update database with latest results
                for result_data in api_results.get("data", []):
                    self._sync_post_result(post.id, result_data)

            except Exception as e:
                logger.error(f"Failed to fetch post results from API: {str(e)}")
                # Continue with database results even if API fails

        # Return post with results
        self.db.refresh(post)
        return {
            "post": post,
            "results": post.results
        }

    async def update_post_status(
        self,
        post_id: int,
        status: str,
        published_at: Optional[datetime] = None
    ) -> PostForMePost:
        """
        Update post status

        Args:
            post_id: Database post ID
            status: New status
            published_at: Publication timestamp

        Returns:
            Updated post
        """
        post = self.db.query(PostForMePost).filter(PostForMePost.id == post_id).first()
        if not post:
            raise ValueError(f"Post {post_id} not found")

        post.status = status
        if published_at:
            post.published_at = published_at

        self.db.commit()
        self.db.refresh(post)

        logger.info(f"Post {post_id} status updated to {status}")

        return post

    async def sync_post_results(self, post_id: int) -> List[PostForMePostResult]:
        """
        Manually sync post results from Post for Me API

        Args:
            post_id: Database post ID

        Returns:
            List of updated results
        """
        post = self.db.query(PostForMePost).filter(PostForMePost.id == post_id).first()
        if not post or not post.postforme_post_id:
            raise ValueError(f"Post {post_id} not found or not submitted to API")

        # Fetch from API
        api_results = await self.client.get_post_results(
            social_post_id=post.postforme_post_id,
            limit=100
        )

        # Sync to database
        results = []
        for result_data in api_results.get("data", []):
            result = self._sync_post_result(post.id, result_data)
            if result:
                results.append(result)

        logger.info(f"Synced {len(results)} results for post {post_id}")

        return results

    def _sync_post_result(
        self,
        post_id: int,
        result_data: Dict[str, Any]
    ) -> Optional[PostForMePostResult]:
        """
        Sync a single post result from API to database

        Args:
            post_id: Database post ID
            result_data: Result data from API

        Returns:
            PostForMePostResult instance or None
        """
        # Find social account by Post for Me ID
        social_account = self.db.query(PostForMeSocialAccount).filter(
            PostForMeSocialAccount.postforme_account_id == result_data.get("socialAccountId")
        ).first()

        if not social_account:
            logger.warning(f"Social account {result_data.get('socialAccountId')} not found in database")
            return None

        # Check if result exists
        result = self.db.query(PostForMePostResult).filter(
            PostForMePostResult.postforme_result_id == result_data.get("id")
        ).first()

        if result:
            # Update existing
            result.status = result_data.get("status")
            result.platform_post_id = result_data.get("platformPostId")
            result.platform_post_url = result_data.get("platformPostUrl")
            result.error_message = result_data.get("errorMessage")
            result.error_code = result_data.get("errorCode")
            result.platform_response = result_data.get("platformResponse", {})
            result.updated_at = datetime.utcnow()

            if result_data.get("publishedAt"):
                result.published_at = datetime.fromisoformat(
                    result_data["publishedAt"].replace("Z", "+00:00")
                )
        else:
            # Create new
            result = PostForMePostResult(
                post_id=post_id,
                postforme_result_id=result_data.get("id"),
                social_account_id=social_account.id,
                platform=result_data.get("platform"),
                status=result_data.get("status"),
                platform_post_id=result_data.get("platformPostId"),
                platform_post_url=result_data.get("platformPostUrl"),
                error_message=result_data.get("errorMessage"),
                error_code=result_data.get("errorCode"),
                platform_response=result_data.get("platformResponse", {}),
                published_at=datetime.fromisoformat(
                    result_data["publishedAt"].replace("Z", "+00:00")
                ) if result_data.get("publishedAt") else None
            )
            self.db.add(result)

        self.db.commit()
        return result

    async def delete_post(self, post_id: int) -> None:
        """
        Delete a post

        Args:
            post_id: Database post ID
        """
        post = self.db.query(PostForMePost).filter(PostForMePost.id == post_id).first()
        if not post:
            raise ValueError(f"Post {post_id} not found")

        # Delete from Post for Me API if it exists there
        if post.postforme_post_id:
            try:
                await self.client.delete_social_post(post.postforme_post_id)
                logger.info(f"Post {post.postforme_post_id} deleted from Post for Me API")
            except Exception as e:
                logger.warning(f"Failed to delete post from API: {str(e)}")
                # Continue with database deletion even if API fails

        # Delete from database (cascade will handle results)
        self.db.delete(post)
        self.db.commit()

        logger.info(f"Post {post_id} deleted from database")
