import requests
import json
import logging

logger = logging.getLogger(__name__)

class TwitterAgentClient:
    def __init__(self, base_url="http://localhost:3000"):
        self.base_url = base_url
        self.headers = {
            "Content-Type": "application/json",
            "Accept": "application/json"
        }

    def send_tweet(self, message, account_id="default", media_files=None, poll_options=None, poll_duration=60):
        """Send a tweet with optional media and poll"""
        try:
            endpoint = f"{self.base_url}/tweets/send"
            payload = {
                "message": message,
                "accountId": account_id
            }
            
            # Only add optional fields if they have values
            if media_files:
                payload["mediaFilePaths"] = media_files
            if poll_options:
                payload["pollOptions"] = poll_options
                payload["pollDurationMinutes"] = poll_duration

            logger.debug(f"Sending request to {endpoint} with payload: {payload}")
            
            response = requests.post(
                endpoint, 
                json=payload,
                headers=self.headers
            )
            
            # Log response details for debugging
            logger.debug(f"Response status code: {response.status_code}")
            logger.debug(f"Response headers: {response.headers}")
            logger.debug(f"Response content: {response.text}")
            
            response.raise_for_status()  # Raise exception for bad status codes
            
            return response.json()

        except requests.exceptions.RequestException as e:
            logger.error(f"Request failed: {str(e)}")
            return {"error": str(e), "success": False}
        except json.JSONDecodeError as e:
            logger.error(f"Failed to parse response: {str(e)}")
            return {"error": "Invalid JSON response", "success": False}
        except Exception as e:
            logger.error(f"Unexpected error: {str(e)}")
            return {"error": str(e), "success": False}

    def like_tweet(self, tweet_id, account_id="default"):
        """Like a tweet"""
        endpoint = f"{self.base_url}/tweets/like"
        payload = {
            "tweetId": tweet_id,
            "accountId": account_id
        }
        response = requests.post(endpoint, json=payload)
        return response.json()

    def retweet(self, tweet_id, account_id="default"):
        """Retweet a tweet"""
        endpoint = f"{self.base_url}/tweets/retweet"
        payload = {
            "tweetId": tweet_id,
            "accountId": account_id
        }
        response = requests.post(endpoint, json=payload)
        return response.json()

    def follow_user(self, username, account_id="default"):
        """Follow a user"""
        endpoint = f"{self.base_url}/tweets/follow"
        payload = {
            "username": username,
            "accountId": account_id
        }
        response = requests.post(endpoint, json=payload)
        return response.json()

# Initialize the client
client = TwitterAgentClient()

# Examples of using the client
try:
    # Send a simple tweet
    result = client.send_tweet("Hello from Python!")
    print("Tweet sent:", result)

    # Like a tweet
    result = client.like_tweet("1234567890")
    print("Like result:", result)

    # Retweet
    result = client.retweet("1234567890")
    print("Retweet result:", result)

    # Follow a user
    result = client.follow_user("elonmusk")
    print("Follow result:", result)

    # Send a tweet with a poll
    result = client.send_tweet(
        message="What's your favorite programming language?",
        poll_options=["Python", "JavaScript", "TypeScript", "Other"],
        poll_duration=1440  # 24 hours
    )
    print("Poll tweet sent:", result)

except requests.exceptions.RequestException as e:
    print(f"Error communicating with server: {e}")
except json.JSONDecodeError as e:
    print(f"Error parsing server response: {e}")