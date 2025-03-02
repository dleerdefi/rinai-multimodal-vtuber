import logging
import requests
from typing import Optional, Dict
import aiohttp
import asyncio
from functools import lru_cache, wraps

logger = logging.getLogger(__name__)

def sync_lru_cache(maxsize=128, typed=False):
    """A synchronous LRU cache decorator that works with async functions.
    It caches the results, not the coroutines themselves."""
    def decorator(fn):
        cached_func = lru_cache(maxsize=maxsize, typed=typed)(lambda *args, **kwargs: None)
        
        @wraps(fn)
        async def wrapper(*args, **kwargs):
            cache_key = args + tuple(sorted(kwargs.items()))
            cached_result = cached_func(*cache_key)
            if cached_result is not None:
                return cached_result
                
            # Call the original function and cache its result
            result = await fn(*args, **kwargs)
            cached_func.__wrapped__(*cache_key, result)
            return result
        return wrapper
    return decorator

class CoinGeckoClient:
    def __init__(self, api_key: str):
        """Initialize CoinGecko client with API key"""
        self.api_key = api_key
        self.base_url = "https://api.coingecko.com/api/v3"
        self.session = None  # Will be initialized when needed
        self.headers = {
            "accept": "application/json",
            "x-cg-demo-api-key": api_key
        }
        
        # Common token mappings
        self.SYMBOL_TO_COINGECKO = {
            "BTC": "bitcoin",
            "ETH": "ethereum",
            "SOL": "solana",
            "USDT": "tether",
            "USDC": "usd-coin",
            "BNB": "binancecoin",
            "XRP": "ripple",
            "ADA": "cardano",
            "DOGE": "dogecoin",
            "MATIC": "polygon",
            "DOT": "polkadot",
            "LINK": "chainlink",
            "AVAX": "avalanche-2",
            "UNI": "uniswap",
            "AAVE": "aave",
        }

        # Cache for token IDs
        self._id_cache = {}

    async def __aenter__(self):
        self.session = aiohttp.ClientSession()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        if self.session:
            await self.session.close()
            
    async def get_token_price(self, token_id: str) -> Optional[Dict]:
        """Get token price using free API endpoint
        Docs: https://docs.coingecko.com/reference/simple-price-1
        """
        if not self.session:
            self.session = aiohttp.ClientSession()
            
        try:
            # Using the simple price endpoint which has higher rate limits
            url = f"{self.base_url}/simple/price"
            params = {
                "ids": token_id,
                "vs_currencies": "usd",
                "include_24hr_change": "true",
                "x_cg_demo_api_key": self.api_key
            }
            
            async with self.session.get(url, params=params) as response:
                if response.status == 429:
                    logger.warning("Rate limit hit, waiting before retry...")
                    await asyncio.sleep(60)  # Wait 60s on rate limit
                    return None
                        
                response.raise_for_status()
                data = await response.json()
                
                if not data or token_id not in data:
                    logger.warning(f"No data found for token: {token_id}")
                    return None
                
                token_data = data[token_id]
                return {
                    "symbol": token_id,
                    "price_usd": token_data.get("usd"),
                    "price_change_24h": token_data.get("usd_24h_change")
                }
                
        except aiohttp.ClientError as e:
            logger.error(f"CoinGecko API connection error: {e}")
            # Ensure session is closed on connection error
            if self.session:
                await self.session.close()
                self.session = None
            return None
        except Exception as e:
            logger.error(f"CoinGecko API error: {e}")
            return None

    async def search_token(self, query: str) -> Optional[Dict]:
        """Search for token ID using query string
        Docs: https://docs.coingecko.com/reference/search-1
        """
        try:
            url = f"{self.base_url}/search"
            params = {
                "query": query,
                "x_cg_demo_api_key": self.api_key
            }
            
            async with aiohttp.ClientSession() as session:
                async with session.get(url, params=params) as response:
                    if response.status == 429:
                        logger.warning("Rate limit hit, waiting before retry...")
                        await asyncio.sleep(60)
                        return None
                        
                    response.raise_for_status()
                    data = await response.json()
                    
                    if not data or "coins" not in data or not data["coins"]:
                        logger.debug(f"No results found for {query}")
                        return None
                    
                    return data  # Return full response for processing
                    
        except Exception as e:
            logger.error(f"CoinGecko search error for {query}: {e}")
            return None

    @sync_lru_cache(maxsize=1000)
    async def _get_coingecko_id(self, symbol: str) -> Optional[str]:
        """Get CoinGecko ID for a token symbol"""
        try:
            # Check cache first
            if symbol in self._id_cache:
                return self._id_cache[symbol]
                
            # First check our known mappings
            if symbol in self.SYMBOL_TO_COINGECKO:
                self._id_cache[symbol] = self.SYMBOL_TO_COINGECKO[symbol]
                return self._id_cache[symbol]
                
            # Try to search for the token
            search_data = await self.search_token(symbol)
            if search_data and isinstance(search_data, dict) and "coins" in search_data:
                # Get the first matching result with exact symbol match
                for coin in search_data["coins"]:
                    if coin.get("symbol", "").upper() == symbol.upper():
                        logger.info(f"Found CoinGecko ID for {symbol}: {coin['id']}")
                        self._id_cache[symbol] = coin['id']
                        return self._id_cache[symbol]
            
            logger.debug(f"No matching token found for symbol: {symbol}")
            return None
            
        except Exception as e:
            logger.error(f"Error finding CoinGecko ID for {symbol}: {e}")
            return None

    async def get_token_details(self, coingecko_id: str) -> Optional[Dict]:
        """Get detailed token data from CoinGecko"""
        try:
            async with aiohttp.ClientSession() as session:
                url = f"{self.base_url}/coins/{coingecko_id}"
                params = {
                    "x_cg_demo_api_key": self.api_key,
                    "localization": "false",
                    "tickers": "false",
                    "community_data": "true",
                    "developer_data": "true",
                    "sparkline": "false"
                }
                
                async with session.get(url, params=params) as response:
                    if response.status == 200:
                        data = await response.json()
                        return {
                            "market_cap": data.get("market_data", {}).get("market_cap", {}).get("usd"),
                            "total_volume": data.get("market_data", {}).get("total_volume", {}).get("usd"),
                            "circulating_supply": data.get("market_data", {}).get("circulating_supply"),
                            "total_supply": data.get("market_data", {}).get("total_supply"),
                            "max_supply": data.get("market_data", {}).get("max_supply"),
                            "price_change_24h": data.get("market_data", {}).get("price_change_percentage_24h"),
                            "price_change_7d": data.get("market_data", {}).get("price_change_percentage_7d"),
                            "price_change_30d": data.get("market_data", {}).get("price_change_percentage_30d"),
                            "twitter_followers": data.get("community_data", {}).get("twitter_followers"),
                            "reddit_subscribers": data.get("community_data", {}).get("reddit_subscribers"),
                            "telegram_channel_user_count": data.get("community_data", {}).get("telegram_channel_user_count"),
                            "forks": data.get("developer_data", {}).get("forks"),
                            "stars": data.get("developer_data", {}).get("stars"),
                            "subscribers": data.get("developer_data", {}).get("subscribers"),
                            "total_issues": data.get("developer_data", {}).get("total_issues"),
                            "closed_issues": data.get("developer_data", {}).get("closed_issues"),
                            "pull_requests_merged": data.get("developer_data", {}).get("pull_requests_merged"),
                            "commit_count_4_weeks": data.get("developer_data", {}).get("commit_count_4_weeks"),
                        }
                    else:
                        logger.error(f"Error getting token details for {coingecko_id}: {response.status}")
                        return None
                        
        except Exception as e:
            logger.error(f"Error getting token details for {coingecko_id}: {e}")
            return None