import pytest
import asyncio
from src.clients.twitter_client import TwitterAgentClient
import logging

logger = logging.getLogger(__name__)

@pytest.mark.asyncio
async def test_twitter_client_send_tweet():
    """Test sending a tweet through the Twitter Agent server"""
    try:
        # Initialize client
        client = TwitterAgentClient(base_url="http://localhost:3000")
        
        # Test simple tweet
        test_message = "Hello I am tweeting from RinAI Multimodal Agent Test! 🐍"
        logger.info(f"Attempting to send tweet: {test_message}")
        
        result = client.send_tweet(
            message=test_message,
            account_id="default"
        )
        
        logger.info(f"Server response: {result}")
        assert isinstance(result, dict)
        assert result.get("success") is True
        if "result" in result:
            logger.info(f"Tweet details: {result['result']}")
        
        return result

    except Exception as e:
        logger.error(f"Error testing Twitter client: {e}")
        raise

@pytest.mark.asyncio
async def test_twitter_client_like_tweet():
    """Test liking a tweet"""
    try:
        client = TwitterAgentClient(base_url="http://localhost:3000")
        
        # First send a tweet to get a tweet ID
        tweet_result = client.send_tweet("Test tweet for liking!")
        assert tweet_result.get("success") is True
        
        tweet_id = tweet_result.get("result", {}).get("id")
        assert tweet_id, "No tweet ID returned"
        
        # Like the tweet
        logger.info(f"Attempting to like tweet: {tweet_id}")
        like_result = client.like_tweet(tweet_id)
        
        logger.info(f"Like response: {like_result}")
        assert isinstance(like_result, dict)
        assert like_result.get("success") is True
        
        return like_result

    except Exception as e:
        logger.error(f"Error testing like operation: {e}")
        raise

@pytest.mark.asyncio
async def test_twitter_client_retweet():
    """Test retweeting a tweet"""
    try:
        client = TwitterAgentClient(base_url="http://localhost:3000")
        
        # First send a tweet to get a tweet ID
        tweet_result = client.send_tweet("Test tweet for retweeting! RinAI Multimodal Agent Test")
        assert tweet_result.get("success") is True
        
        tweet_id = tweet_result.get("result", {}).get("id")
        assert tweet_id, "No tweet ID returned"
        
        # Retweet
        logger.info(f"Attempting to retweet: {tweet_id}")
        retweet_result = client.retweet(tweet_id)
        
        logger.info(f"Retweet response: {retweet_result}")
        assert isinstance(retweet_result, dict)
        assert retweet_result.get("success") is True
        
        return retweet_result

    except Exception as e:
        logger.error(f"Error testing retweet operation: {e}")
        raise

@pytest.mark.asyncio
async def test_twitter_client_follow():
    """Test following a user"""
    try:
        client = TwitterAgentClient(base_url="http://localhost:3000")
        
        test_username = "dleer_defi"  # Replace with a valid test username
        logger.info(f"Attempting to follow user: {test_username}")
        
        follow_result = client.follow_user(test_username)
        
        logger.info(f"Follow response: {follow_result}")
        assert isinstance(follow_result, dict)
        assert follow_result.get("success") is True
        
        return follow_result

    except Exception as e:
        logger.error(f"Error testing follow operation: {e}")
        raise

@pytest.mark.asyncio
async def test_twitter_client_with_poll():
    """Test sending a tweet with a poll"""
    try:
        client = TwitterAgentClient(base_url="http://localhost:3000")
        
        poll_message = "What's your favorite programming language? #coding"
        poll_options = ["Python", "JavaScript", "TypeScript", "Other"]
        
        logger.info(f"Attempting to send poll tweet: {poll_message}")
        logger.info(f"Poll options: {poll_options}")
        
        result = client.send_tweet(
            message=poll_message,
            account_id="default",
            poll_options=poll_options,
            poll_duration=1440  # 24 hours
        )
        
        logger.info(f"Poll tweet response: {result}")
        assert isinstance(result, dict)
        assert result.get("success") is True
        if "result" in result and result["result"]:
            assert "poll" in result["result"], "Poll data not found in response"
        
        return result

    except Exception as e:
        logger.error(f"Error testing poll tweet: {e}")
        raise

if __name__ == "__main__":
    # Run the test directly
    async def run_tests():
        logger.info("Starting Twitter client tests...")
        
        # Configure logging
        logging.basicConfig(
            level=logging.INFO,
            format='%(asctime)s - %(levelname)s - %(message)s'
        )
        
        # Run all tests in sequence
        tests = [
            test_twitter_client_send_tweet(),
            test_twitter_client_like_tweet(),
            test_twitter_client_retweet(),
            test_twitter_client_follow(),
            test_twitter_client_with_poll()
        ]
        
        # Wait between tests to avoid rate limiting
        for test in tests:
            await test
            await asyncio.sleep(2)
        
        logger.info("All tests completed successfully")

    # Run tests
    asyncio.run(run_tests()) 