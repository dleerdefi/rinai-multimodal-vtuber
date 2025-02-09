import requests
import json
import logging
from typing import Optional, Dict

logger = logging.getLogger(__name__)

class TwitterAgentClient:
    def __init__(self, base_url="http://localhost:3000"):
        self.base_url = base_url
        self.headers = {
            "Content-Type": "application/json",
            "Accept": "application/json"
        }

    async def send_tweet(self, content: str, params: Optional[Dict] = None) -> bool:
        """Send a tweet through our Twitter client server"""
        try:
            endpoint = f"{self.base_url}/tweets/send"
            payload = {
                "message": content,
                "accountId": params.get("account_id", "default")
            }
            
            # Add optional parameters if provided
            if params:
                if "media_files" in params:
                    payload["mediaFilePaths"] = params["media_files"]
                if "poll_options" in params:
                    payload["pollOptions"] = params["poll_options"]
                    payload["pollDurationMinutes"] = params.get("poll_duration", 60)

            logger.debug(f"Sending request to {endpoint} with payload: {payload}")
            
            response = requests.post(
                endpoint, 
                json=payload,
                headers=self.headers
            )
            
            logger.debug(f"Response status code: {response.status_code}")
            logger.debug(f"Response content: {response.text}")
            
            response.raise_for_status()
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
        try:
            endpoint = f"{self.base_url}/tweets/like"
            payload = {
                "tweetId": tweet_id,
                "accountId": account_id
            }
            response = requests.post(endpoint, json=payload, headers=self.headers)
            response.raise_for_status()
            return response.json()
        except Exception as e:
            logger.error(f"Like tweet failed: {str(e)}")
            return {"error": str(e), "success": False}

    def retweet(self, tweet_id, account_id="default"):
        """Retweet a tweet"""
        try:
            endpoint = f"{self.base_url}/tweets/retweet"
            payload = {
                "tweetId": tweet_id,
                "accountId": account_id
            }
            response = requests.post(endpoint, json=payload, headers=self.headers)
            response.raise_for_status()
            return response.json()
        except Exception as e:
            logger.error(f"Retweet failed: {str(e)}")
            return {"error": str(e), "success": False}

    def follow_user(self, username, account_id="default"):
        """Follow a user"""
        try:
            endpoint = f"{self.base_url}/tweets/follow"
            payload = {
                "username": username,
                "accountId": account_id
            }
            response = requests.post(endpoint, json=payload, headers=self.headers)
            response.raise_for_status()
            return response.json()
        except Exception as e:
            logger.error(f"Follow user failed: {str(e)}")
            return {"error": str(e), "success": False}