from motor.motor_asyncio import AsyncIOMotorClient
from typing import Optional
import logging
from .db_schema import RinDB

logger = logging.getLogger(__name__)

class MongoManager:
    _instance: Optional[AsyncIOMotorClient] = None
    _db: Optional[RinDB] = None

    @classmethod
    async def initialize(cls, mongo_uri: str, db_name: str = 'rin_dev_db'):
        """Initialize MongoDB connection"""
        try:
            if cls._instance is None:
                logger.info(f"Initializing MongoDB connection to database: {db_name}")
                cls._instance = AsyncIOMotorClient(mongo_uri)
                # Initialize RinDB with the client
                cls._db = RinDB(cls._instance)
                # Initialize database and collections
                await cls._db.initialize()
                logger.info("MongoDB connection and collections verified successfully")
            return cls._instance
        except Exception as e:
            logger.error(f"Failed to initialize MongoDB: {e}")
            raise

    @classmethod
    async def is_connected(cls) -> bool:
        """Check if MongoDB is connected"""
        try:
            if cls._db:
                return await cls._db.is_initialized()
            return False
        except Exception:
            return False

    @classmethod
    def get_db(cls) -> RinDB:
        """Get RinDB instance"""
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