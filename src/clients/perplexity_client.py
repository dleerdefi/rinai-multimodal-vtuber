import aiohttp
import logging
import asyncio
from typing import Dict
from datetime import datetime

logger = logging.getLogger(__name__)

class PerplexityClient:
    def __init__(self, api_key: str):
        self.api_key = api_key
        self.base_url = "https://api.perplexity.ai"
        self.session = None
    
    async def initialize(self):
        """Initialize aiohttp session"""
        if not self.session or self.session.closed:
            self.session = aiohttp.ClientSession(
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json"
                },
                timeout=aiohttp.ClientTimeout(total=30)
            )
    
    async def close(self):
        """Close aiohttp session"""
        if self.session and not self.session.closed:
            await self.session.close()
            self.session = None
    
    async def search(self, 
        query: str, 
        timeout: float = 30.0,
        max_tokens: int = 300,
        retries: int = 3
    ) -> Dict:
        """
        Perform a search query with timeout and retries
        Returns: Dict containing search results or error
        """
        for attempt in range(retries):
            try:
                if not self.session or self.session.closed:
                    await self.initialize()
                
                payload = {
                    "model": "sonar-reasoning",
                    "messages": [{"role": "user", "content": query}],
                    "max_tokens": max_tokens
                }
                
                async with self.session.post(
                    f"{self.base_url}/chat/completions",
                    json=payload,
                    timeout=timeout,
                    raise_for_status=True
                ) as response:
                    if response.status == 200:
                        result = await response.json()
                        return {
                            "status": "success",
                            "data": result.get("choices", [{}])[0].get("message", {}).get("content", ""),
                            "timestamp": datetime.utcnow().isoformat()
                        }
                    else:
                        error_text = await response.text()
                        logger.error(f"Perplexity API error: {error_text}")
                        # If we get a 503, wait before retrying
                        if response.status == 503 and attempt < retries - 1:
                            await asyncio.sleep(1 * (attempt + 1))
                            continue
                        return {
                            "status": "error",
                            "error": f"API returned {response.status}: {error_text}",
                            "timestamp": datetime.utcnow().isoformat()
                        }
                        
            except asyncio.TimeoutError:
                logger.error(f"Perplexity API timeout after {timeout}s")
                if attempt < retries - 1:
                    await asyncio.sleep(1)
                    continue
                return {
                    "status": "timeout",
                    "error": "Request timed out",
                    "timestamp": datetime.utcnow().isoformat()
                }
            except Exception as e:
                logger.error(f"Perplexity API error: {str(e)}")
                if attempt < retries - 1:
                    await asyncio.sleep(1)
                    continue
                return {
                    "status": "error",
                    "error": str(e),
                    "timestamp": datetime.utcnow().isoformat()
                } 