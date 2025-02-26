import requests
import json
import logging
from typing import Optional, Dict
from datetime import datetime, UTC

logger = logging.getLogger(__name__)

class TwitterAgentClient:
    def __init__(self, base_url="http://localhost:3000"):
        self.base_url = base_url
        self.headers = {
            "Content-Type": "application/json",
            "Accept": "application/json"
        }

    async def send_tweet(self, content: str, params: Optional[Dict] = None, test_mode: bool = False) -> Dict:
        """Send a tweet through our Twitter client server"""
        try:
            if test_mode:
                logger.info(f"TEST MODE: Would have posted tweet: {content}")
                return {
                    "success": True,
                    "id": f"mock_tweet_{datetime.now(UTC).timestamp()}",
                    "text": content
                }
            
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

    async def execute(self, operation: Dict) -> Dict:
        """Execute a tweet operation"""
        try:
            content = operation.get('content', {}).get('raw_content')
            params = operation.get('parameters', {}).get('custom_params', {})
            
            if not content:
                raise ValueError("No content provided for tweet")
            
            # Send tweet using the provided parameters
            response = await self.send_tweet(content=content, params=params)
            
            if response.get("success"):
                return {
                    "success": True,
                    "tweet_id": response.get('id'),
                    "timestamp": datetime.now(UTC).isoformat(),
                    "response": response
                }
            else:
                raise Exception(f"Tweet failed: {response.get('error')}")
            
        except Exception as e:
            logger.error(f"Failed to execute tweet: {e}")
            return {
                "success": False,
                "error": str(e)
            }