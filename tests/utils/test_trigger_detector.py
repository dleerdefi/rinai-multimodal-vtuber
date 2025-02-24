import pytest
from src.utils.trigger_detector import TriggerDetector
from src.db.enums import ToolType, ContentType

# Set up logging (optional, but helpful for debugging)
import logging
logger = logging.getLogger(__name__)

@pytest.fixture
def trigger_detector():
    """Create a fresh TriggerDetector instance for each test"""
    return TriggerDetector()

def test_twitter_basic_detection(trigger_detector):
    """Test basic Twitter trigger detection"""
    # Test positive cases
    assert trigger_detector.should_use_twitter("schedule some tweets")
    assert trigger_detector.should_use_twitter("post on twitter")
    assert trigger_detector.should_use_twitter("tweet about AI")
    
    # Test negative cases
    assert not trigger_detector.should_use_twitter("what's the weather")
    assert not trigger_detector.should_use_twitter("normal chat message")

def test_twitter_tool_type_detection(trigger_detector):
    """Test specific tool type detection for Twitter"""
    # Test Twitter detection
    assert trigger_detector.get_specific_tool_type("schedule tweets") == "twitter"
    assert trigger_detector.get_specific_tool_type("post on twitter") == "twitter"
    
    # Test non-Twitter cases
    assert trigger_detector.get_specific_tool_type("check crypto") != "twitter"
    assert trigger_detector.get_specific_tool_type("normal message") is None

def test_twitter_operation_types(trigger_detector):
    """Test detection of specific Twitter operations"""
    # Test scheduling operations
    assert trigger_detector.get_tool_operation_type("schedule tweets") == "schedule_tweets"
    assert trigger_detector.get_tool_operation_type("plan tweets for tomorrow") == "schedule_tweets"
    
    # Test immediate posting
    assert trigger_detector.get_tool_operation_type("tweet this now") == "send_tweet"
    assert trigger_detector.get_tool_operation_type("post immediately") == "send_tweet"

def test_twitter_complex_phrases(trigger_detector):
    """Test more complex Twitter-related phrases"""
    test_cases = [
        # Scheduling cases
        ("I want to schedule 5 tweets about AI", "schedule_tweets"),
        ("plan a series of tweets for next week", "schedule_tweets"),
        ("queue up some tweets about coding", "schedule_tweets"),
        
        # Immediate posting cases
        ("can you post this on twitter?", "send_tweet"),
        ("write a tweet thread about coding", "send_tweet"),
        ("tweet about the new features", "send_tweet"),
        
        # Reply cases
        ("reply to that tweet", "reply_tweet"),
        ("respond to @user's tweet", "reply_tweet"),
        
        # Mixed or ambiguous cases
        ("create a tweet about AI", "send_tweet"),
        ("post some thoughts on twitter", "send_tweet"),
        ("make an announcement on twitter", "send_tweet")
    ]
    
    for message, expected_operation in test_cases:
        result = trigger_detector.get_tool_operation_type(message)
        assert result == expected_operation, f"Failed on message: '{message}'\nExpected: {expected_operation}\nGot: {result}"

def test_edge_cases(trigger_detector):
    """Test edge cases and potential issues"""
    # Empty or invalid input
    assert trigger_detector.get_specific_tool_type("") is None
    assert trigger_detector.get_tool_operation_type("") is None
    
    # Mixed tool signals
    assert trigger_detector.get_specific_tool_type("tweet about the weather") == "twitter"
    
    # Case insensitivity
    assert trigger_detector.get_specific_tool_type("TWEET THIS") == "twitter"
    assert trigger_detector.get_specific_tool_type("Schedule TWEETS") == "twitter" 