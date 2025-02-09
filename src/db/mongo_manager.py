from motor.motor_asyncio import AsyncIOMotorClient
from typing import Optional
import logging
import asyncio

logger = logging.getLogger(__name__)

class MongoManager:
    _instance: Optional[AsyncIOMotorClient] = None
    _db = None

    @classmethod
    async def initialize(cls, mongo_uri: str, max_retries: int = 3):
        """Initialize MongoDB connection with retries"""
        if cls._instance is not None:
            return cls._instance

        retry_count = 0
        while retry_count < max_retries:
            try:
                logger.info(f"Initializing MongoDB connection (attempt {retry_count + 1})")
                cls._instance = AsyncIOMotorClient(mongo_uri)
                
                # Test connection
                await cls._instance.admin.command('ping')
                
                # Import here to avoid circular import
                from src.db.db_schema import RinDB
                
                # Initialize RinDB with the client
                cls._db = RinDB(cls._instance)
                await cls._db.initialize()
                
                logger.info("MongoDB connection and collections verified")
                return cls._instance
                
            except Exception as e:
                retry_count += 1
                if retry_count >= max_retries:
                    logger.error(f"Failed to initialize MongoDB after {max_retries} attempts: {e}")
                    raise
                logger.warning(f"MongoDB connection attempt {retry_count} failed: {e}. Retrying...")
                await asyncio.sleep(1)

    @classmethod
    def get_db(cls):
        """Get database instance"""
        if cls._db is None:
            raise RuntimeError("MongoDB not initialized. Call initialize() first")
        return cls._db

    @classmethod
    async def close(cls):
        """Close MongoDB connection"""
        if cls._instance is not None:
            cls._instance.close()
            cls._instance = None
            cls._db = None
            logger.info("MongoDB connection closed")

    @classmethod
    def is_initialized(cls) -> bool:
        """Check if MongoDB is initialized"""
        return cls._instance is not None and cls._db is not None 