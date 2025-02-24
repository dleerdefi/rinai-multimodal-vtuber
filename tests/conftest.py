import pytest
import os
import sys
from pathlib import Path
from datetime import datetime, UTC
import logging
import asyncio

# Add project root to Python path
current_dir = os.path.dirname(os.path.abspath(__file__))
project_root = os.path.dirname(current_dir)
sys.path.append(project_root)

# Load environment variables before imports
from dotenv import load_dotenv
load_dotenv(dotenv_path=Path(project_root) / '.env')

# Now import project modules
from src.db.mongo_manager import MongoManager

# Set up logging
logger = logging.getLogger(__name__)
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    force=True
)

@pytest.fixture(scope="session")
def event_loop():
    """Create an instance of the default event loop for each test case."""
    loop = asyncio.get_event_loop_policy().new_event_loop()
    yield loop
    loop.close()

@pytest.fixture(scope="session")
async def mongo_connection():
    """Initialize MongoDB connection for tests"""
    mongo_uri = os.getenv('MONGO_URI')
    if not mongo_uri:
        raise ValueError("MONGO_URI not found in environment variables")
    
    await MongoManager.initialize(mongo_uri)
    yield MongoManager.get_db()
    
    # Cleanup
    try:
        db = MongoManager.get_db()
        await db.tool_operations.delete_many({})
        await db.tool_items.delete_many({})
    finally:
        await MongoManager.close() 