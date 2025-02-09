from typing import TypedDict, List, Optional
from datetime import datetime
import logging
import uuid
from bson.objectid import ObjectId

logger = logging.getLogger(__name__)

class Message(TypedDict):
    role: str  # "user" or "assistant"
    content: str
    timestamp: datetime

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

class RinDB:
    def __init__(self, client):
        self.client = client
        self.db = client['rin_dev_db']  # Use same database as Node.js
        self.messages = self.db['rin.messages']
        self.context_configs = self.db['rin.context_configs']
        logger.info(f"Connected to database: {self.db.name}")

    async def initialize(self):
        """Initialize database and collections if they don't exist"""
        try:
            # Create collections if they don't exist
            collections = await self.db.list_collection_names()
            
            if 'rin.messages' not in collections:
                await self.db.create_collection('rin.messages')
                logger.info("Created rin.messages collection")
            
            if 'rin.context_configs' not in collections:
                await self.db.create_collection('rin.context_configs')
                logger.info("Created rin.context_configs collection")
            
            # Setup indexes
            await self._setup_indexes()
            
            logger.info("Database initialization complete")
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
                for col in ['rin.messages', 'rin.context_configs']
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
            logger.info("Successfully created database indexes")
        except Exception as e:
            logger.error(f"Error setting up indexes: {str(e)}")
            raise

    async def add_message(self, session_id: str, role: str, content: str, 
                         interaction_type: str = 'chat', metadata: Optional[dict] = None):
        """Add a message to the database with optional metadata"""
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