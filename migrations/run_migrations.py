import sys
import asyncio
import logging
import os
from pathlib import Path
from dotenv import load_dotenv

# Add project root to Python path
project_root = str(Path(__file__).parent.parent)
sys.path.append(project_root)

# Now we can import from src
from src.db.db_schema import RinDB
from src.db.mongo_manager import MongoManager
from migrations.align_tweet_schema import (
    migrate_to_tool_items,
    migrate_tweet_schedules_to_scheduled_operations,
    cleanup_old_collections
)
from src.utils.logging_config import setup_logging

# Set up logging
logger = logging.getLogger(__name__)

async def run_migrations(mongo_uri: str):
    """Run all migrations in order"""
    try:
        # Initialize MongoDB connection
        await MongoManager.initialize(mongo_uri)
        db = MongoManager.get_db()
        
        logger.info("Starting migrations...")
        
        # Run migrations in order
        await migrate_to_tool_items(db)
        await migrate_tweet_schedules_to_scheduled_operations(db)
        await cleanup_old_collections(db)
        
        logger.info("Migrations completed successfully")
        
    except Exception as e:
        logger.error(f"Migration failed: {e}")
        raise
    finally:
        await MongoManager.close()

if __name__ == "__main__":
    # Setup logging
    setup_logging()
    
    # Load environment variables
    load_dotenv()
    
    # Get MongoDB URI from environment
    mongo_uri = os.getenv("MONGO_URI", "mongodb://localhost:27017")
    
    try:
        asyncio.run(run_migrations(mongo_uri))
    except KeyboardInterrupt:
        logger.info("Migration interrupted by user")
    except Exception as e:
        logger.error(f"Fatal error during migration: {e}")
        raise 