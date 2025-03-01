import logging
import json
import asyncio
import aiohttp
import uuid
from typing import Dict, List, Optional, Any, Union
from datetime import datetime, timedelta
import websockets

logger = logging.getLogger(__name__)

class SolverBusClient:
    """Client for interacting with the Defuse Protocol Solver Bus API"""
    
    def __init__(self, rpc_url: str = "https://solver-relay-v2.chaindefuser.com/rpc", 
                 ws_url: str = "wss://solver-relay-v2.chaindefuser.com/ws"):
        """Initialize the Solver Bus client"""
        self.rpc_url = rpc_url
        self.ws_url = ws_url
        self.session = None
        self.ws_connection = None
        self.subscription_id = None
        self.request_id = 1  # Counter for JSON-RPC request IDs
        self.callbacks = {}  # Store callbacks for websocket events
        self.intent_status_callbacks = {}  # Callbacks for intent status updates
        
    async def __aenter__(self):
        """Set up aiohttp session for async context manager support"""
        self.session = aiohttp.ClientSession()
        return self
        
    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Clean up resources when exiting context"""
        if self.session:
            await self.session.close()
        if self.ws_connection:
            await self.ws_connection.close()
    
    async def initialize(self):
        """Initialize the client and create HTTP session"""
        if not self.session:
            self.session = aiohttp.ClientSession()
            
    async def cleanup(self):
        """Clean up resources"""
        if self.session:
            await self.session.close()
        if self.ws_connection:
            await self.ws_connection.close()
    
    async def get_quote(self, 
                        token_in: str, 
                        token_out: str, 
                        amount_in: str = None,
                        amount_out: str = None,
                        quote_id: str = None,
                        min_deadline_ms: int = 60000) -> Dict:
        """
        Get quotes from solvers for a potential swap
        
        Args:
            token_in: Input token identifier (e.g., "nep141:ft1.near")
            token_out: Output token identifier (e.g., "nep141:ft2.near")
            amount_in: Exact amount of input token (mutually exclusive with amount_out)
            amount_out: Exact amount of output token (mutually exclusive with amount_in)
            quote_id: Optional identifier for the quote
            min_deadline_ms: Minimum validity time for offers
            
        Returns:
            Dict containing quotes from solvers
        """
        if not self.session:
            await self.initialize()
            
        if not quote_id:
            quote_id = str(uuid.uuid4())
            
        if amount_in is None and amount_out is None:
            raise ValueError("Either amount_in or amount_out must be specified")
            
        if amount_in is not None and amount_out is not None:
            raise ValueError("Only one of amount_in or amount_out should be specified")
        
        # Prepare request params based on what's provided
        params = {
            "defuse_asset_identifier_in": token_in,
            "defuse_asset_identifier_out": token_out,
            "quote_id": quote_id,
            "min_deadline_ms": str(min_deadline_ms)
        }
        
        if amount_in is not None:
            params["exact_amount_in"] = str(amount_in)
        else:
            params["exact_amount_out"] = str(amount_out)
            
        # Prepare JSON-RPC request
        request_data = {
            "id": self.request_id,
            "jsonrpc": "2.0",
            "method": "quote",
            "params": [params]
        }
        self.request_id += 1
        
        try:
            async with self.session.post(self.rpc_url, json=request_data) as response:
                if response.status != 200:
                    error_text = await response.text()
                    logger.error(f"Error getting quote: {response.status} - {error_text}")
                    return {
                        "success": False,
                        "error": f"HTTP error: {response.status}",
                        "details": error_text
                    }
                
                result = await response.json()
                logger.info(f"Quote result: {result}")
                
                if "error" in result:
                    return {
                        "success": False,
                        "error": result["error"].get("message", "Unknown error"),
                        "code": result["error"].get("code", -1)
                    }
                
                if "result" not in result:
                    return {
                        "success": False,
                        "error": "Invalid response: missing 'result'"
                    }
                
                return {
                    "success": True,
                    "quotes": result["result"].get("quotes", []),
                    "quote_id": quote_id
                }
                
        except Exception as e:
            logger.error(f"Error in get_quote: {str(e)}")
            return {
                "success": False,
                "error": f"Exception: {str(e)}"
            }
    
    async def publish_intent(self, quote_hashes: List[str], signed_data: Dict) -> Dict:
        """
        Publish a signed intent to the Solver Bus
        
        Args:
            quote_hashes: List of quote hashes to execute
            signed_data: Signed intent data with standard, message, nonce, etc.
            
        Returns:
            Dict with status of the published intent
        """
        if not self.session:
            await self.initialize()
            
        # Prepare JSON-RPC request
        request_data = {
            "id": self.request_id,
            "jsonrpc": "2.0",
            "method": "publish_intent",
            "params": [{
                "quote_hashes": quote_hashes,
                "signed_data": signed_data
            }]
        }
        self.request_id += 1
        
        try:
            async with self.session.post(self.rpc_url, json=request_data) as response:
                if response.status != 200:
                    error_text = await response.text()
                    logger.error(f"Error publishing intent: {response.status} - {error_text}")
                    return {
                        "success": False,
                        "error": f"HTTP error: {response.status}",
                        "details": error_text
                    }
                
                result = await response.json()
                logger.info(f"Publish intent result: {result}")
                
                if "error" in result:
                    return {
                        "success": False,
                        "error": result["error"].get("message", "Unknown error"),
                        "code": result["error"].get("code", -1)
                    }
                
                if "result" not in result:
                    return {
                        "success": False,
                        "error": "Invalid response: missing 'result'"
                    }
                
                return {
                    "success": True,
                    "status": result["result"].get("status"),
                    "reason": result["result"].get("reason"),
                    "intent_hash": result["result"].get("intent_hash")
                }
                
        except Exception as e:
            logger.error(f"Error in publish_intent: {str(e)}")
            return {
                "success": False,
                "error": f"Exception: {str(e)}"
            }
    
    async def get_intent_status(self, intent_hash: str) -> Dict:
        """
        Get the status of an intent
        
        Args:
            intent_hash: Hash of the intent to check
            
        Returns:
            Dict with the status of the intent
        """
        if not self.session:
            await self.initialize()
            
        # Prepare JSON-RPC request
        request_data = {
            "id": self.request_id,
            "jsonrpc": "2.0",
            "method": "get_status",
            "params": [{
                "intent_hash": intent_hash
            }]
        }
        self.request_id += 1
        
        try:
            async with self.session.post(self.rpc_url, json=request_data) as response:
                if response.status != 200:
                    error_text = await response.text()
                    logger.error(f"Error getting intent status: {response.status} - {error_text}")
                    return {
                        "success": False,
                        "error": f"HTTP error: {response.status}",
                        "details": error_text
                    }
                
                result = await response.json()
                logger.info(f"Intent status result: {result}")
                
                if "error" in result:
                    return {
                        "success": False,
                        "error": result["error"].get("message", "Unknown error"),
                        "code": result["error"].get("code", -1)
                    }
                
                if "result" not in result:
                    return {
                        "success": False,
                        "error": "Invalid response: missing 'result'"
                    }
                
                return {
                    "success": True,
                    "intent_hash": result["result"].get("intent_hash"),
                    "status": result["result"].get("status"),
                    "data": result["result"].get("data", {})
                }
                
        except Exception as e:
            logger.error(f"Error in get_intent_status: {str(e)}")
            return {
                "success": False,
                "error": f"Exception: {str(e)}"
            }
    
    async def start_websocket_connection(self):
        """Start WebSocket connection to Solver Bus"""
        try:
            self.ws_connection = await websockets.connect(self.ws_url)
            
            # Subscribe to quote events
            subscribe_request = {
                "jsonrpc": "2.0",
                "id": self.request_id,
                "method": "subscribe",
                "params": ["quote"]
            }
            self.request_id += 1
            
            await self.ws_connection.send(json.dumps(subscribe_request))
            response = await self.ws_connection.recv()
            result = json.loads(response)
            
            if "result" in result:
                self.subscription_id = result["result"]
                logger.info(f"Successfully subscribed to quote events with ID: {self.subscription_id}")
                
                # Start listening for events in the background
                asyncio.create_task(self._listen_for_ws_events())
                return True
            else:
                logger.error(f"Failed to subscribe to quote events: {result}")
                return False
                
        except Exception as e:
            logger.error(f"Error starting WebSocket connection: {str(e)}")
            return False
    
    async def _listen_for_ws_events(self):
        """Listen for WebSocket events and process them"""
        try:
            while True:
                if not self.ws_connection:
                    logger.error("WebSocket connection is not established")
                    break
                    
                message = await self.ws_connection.recv()
                data = json.loads(message)
                
                if "method" in data and data["method"] == "subscribe":
                    params = data.get("params", {})
                    
                    # Check if this is a quote_status event
                    if "quote_hash" in params and "intent_hash" in params:
                        quote_hash = params["quote_hash"]
                        intent_hash = params["intent_hash"]
                        tx_hash = params.get("tx_hash")
                        
                        logger.info(f"Received quote_status event: quote_hash={quote_hash}, intent_hash={intent_hash}")
                        
                        # Call intent status callback if registered
                        if intent_hash in self.intent_status_callbacks:
                            callback = self.intent_status_callbacks[intent_hash]
                            asyncio.create_task(callback({
                                "intent_hash": intent_hash,
                                "quote_hash": quote_hash,
                                "tx_hash": tx_hash
                            }))
                    
                    # Check if this is a quote request event
                    elif "quote_id" in params:
                        quote_id = params["quote_id"]
                        logger.info(f"Received quote request: {params}")
                        
                        # Call quote callback if registered
                        if quote_id in self.callbacks:
                            callback = self.callbacks[quote_id]
                            asyncio.create_task(callback(params))
                
        except websockets.exceptions.ConnectionClosed:
            logger.warning("WebSocket connection closed")
        except Exception as e:
            logger.error(f"Error in WebSocket listener: {str(e)}")
        finally:
            # Try to reconnect if connection was lost
            if self.ws_connection:
                try:
                    await self.ws_connection.close()
                except:
                    pass
                self.ws_connection = None
    
    async def stop_websocket_connection(self):
        """Stop WebSocket connection and unsubscribe"""
        if self.ws_connection and self.subscription_id:
            try:
                # Unsubscribe from quote events
                unsubscribe_request = {
                    "jsonrpc": "2.0",
                    "id": self.request_id,
                    "method": "unsubscribe",
                    "params": [self.subscription_id]
                }
                self.request_id += 1
                
                await self.ws_connection.send(json.dumps(unsubscribe_request))
                await self.ws_connection.close()
                self.ws_connection = None
                self.subscription_id = None
                logger.info("Successfully closed WebSocket connection")
                return True
            except Exception as e:
                logger.error(f"Error stopping WebSocket connection: {str(e)}")
                return False
        return True
    
    def register_intent_status_callback(self, intent_hash: str, callback):
        """Register a callback for intent status updates"""
        self.intent_status_callbacks[intent_hash] = callback
        
    def unregister_intent_status_callback(self, intent_hash: str):
        """Unregister a callback for intent status updates"""
        if intent_hash in self.intent_status_callbacks:
            del self.intent_status_callbacks[intent_hash] 