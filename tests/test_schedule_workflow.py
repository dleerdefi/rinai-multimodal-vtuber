import pytest
import asyncio
import logging
from datetime import datetime, timedelta, UTC
import os
from dotenv import load_dotenv
from src.services.schedule_service import ScheduleService
from src.managers.tool_state_manager import ToolStateManager, ToolOperationState
from src.db.db_schema import RinDB, TweetStatus, ValidatedTweet
from motor.motor_asyncio import AsyncIOMotorClient
from src.db.mongo_manager import MongoManager
from bson.objectid import ObjectId
from src.orchestrator.orchestrator import Orchestrator
from types import SimpleNamespace

# Load environment variables
load_dotenv()

# Setup logging
logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger(__name__)

@pytest.fixture
async def setup_test_env():
    """Setup test environment"""
    try:
        mongo_uri = os.getenv('MONGO_URI')
        if not mongo_uri:
            raise ValueError("MONGO_URI not found in environment variables")
            
        logger.info(f"Connecting to MongoDB at {mongo_uri.split('@')[-1]}")
        
        # Initialize MongoDB through MongoManager
        await MongoManager.initialize(mongo_uri)
        db = MongoManager.get_db()
        logger.info("MongoDB initialized through MongoManager")
        
        # Initialize collections if they don't exist
        collections = await db.db.list_collection_names()
        logger.info(f"Existing collections: {collections}")
        
        # Create collections if they don't exist
        for collection in ['rin.tweets', 'rin.tool_operations', 'rin.tweet_schedules']:
            if collection not in collections:
                try:
                    await db.db.create_collection(collection)
                    logger.info(f"Created collection: {collection}")
                except Exception as e:
                    logger.debug(f"Collection {collection} might already exist: {e}")
        
        # Initialize services
        schedule_service = ScheduleService(mongo_uri)
        tool_state_manager = ToolStateManager(db)
        
        # Initialize orchestrator with dependencies
        deps = SimpleNamespace(
            conversation_id=f"test-convo-{datetime.now(UTC).timestamp()}",
            db=db
        )
        orchestrator = Orchestrator(deps)
        
        return {
            'db': db,
            'schedule_service': schedule_service,
            'tool_state_manager': tool_state_manager,
            'orchestrator': orchestrator
        }
    except Exception as e:
        logger.error(f"Error in setup_test_env: {e}")
        raise

@pytest.mark.asyncio
async def test_tweet_scheduling_workflow(setup_test_env):
    """Test the complete tweet scheduling workflow"""
    logger.info("Starting tweet scheduling workflow test")
    env = await setup_test_env
    session_id = f"test-session-{datetime.now(UTC).timestamp()}"
    
    try:
        # Step 1: Start scheduling operation
        initial_data = {
            "topic": "Python Programming",
            "tweet_count": 3,
            "schedule_info": {
                "start_time": datetime.now(UTC) + timedelta(minutes=5),
                "interval_minutes": 60
            }
        }
        
        logger.info(f"Starting operation with session_id: {session_id}")
        success = await env['tool_state_manager'].start_operation(
            session_id=session_id,
            operation_type="schedule_tweets",
            initial_data=initial_data
        )
        assert success, "Failed to start operation"
        
        # Step 2: Create scheduled tweets
        logger.info("Creating test tweets")
        tweets = [
            ValidatedTweet(
                content=f"Python tip #{i}: {content}",
                metadata={
                    "estimated_engagement": "medium",
                    "generated_at": datetime.now(UTC).isoformat()
                },
                twitter_api_params={
                    "message": f"Python tip #{i}: {content}",
                    "account_id": "default",
                    "media_files": None,
                    "poll_options": None,
                    "poll_duration": None
                }
            )
            for i, content in enumerate([
                "Use list comprehensions for cleaner code!",
                "Virtual environments keep dependencies clean",
                "Type hints improve code readability"
            ], 1)
        ]
        
        tweet_ids = []
        schedule_time = initial_data["schedule_info"]["start_time"]
        
        # Direct MongoDB insertion for testing
        for tweet in tweets:
            result = await env['db'].db['rin.tweets'].insert_one({
                "content": tweet["content"],
                "status": TweetStatus.PENDING.value,
                "created_at": datetime.now(UTC),
                "scheduled_time": schedule_time,
                "posted_time": None,
                "metadata": tweet["metadata"],
                "twitter_api_params": tweet["twitter_api_params"],
                "twitter_response": None,
                "retry_count": 0,
                "last_error": None,
                "schedule_id": session_id,
                "session_id": session_id
            })
            tweet_id = str(result.inserted_id)
            logger.info(f"Created tweet with ID: {tweet_id}")
            tweet_ids.append(tweet_id)
            schedule_time += timedelta(minutes=initial_data["schedule_info"]["interval_minutes"])
        
        # Verify tweets were created
        for tweet_id in tweet_ids:
            tweet = await env['db'].db['rin.tweets'].find_one({"_id": ObjectId(tweet_id)})
            logger.info(f"Verifying tweet {tweet_id}: {tweet}")
            assert tweet is not None, f"Tweet {tweet_id} was not created"
        
        # Update tweets to SCHEDULED status
        for tweet_id in tweet_ids:
            await env['db'].db['rin.tweets'].update_one(
                {"_id": ObjectId(tweet_id)},
                {"$set": {
                    "status": TweetStatus.SCHEDULED.value,
                    "scheduled_time": schedule_time
                }}
            )
            logger.info(f"Updated tweet {tweet_id} to SCHEDULED status")
        
        # After creating tweets, add this code to create the tweet schedule
        logger.info("Creating tweet schedule")
        schedule_doc = {
            "session_id": session_id,
            "topic": initial_data["topic"],
            "total_tweets_requested": initial_data["tweet_count"],
            "schedule_info": initial_data["schedule_info"],
            "approved_tweets": tweet_ids,
            "pending_tweets": [],
            "status": "scheduled",  # or "collecting_approval" based on your workflow
            "created_at": datetime.now(UTC),
            "last_updated": datetime.now(UTC),
            "last_error": None
        }

        try:
            result = await env['db'].db['rin.tweet_schedules'].insert_one(schedule_doc)
            schedule_id = str(result.inserted_id)
            logger.info(f"Created tweet schedule with ID: {schedule_id}")
            
            # Verify schedule was created
            schedule = await env['db'].db['rin.tweet_schedules'].find_one({"_id": ObjectId(schedule_id)})
            logger.info(f"Verifying schedule: {schedule}")
            assert schedule is not None, "Tweet schedule was not created"
        except Exception as e:
            logger.error(f"Error creating tweet schedule: {e}")
            raise
        
        # Start schedule service
        await env['schedule_service'].start()
        
        # Step 4: Update operation state
        await env['tool_state_manager'].update_operation(
            session_id=session_id,
            state=ToolOperationState.EXECUTING,
            step="scheduling",
            data={"tweet_ids": tweet_ids}
        )
        
        # Step 5: Wait for processing
        await asyncio.sleep(2)
        
        # Step 6: Verify tweets
        for tweet_id in tweet_ids:
            tweet = await env['db'].db['rin.tweets'].find_one({"_id": ObjectId(tweet_id)})
            assert tweet is not None
            assert tweet['status'] in [TweetStatus.SCHEDULED.value, TweetStatus.POSTED.value]
        
        # Step 7: Complete operation
        success = await env['tool_state_manager'].end_operation(session_id, success=True)
        assert success, "Failed to end operation"
        
        # Verify final state
        operation = await env['db'].get_tool_operation_state(session_id)
        assert operation['state'] == ToolOperationState.COMPLETED.value
        
    finally:
        # Only stop the service and close connections, don't drop collections
        await env['schedule_service'].stop()
        await asyncio.sleep(1)
        await MongoManager.close()
        logger.info("Test cleanup completed (connections closed)")

@pytest.mark.asyncio
async def test_schedule_service_error_handling(setup_test_env):
    """Test schedule service error handling"""
    logger.info("Starting error handling test")
    env = await setup_test_env
    session_id = f"test-session-{datetime.now(UTC).timestamp()}"
    
    try:
        # Create a tweet with invalid content to trigger error
        result = await env['db'].db['rin.tweets'].insert_one({
            "content": "",  # Invalid empty content
            "status": TweetStatus.SCHEDULED.value,
            "created_at": datetime.now(UTC),
            "scheduled_time": datetime.now(UTC) - timedelta(minutes=1),
            "posted_time": None,
            "metadata": {},
            "twitter_api_params": {
                "message": "",
                "account_id": "default"
            },
            "twitter_response": None,
            "retry_count": 0,
            "last_error": None,
            "schedule_id": session_id,
            "session_id": session_id
        })
        tweet_id = str(result.inserted_id)
        logger.info(f"Created test tweet with ID: {tweet_id}")
        
        # Verify tweet was created
        initial_tweet = await env['db'].db['rin.tweets'].find_one({"_id": ObjectId(tweet_id)})
        logger.info(f"Initial tweet state: {initial_tweet}")
        assert initial_tweet is not None, "Tweet was not created"
        
        # Start service and wait for processing
        await env['schedule_service'].start()
        
        # Wait and check status multiple times
        for i in range(5):  # Try up to 5 times
            await asyncio.sleep(1)  # Check every second
            tweet = await env['db'].db['rin.tweets'].find_one({"_id": ObjectId(tweet_id)})
            logger.info(f"Tweet status check {i+1}: {tweet['status']}")
            if tweet['status'] == TweetStatus.FAILED.value:
                break
        
        # Final verification with detailed logging
        final_tweet = await env['db'].db['rin.tweets'].find_one({"_id": ObjectId(tweet_id)})
        logger.info(f"Final tweet state: {final_tweet}")
        assert final_tweet is not None, "Tweet not found"
        assert final_tweet['status'] == TweetStatus.FAILED.value, f"Expected status FAILED, got {final_tweet['status']}"
        assert final_tweet['retry_count'] > 0, f"Retry count not incremented: {final_tweet['retry_count']}"
        assert final_tweet['last_error'] is not None, f"Error message not set: {final_tweet['last_error']}"
        
    finally:
        # Only stop the service and close connections, don't drop collections
        await env['schedule_service'].stop()
        await asyncio.sleep(1)
        await MongoManager.close()
        logger.info("Test cleanup completed (connections closed)")

@pytest.mark.asyncio
async def test_tweet_scheduling_with_orchestrator(setup_test_env):
    """Test tweet scheduling with orchestrator integration"""
    logger.info("Starting orchestrator integration test")
    env = await setup_test_env
    session_id = f"test-session-{datetime.now(UTC).timestamp()}"
    
    try:
        # Step 1: Generate tweets via orchestrator
        schedule_info = {
            "start_time": datetime.now(UTC) + timedelta(minutes=5),
            "interval_minutes": 60,
            "topic": "Python Programming",
            "tweet_count": 3
        }
        
        tweets = await env['orchestrator']._generate_tweet_series(
            topic=schedule_info["topic"],
            count=schedule_info["tweet_count"],
            tone="professional"
        )
        assert len(tweets) == schedule_info["tweet_count"], "Wrong number of tweets generated"
        
        # Step 2: Store tweets and create schedule
        schedule_id = await env['orchestrator']._store_approved_tweets(tweets, schedule_info)
        assert schedule_id is not None, "Failed to create schedule"
        
        # Verify schedule was created
        schedule = await env['db'].db['rin.tweet_schedules'].find_one({"_id": ObjectId(schedule_id)})
        logger.info(f"Created schedule: {schedule}")
        assert schedule is not None, "Schedule not found"
        assert schedule["status"] == "collecting_approval"
        
        # Step 3: Activate schedule
        success = await env['orchestrator']._activate_tweet_schedule(schedule_id, schedule_info)
        assert success, "Failed to activate schedule"
        
        # Verify tweets were scheduled
        tweets = await env['db'].db['rin.tweets'].find({
            "schedule_id": schedule_id
        }).to_list(None)
        
        for tweet in tweets:
            assert tweet["status"] == TweetStatus.SCHEDULED.value
            assert tweet["scheduled_time"] is not None
        
        # Step 4: Start schedule service
        await env['schedule_service'].start()
        
        # Wait for processing
        await asyncio.sleep(2)
        
        # Verify final states
        schedule = await env['db'].db['rin.tweet_schedules'].find_one({"_id": ObjectId(schedule_id)})
        assert schedule["status"] == "scheduled"
        
        tweets = await env['db'].db['rin.tweets'].find({
            "schedule_id": schedule_id
        }).to_list(None)
        
        for tweet in tweets:
            assert tweet["status"] in [TweetStatus.SCHEDULED.value, TweetStatus.POSTED.value]
            
    finally:
        await env['schedule_service'].stop()
        await asyncio.sleep(1)
        await MongoManager.close()
        logger.info("Test cleanup completed")

if __name__ == "__main__":
    pytest.main(["-v", "test_schedule_workflow.py"]) 