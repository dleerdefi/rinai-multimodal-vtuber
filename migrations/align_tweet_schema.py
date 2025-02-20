import sys
import os
from pathlib import Path
import asyncio
import logging
from datetime import datetime, UTC
from typing import Dict, List

# Add project root to Python path
project_root = str(Path(__file__).parent.parent)
sys.path.append(project_root)

# Now we can import from src
from src.db.db_schema import (
    RinDB, 
    ContentType, 
    OperationStatus, 
    ToolItemContent,
    ToolItemParams,
    ToolItemMetadata,
    ToolItemResponse,
    OperationMetadata,
    Tweet
)
from src.db.mongo_manager import MongoManager

logger = logging.getLogger(__name__)

async def migrate_to_tool_items(db: RinDB):
    """Migrate tweets to generic tool items"""
    try:
        # Check if old collection exists
        collections = await db.db.list_collection_names()
        if 'rin.tweets' not in collections:
            logger.info("No tweets collection found, skipping migration")
            return

        tweets = await db.tweets.find({}).to_list(None)
        if not tweets:
            logger.info("No tweets to migrate")
            return
            
        logger.info(f"Found {len(tweets)} tweets to migrate")
        
        for tweet in tweets:
            tool_item = {
                # Base ToolItem fields
                "session_id": tweet.get("session_id"),
                "workflow_id": tweet.get("workflow_id"),
                "tool_operation_id": tweet.get("tool_operation_id"),
                "content_type": ContentType.TWEET.value,
                "status": tweet.get("status", OperationStatus.PENDING.value),
                
                # Content structure
                "content": {
                    "raw_content": tweet.get("content"),
                    "formatted_content": tweet.get("formatted_content"),
                    "thread_structure": tweet.get("thread_structure", []),
                    "mentions": tweet.get("mentions", []),
                    "hashtags": tweet.get("hashtags", []),
                    "urls": tweet.get("urls", []),
                    "version": "1.0"
                },
                
                # Tool-specific parameters
                "parameters": {
                    "account_id": tweet.get("twitter_api_params", {}).get("account_id"),
                    "media_files": tweet.get("twitter_api_params", {}).get("media_files", []),
                    "poll_options": tweet.get("twitter_api_params", {}).get("poll_options", []),
                    "poll_duration": tweet.get("twitter_api_params", {}).get("poll_duration"),
                    "schedule_time": tweet.get("scheduled_time"),
                    "retry_policy": {"max_attempts": 3, "delay": 300}
                },
                
                # Enhanced metadata
                "metadata": {
                    "generated_at": tweet.get("metadata", {}).get("generated_at", datetime.now(UTC).isoformat()),
                    "generated_by": tweet.get("metadata", {}).get("generated_by", "system"),
                    "last_modified": datetime.now(UTC).isoformat(),
                    "version": "1.0"
                },
                
                # Timing
                "created_at": tweet.get("created_at", datetime.now(UTC)),
                "scheduled_time": tweet.get("scheduled_time"),
                "executed_time": tweet.get("executed_time"),
                "posted_time": tweet.get("posted_time"),
                
                # Operation tracking
                "schedule_id": tweet.get("schedule_id"),
                "operation_id": tweet.get("operation_id"),
                "execution_id": tweet.get("execution_id"),
                
                # Error handling
                "retry_count": tweet.get("retry_count", 0),
                "last_error": tweet.get("last_error")
            }
            
            result = await db.tool_items.insert_one(tool_item)
            logger.info(f"Migrated tweet {tweet.get('_id')} to tool_item {result.inserted_id}")
            
    except Exception as e:
        logger.error(f"Error migrating tweets: {e}")
        raise

async def migrate_tweet_schedules_to_scheduled_operations(db: RinDB):
    """Migrate tweet schedules to scheduled operations"""
    try:
        schedules = await db.tweet_schedules.find({}).to_list(None)
        
        for schedule in schedules:
            scheduled_op = {
                "session_id": schedule["session_id"],
                "workflow_id": schedule.get("workflow_id"),
                "tool_operation_id": schedule.get("operation_id"),
                "content_type": ContentType.TWEET.value,
                "status": schedule["status"],
                
                # Generic scheduling parameters
                "count": schedule.get("total_items", 1),
                "schedule_type": "one_time",  # Default to one_time for migration
                "schedule_time": schedule.get("schedule_info", {}).get("schedule_time"),
                "approval_required": True,  # Default to requiring approval
                
                # Content management
                "content": schedule.get("content", {}),
                "pending_items": schedule.get("pending_tweets", []),
                "approved_items": schedule.get("approved_tweets", []),
                "rejected_items": schedule.get("rejected_tweets", []),
                
                # Timing
                "created_at": schedule["created_at"],
                "scheduled_time": schedule.get("scheduled_time"),
                "executed_time": schedule.get("executed_time"),
                
                # Metadata
                "metadata": {
                    "content_type": ContentType.TWEET.value,
                    "original_schedule_id": str(schedule["_id"]),
                    "migrated_at": datetime.now(UTC).isoformat()
                },
                "retry_count": schedule.get("retry_count", 0),
                "last_error": schedule.get("last_error")
            }
            
            result = await db.scheduled_operations.insert_one(scheduled_op)
            logger.info(f"Migrated schedule {schedule['_id']} to scheduled operation {result.inserted_id}")
            
    except Exception as e:
        logger.error(f"Error migrating schedules: {e}")
        raise

async def cleanup_old_collections(db: RinDB):
    """Remove old collections after successful migration"""
    try:
        collections = await db.db.list_collection_names()
        
        # Only delete if old collections exist
        if 'rin.tweets' in collections:
            await db.db.drop_collection('rin.tweets')
            logger.info("Dropped rin.tweets collection")
            
        if 'rin.tweet_schedules' in collections:
            await db.db.drop_collection('rin.tweet_schedules')
            logger.info("Dropped rin.tweet_schedules collection")
            
    except Exception as e:
        logger.error(f"Error cleaning up old collections: {e}")
        raise

async def run_migration():
    """Run the migration"""
    try:
        # Initialize MongoDB connection
        mongo_manager = MongoManager()
        db = RinDB(mongo_manager.client)
        
        # Run migrations
        await migrate_to_tool_items(db)
        await migrate_tweet_schedules_to_scheduled_operations(db)
        await cleanup_old_collections(db)
        logger.info("Migration completed successfully")
        
    except Exception as e:
        logger.error(f"Migration failed: {e}")
        raise
    finally:
        # Close MongoDB connection
        mongo_manager.client.close()

if __name__ == "__main__":
    # Setup logging
    logging.basicConfig(level=logging.INFO)
    
    # Run migration
    asyncio.run(run_migration()) 