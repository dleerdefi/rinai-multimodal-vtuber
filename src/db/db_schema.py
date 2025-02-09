from typing import TypedDict, List, Optional, Dict, Literal
from datetime import datetime
import logging
import uuid
from bson.objectid import ObjectId
from motor.motor_asyncio import AsyncIOMotorClient
from enum import Enum

logger = logging.getLogger(__name__)

class Message(TypedDict):
    role: str  # "host" or username from livestream
    content: str
    timestamp: datetime
    interaction_type: str  # "local_agent" or "livestream"
    session_id: str

class Session(TypedDict):
    session_id: str
    messages: List[Message]
    created_at: datetime
    last_updated: datetime
    metadata: Optional[dict]

class ContextConfiguration(TypedDict):
    session_id: str
    latest_summary: Optional[dict]  # The most recent summary message
    active_message_ids: List[str]   # IDs of messages in current context
    last_updated: datetime

class TweetStatus(str, Enum):
    """Tweet status types"""
    PENDING = "pending"
    APPROVED = "approved"
    SCHEDULED = "scheduled"
    POSTED = "posted"
    FAILED = "failed"
    REJECTED = "rejected"

class Tweet(TypedDict):
    content: str
    status: str  # Will contain TweetStatus values
    created_at: datetime
    scheduled_time: Optional[datetime]  # When this specific tweet should be posted
    posted_time: Optional[datetime]
    metadata: Dict[str, any]  # estimated_engagement, schedule_time, etc
    twitter_api_params: Dict[str, any]  # API parameters
    twitter_response: Optional[Dict]  # Response from Twitter API after posting
    retry_count: int
    last_error: Optional[str]
    schedule_id: str  # Reference to parent schedule
    session_id: str   # Reference to conversation session

class TweetSchedule(TypedDict):
    """Manages a group of related tweets to be posted according to a schedule"""
    session_id: str
    topic: str
    total_tweets_requested: int
    schedule_info: Dict[str, any]  # Overall schedule parameters (frequency, time slots, etc)
    approved_tweets: List[str]  # List of Tweet IDs
    pending_tweets: Optional[List[str]]  # List of Tweet IDs awaiting approval
    status: str  # 'collecting_approval', 'ready_to_schedule', 'scheduled', 'completed', 'error'
    created_at: datetime
    last_updated: datetime
    last_error: Optional[str]

class ToolOperation(TypedDict):
    session_id: str
    state: str  # Maps to ToolOperationState
    operation_type: str
    step: str
    data: Dict[str, any]
    created_at: datetime
    last_updated: datetime

class TwitterAPIParams(TypedDict):
    message: str
    account_id: str
    media_files: Optional[List[str]]
    poll_options: Optional[List[str]]
    poll_duration: Optional[int]

class TweetMetadata(TypedDict):
    estimated_engagement: str
    generated_at: str

class ValidatedTweet(TypedDict):
    content: str
    metadata: TweetMetadata
    twitter_api_params: TwitterAPIParams

class RinDB:
    def __init__(self, client: AsyncIOMotorClient):
        self.client = client
        self.db = client['rin_multimodal']
        self.messages = self.db['rin.messages']
        self.context_configs = self.db['rin.context_configs']
        self.tweets = self.db['rin.tweets']
        self.tweet_schedules = self.db['rin.tweet_schedules']
        self.tool_operations = self.db.get_collection('rin.tool_operations')
        self._initialized = False
        logger.info(f"Connected to database: {self.db.name}")

    async def initialize(self):
        """Initialize database and collections"""
        try:
            collections = await self.db.list_collection_names()
            
            # Create collections if they don't exist
            if 'rin.messages' not in collections:
                await self.db.create_collection('rin.messages')
                logger.info("Created rin.messages collection")
            
            if 'rin.context_configs' not in collections:
                await self.db.create_collection('rin.context_configs')
                logger.info("Created rin.context_configs collection")
            
            if 'rin.tweets' not in collections:
                await self.db.create_collection('rin.tweets')
                logger.info("Created rin.tweets collection")
            
            if 'rin.tweet_schedules' not in collections:
                await self.db.create_collection('rin.tweet_schedules')
                logger.info("Created rin.tweet_schedules collection")
            
            # Setup indexes
            await self._setup_indexes()
            self._initialized = True
            
            return True
            
        except Exception as e:
            logger.error(f"Failed to initialize database: {e}")
            raise

    async def is_initialized(self) -> bool:
        """Check if database is properly initialized"""
        try:
            # Check if collections exist
            collections = await self.db.list_collection_names()
            has_collections = all(
                col in collections 
                for col in ['rin.messages', 'rin.context_configs', 'rin.tweets', 'rin.tweet_schedules']
            )
            
            if not has_collections:
                logger.warning("Required collections not found")
                return False
                
            # Verify connection
            await self.db.command('ping')
            return True
        except Exception as e:
            logger.error(f"Database connection check failed: {e}")
            return False

    async def _setup_indexes(self):
        """Setup indexes for Rin collections"""
        try:
            await self.messages.create_index([("session_id", 1)])
            await self.messages.create_index([("timestamp", 1)])
            await self.context_configs.create_index([("session_id", 1)])
            
            # Indexes for tweet scheduling
            await self.tweet_schedules.create_index([("session_id", 1)])
            await self.tweet_schedules.create_index([("status", 1)])
            
            # Indexes for individual tweets
            await self.tweets.create_index([("status", 1)])
            await self.tweets.create_index([("schedule_id", 1)])
            await self.tweets.create_index([("session_id", 1)])
            await self.tweets.create_index([("scheduled_time", 1)])
            await self.tweets.create_index([("created_at", 1)])
            
            # Compound indexes for efficient querying
            await self.tweets.create_index([
                ("status", 1),
                ("scheduled_time", 1)
            ])
            
            # Tool operation indexes
            await self.tool_operations.create_index([("session_id", 1)])
            await self.tool_operations.create_index([("state", 1)])
            await self.tool_operations.create_index([("last_updated", 1)])
            
            logger.info("Successfully created database indexes")
        except Exception as e:
            logger.error(f"Error setting up indexes: {str(e)}")
            raise

    async def add_message(self, session_id: str, role: str, content: str, 
                         interaction_type: str = 'local_agent', metadata: Optional[dict] = None):
        """Add a message to the database
        
        Args:
            session_id: Unique session identifier
            role: Either "host" for local input or username for livestream
            content: Message content
            interaction_type: Either "local_agent" or "livestream"
            metadata: Optional additional metadata
        """
        message = {
            "session_id": session_id,
            "role": role,
            "content": content,
            "timestamp": datetime.utcnow(),
            "interaction_type": interaction_type
        }
        if metadata:
            message["metadata"] = metadata
            
        await self.messages.insert_one(message)
        return message

    async def get_session_messages(self, session_id: str):
        cursor = self.messages.find({"session_id": session_id}).sort("timestamp", 1)
        return await cursor.to_list(length=None)

    async def clear_session(self, session_id: str):
        await self.messages.delete_many({"session_id": session_id})


    async def update_session_metadata(self, session_id: str, metadata: dict):
        """Update or create session metadata"""
        try:
            await self.messages.update_many(
                {"session_id": session_id},
                {"$set": {"metadata": metadata}},
                upsert=True
            )
            logger.info(f"Updated metadata for session {session_id}")
            return True
        except Exception as e:
            logger.error(f"Failed to update session metadata: {e}")
            return False

    async def add_context_summary(self, session_id: str, summary: dict, active_message_ids: List[str]):
        """Update context configuration with new summary"""
        await self.context_configs.update_one(
            {"session_id": session_id},
            {
                "$set": {
                    "latest_summary": summary,
                    "active_message_ids": active_message_ids,
                    "last_updated": datetime.utcnow()
                }
            },
            upsert=True
        )

    async def get_context_configuration(self, session_id: str) -> Optional[ContextConfiguration]:
        """Get current context configuration"""
        return await self.context_configs.find_one({"session_id": session_id})

    async def get_messages_by_ids(self, session_id: str, message_ids: List[str]) -> List[Message]:
        """Get specific messages by their IDs"""
        cursor = self.messages.find({
            "session_id": session_id,
            "_id": {"$in": [ObjectId(id) for id in message_ids]}
        }).sort("timestamp", 1)
        return await cursor.to_list(length=None)

    async def create_tweet_schedule(self, session_id: str, topic: str, 
                                  total_tweets: int, schedule_info: Dict) -> str:
        """Create a new tweet schedule"""
        schedule = TweetSchedule(
            session_id=session_id,
            topic=topic,
            total_tweets_requested=total_tweets,
            schedule_info=schedule_info,
            approved_tweets=[],
            pending_tweets=None,
            status='collecting_approval',
            created_at=datetime.utcnow(),
            last_updated=datetime.utcnow(),
            last_error=None
        )
        result = await self.tweet_schedules.insert_one(schedule)
        return str(result.inserted_id)

    async def update_tweet_schedule(self, schedule_id: str, 
                                  approved_tweet_ids: Optional[List[str]] = None,
                                  pending_tweet_ids: Optional[List[str]] = None,
                                  status: Optional[str] = None,
                                  schedule_info: Optional[Dict] = None) -> bool:
        """Update a tweet schedule with new tweet IDs, status, or schedule info"""
        try:
            update_data = {"last_updated": datetime.utcnow()}
            if approved_tweet_ids is not None:
                update_data["approved_tweets"] = approved_tweet_ids
            if pending_tweet_ids is not None:
                update_data["pending_tweets"] = pending_tweet_ids
            if status:
                update_data["status"] = status
            if schedule_info:
                update_data["schedule_info"] = schedule_info

            result = await self.tweet_schedules.update_one(
                {"_id": ObjectId(schedule_id)},
                {"$set": update_data}
            )
            return result.modified_count > 0
        except Exception as e:
            logger.error(f"Error updating tweet schedule: {e}")
            return False

    async def get_tweet_schedule(self, schedule_id: str) -> Optional[TweetSchedule]:
        """Get a tweet schedule by ID"""
        try:
            return await self.tweet_schedules.find_one({"_id": ObjectId(schedule_id)})
        except Exception as e:
            logger.error(f"Error fetching tweet schedule: {e}")
            return None

    async def get_session_tweet_schedule(self, session_id: str) -> Optional[TweetSchedule]:
        """Get active tweet schedule for a session"""
        try:
            return await self.tweet_schedules.find_one({
                "session_id": session_id,
                "status": {"$in": ["collecting_approval", "ready_to_schedule"]}
            })
        except Exception as e:
            logger.error(f"Error fetching session tweet schedule: {e}")
            return None

    async def get_pending_scheduled_tweets(self) -> List[TweetSchedule]:
        """Get all tweet schedules ready for execution"""
        try:
            current_time = datetime.utcnow()
            cursor = self.tweet_schedules.find({
                "status": "scheduled",
                "execution_times": {"$lte": current_time}
            })
            return await cursor.to_list(length=None)
        except Exception as e:
            logger.error(f"Error fetching pending scheduled tweets: {e}")
            return []

    async def create_tweet(self, content: str, schedule_id: str, session_id: str,
                          scheduled_time: Optional[datetime] = None) -> str:
        """Create a new tweet"""
        tweet = Tweet(
            content=content,
            status=TweetStatus.PENDING,
            created_at=datetime.utcnow(),
            scheduled_time=scheduled_time,
            posted_time=None,
            metadata={},
            twitter_api_params={
                "message": content,
                "account_id": "default"
            },
            twitter_response=None,
            retry_count=0,
            last_error=None,
            schedule_id=schedule_id,
            session_id=session_id
        )
        result = await self.tweets.insert_one(tweet)
        return str(result.inserted_id)

    async def update_tweet_status(self, tweet_id: str, status: TweetStatus,
                                twitter_response: Optional[Dict] = None,
                                error: Optional[str] = None,
                                metadata: Optional[Dict] = None) -> bool:
        """Update tweet status and related fields"""
        try:
            update_data = {
                "status": status,
                "last_updated": datetime.utcnow()
            }
            
            if status == TweetStatus.POSTED and twitter_response:
                update_data["posted_time"] = datetime.utcnow()
                update_data["twitter_response"] = twitter_response
            
            if error:
                update_data["last_error"] = error
            
            if metadata:
                update_data["metadata"] = metadata
            
            result = await self.tweets.update_one(
                {"_id": ObjectId(tweet_id)},
                {
                    "$set": update_data,
                    "$inc": {"retry_count": 1} if error else {}
                }
            )
            return result.modified_count > 0
        except Exception as e:
            logger.error(f"Error updating tweet status: {e}")
            return False

    async def get_tweets_by_schedule(self, schedule_id: str) -> List[Tweet]:
        """Get all tweets for a schedule"""
        try:
            cursor = self.tweets.find({"schedule_id": schedule_id})
            return await cursor.to_list(length=None)
        except Exception as e:
            logger.error(f"Error fetching schedule tweets: {e}")
            return []

    async def get_pending_tweets(self, schedule_id: Optional[str] = None) -> List[Tweet]:
        """Get pending tweets, optionally filtered by schedule"""
        try:
            query = {"status": TweetStatus.PENDING}
            if schedule_id:
                query["schedule_id"] = schedule_id
            
            cursor = self.tweets.find(query)
            return await cursor.to_list(length=None)
        except Exception as e:
            logger.error(f"Error fetching pending tweets: {e}")
            return []

    async def get_scheduled_tweets_for_execution(self) -> List[Tweet]:
        """Get tweets that are ready to be posted"""
        try:
            current_time = datetime.utcnow()
            cursor = self.tweets.find({
                "status": TweetStatus.SCHEDULED.value,
                "$or": [
                    {"scheduled_time": {"$lte": current_time}},
                    {"metadata.scheduled_time": {"$lte": current_time.isoformat()}}
                ],
                "retry_count": {"$lt": 3}
            })
            return await cursor.to_list(length=None)
        except Exception as e:
            logger.error(f"Error fetching executable tweets: {e}")
            return []

    async def set_tool_operation_state(self, session_id: str, operation_data: Dict) -> bool:
        try:
            await self.tool_operations.update_one(
                {"session_id": session_id},
                {"$set": operation_data},
                upsert=True
            )
            return True
        except Exception as e:
            logger.error(f"Error setting tool operation state: {e}")
            return False

    async def get_tool_operation_state(self, session_id: str) -> Optional[ToolOperation]:
        try:
            return await self.tool_operations.find_one({"session_id": session_id})
        except Exception as e:
            logger.error(f"Error getting tool operation state: {e}")
            return None