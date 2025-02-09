import pytest
import asyncio
from datetime import datetime, timedelta
from src.utils.trigger_detector import TriggerDetector
from src.tools.orchestrator import Orchestrator
from src.agents.rin.agent import RinAgent
from src.services.schedule_service import ScheduleService
from src.managers.tool_state_manager import ToolStateManager, ToolOperationState
from src.db.db_schema import RinDB, Tweet, TweetStatus
from motor.motor_asyncio import AsyncIOMotorClient

@pytest.fixture
async def setup_test_env():
    """Setup test environment with all components"""
    mongo_uri = "mongodb://localhost:27017"
    db_client = AsyncIOMotorClient(mongo_uri)
    db = RinDB(db_client)
    await db.initialize()
    
    # Initialize components
    agent = RinAgent(mongo_uri)
    await agent.initialize()
    
    schedule_service = ScheduleService(mongo_uri)
    await schedule_service.start()
    
    yield {
        'db': db,
        'agent': agent,
        'schedule_service': schedule_service
    }
    
    # Cleanup
    await schedule_service.stop()
    await db_client.drop_database('rin_multimodal')
    await db_client.close()

@pytest.mark.asyncio
async def test_tweet_scheduling_workflow(setup_test_env):
    """Test complete tweet scheduling workflow"""
    env = await setup_test_env
    session_id = "test-session-123"
    
    # Step 1: Initial user request
    message = "Please schedule 3 tweets about Python programming for next week"
    response = await env['agent'].get_response(session_id, message)
    
    # Verify tool operation started
    operation = await env['db'].get_tool_operation_state(session_id)
    assert operation is not None
    assert operation['state'] == ToolOperationState.COLLECTING.value
    assert operation['operation_type'] == "schedule_tweets"
    
    # Step 2: Approve generated tweets
    approval_msg = "Yes, these tweets look good. Approve all of them."
    response = await env['agent'].get_response(session_id, approval_msg)
    
    # Verify tweets were created
    schedule = await env['db'].get_session_tweet_schedule(session_id)
    assert schedule is not None
    assert len(schedule['approved_tweets']) == 3
    
    # Step 3: Verify tweets are scheduled
    tweets = await env['db'].get_tweets_by_schedule(schedule['_id'])
    assert len(tweets) == 3
    for tweet in tweets:
        assert tweet['status'] == TweetStatus.SCHEDULED
        assert tweet['scheduled_time'] is not None
    
    # Step 4: Verify operation completed
    operation = await env['db'].get_tool_operation_state(session_id)
    assert operation['state'] == ToolOperationState.COMPLETED.value

@pytest.mark.asyncio
async def test_immediate_tweet_workflow(setup_test_env):
    """Test immediate tweet workflow"""
    env = await setup_test_env
    session_id = "test-session-456"
    
    # Step 1: Send immediate tweet request
    message = "Tweet 'Hello World from Python!' right now"
    response = await env['agent'].get_response(session_id, message)
    
    # Verify operation type
    operation = await env['db'].get_tool_operation_state(session_id)
    assert operation is not None
    assert operation['operation_type'] == "send_tweet"
    
    # Step 2: Verify tweet created
    tweets = await env['db'].get_tweets_by_schedule(operation['data'].get('schedule_id'))
    assert len(tweets) == 1
    assert tweets[0]['content'] == 'Hello World from Python!'
    assert tweets[0]['status'] == TweetStatus.PENDING

@pytest.mark.asyncio
async def test_schedule_service_integration(setup_test_env):
    """Test schedule service processes tweets correctly"""
    env = await setup_test_env
    session_id = "test-session-789"
    
    # Create a tweet scheduled for immediate posting
    schedule_id = "test-schedule-123"
    tweet_id = await env['db'].create_tweet(
        content="Test scheduled tweet",
        schedule_id=schedule_id,
        session_id=session_id,
        scheduled_time=datetime.utcnow()
    )
    
    # Wait for schedule service to process
    await asyncio.sleep(2)
    
    # Verify tweet was processed
    tweet = await env['db'].tweets.find_one({"_id": tweet_id})
    assert tweet['status'] in [TweetStatus.POSTED, TweetStatus.FAILED]

if __name__ == "__main__":
    pytest.main(["-v", "test_tool_integration.py"]) 