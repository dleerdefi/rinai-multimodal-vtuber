from datetime import datetime
import logging
import asyncio
from typing import Dict, Optional, Any

from src.tools.base import BaseTool, AgentResult
from src.clients.coingecko_client import CoinGeckoClient

logger = logging.getLogger(__name__)

class CryptoTool(BaseTool):
    name = "crypto_data"
    description = "Cryptocurrency price and market data tool"
    version = "1.0.0"
    
    def __init__(self, coingecko_client: Optional[CoinGeckoClient]):
        super().__init__()
        self.coingecko = coingecko_client
        
    async def initialize(self):
        """Initialize the CoinGecko client's async session"""
        if self.coingecko:
            self.coingecko = await self.coingecko.__aenter__()
            
    async def cleanup(self):
        """Cleanup the CoinGecko client's async session"""
        if self.coingecko:
            await self.coingecko.__aexit__(None, None, None)

    async def run(self, input_data: Any) -> Dict[str, Any]:
        """Main execution method"""
        return await self._get_crypto_data(input_data)

    def can_handle(self, input_data: Any) -> bool:
        """Delegate to TriggerDetector"""
        return isinstance(input_data, str)  # Basic type check only

    async def execute(self, command: str) -> Dict:
        """Execute crypto data command"""
        try:
            if not self.coingecko:
                return {
                    "status": "error",
                    "error": "CoinGecko client not configured",
                    "timestamp": datetime.utcnow().isoformat()
                }
            
            # Extract symbol from command
            words = command.lower().split()
            for word in words:
                if word in self.coingecko.SYMBOL_TO_COINGECKO:
                    symbol = word.upper()
                    break
            else:
                symbol = 'BTC'  # Default to Bitcoin if no clear symbol found
                
            return await self._get_crypto_data(
                symbol=symbol,
                include_details=True
            )
            
        except Exception as e:
            logger.error(f"Error executing crypto command: {e}")
            return {
                "status": "error",
                "error": str(e),
                "timestamp": datetime.utcnow().isoformat()
            }

    async def _get_crypto_data(self, symbol: str, include_details: bool = False) -> Dict:
        """Get cryptocurrency data with proper session handling"""
        try:
            # Get CoinGecko ID
            coingecko_id = await self.coingecko._get_coingecko_id(symbol)
            if not coingecko_id:
                return {
                    "status": "error",
                    "error": f"Could not find CoinGecko ID for symbol: {symbol}",
                    "timestamp": datetime.utcnow().isoformat()
                }

            # Gather all requested data concurrently
            tasks = [self.coingecko.get_token_price(coingecko_id)]
            
            if include_details:
                tasks.append(self.coingecko.get_token_details(coingecko_id))

            results = await asyncio.gather(*tasks)
            
            # Process results
            data = {}
            for result in results:
                if result:  # Only update if result is not None
                    data.update(result)

            return {
                "status": "success",
                "data": data,
                "timestamp": datetime.utcnow().isoformat()
            }

        except Exception as e:
            logger.error(f"Error fetching crypto data for {symbol}: {str(e)}")
            return {
                "status": "error",
                "error": str(e),
                "timestamp": datetime.utcnow().isoformat()
            }

    async def _get_crypto_market_data(self, symbol: str, include_social: bool = True) -> Dict:
        """Get detailed cryptocurrency market data including social metrics"""
        try:
            coingecko_id = await self.coingecko._get_coingecko_id(symbol)
            if not coingecko_id:
                return {
                    "status": "error",
                    "error": f"Could not find CoinGecko ID for symbol: {symbol}"
                }
            
            details = await self.coingecko.get_token_details(coingecko_id)
            if not details:
                return {
                    "status": "error",
                    "error": f"Could not fetch market data for {symbol}"
                }
            
            # Filter social metrics if not requested
            if not include_social:
                details = {k: v for k, v in details.items() 
                          if not k in ['twitter_followers', 'reddit_subscribers', 
                                     'telegram_channel_user_count']}
            
            return {
                "status": "success",
                "data": details,
                "timestamp": datetime.utcnow().isoformat()
            }
            
        except Exception as e:
            logger.error(f"Error fetching market data for {symbol}: {str(e)}")
            return {
                "status": "error",
                "error": str(e),
                "timestamp": datetime.utcnow().isoformat()
            }

    def _format_crypto_response(self, data: Dict) -> str:
        """Format cryptocurrency data for Rin agent consumption"""
        try:
            response_parts = []
            
            # Basic price info
            if 'price_usd' in data:
                response_parts.append(f"💰 Current Price: ${data['price_usd']:,.2f} USD")
            
            # Price changes
            changes = {
                '24h': data.get('price_change_24h'),
                '7d': data.get('price_change_7d'),
                '30d': data.get('price_change_30d')
            }
            change_strs = []
            for period, change in changes.items():
                if change is not None:
                    emoji = "📈" if change > 0 else "📉"
                    change_strs.append(f"{emoji} {period}: {change:+.2f}%")
            if change_strs:
                response_parts.append("Price Changes:")
                response_parts.extend(change_strs)
            
            # Market data
            if 'market_cap' in data:
                response_parts.append(f"🌐 Market Cap: ${data['market_cap']:,.0f}")
            if 'total_volume' in data:
                response_parts.append(f"📊 24h Volume: ${data['total_volume']:,.0f}")
            
            # Supply info
            if any(k in data for k in ['circulating_supply', 'total_supply', 'max_supply']):
                response_parts.append("Supply Information:")
                if 'circulating_supply' in data:
                    response_parts.append(f"  • Circulating: {data['circulating_supply']:,.0f}")
                if 'total_supply' in data:
                    response_parts.append(f"  • Total: {data['total_supply']:,.0f}")
                if 'max_supply' in data:
                    response_parts.append(f"  • Max: {data['max_supply']:,.0f}")
            
            # Social metrics (if available)
            social_metrics = {
                'twitter_followers': '𝕏',
                'reddit_subscribers': '📱',
                'telegram_channel_user_count': '📢'
            }
            social_data = []
            for key, emoji in social_metrics.items():
                if data.get(key):
                    social_data.append(f"{emoji} {key.replace('_', ' ').title()}: {data[key]:,}")
            if social_data:
                response_parts.append("\nSocial Metrics:")
                response_parts.extend(social_data)
            
            return "\n".join(response_parts)
            
        except Exception as e:
            logger.error(f"Error formatting crypto response: {e}")
            return str(data)  # Fallback to basic string representation
