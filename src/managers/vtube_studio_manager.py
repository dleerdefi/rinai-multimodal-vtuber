import logging
import asyncio
import pyvts
from typing import Optional, Dict, Any
import traceback
import os
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)

class VTubeStudioManager:
    def __init__(self):
        # Define token file paths more robustly
        self.token_directory = Path("tokens")  # Create a dedicated tokens directory
        self.plugin_infos = {
            'rin': {
                'plugin_name': 'StreamBattleManager_Rin',
                'developer': 'TradingBattleSystem',
                'authentication_token_path': self.token_directory / 'token_rin.txt'
            },
            'biscuit': {
                'plugin_name': 'StreamBattleManager_Biscuit',
                'developer': 'TradingBattleSystem',
                'authentication_token_path': self.token_directory / 'token_biscuit.txt'
            }
        }
        
        # Define expression mappings based on actual hotkeys
        self.expression_mappings = {
            'rin': {
                'happy': '309cf4907d624bfcbfaa12cef9523047',  # 'naughty' animation (Surprised.motion3.json)
                'sad': '11917e017b184a3f9ee5e44894b9f09d',   # 'cry' expression
                'neutral': '71c1e26018f44dd98220425d770f58e8' # 'RemoveAllExpressions' to reset
            },
            'biscuit': {
                'happy': '8c6c08a9431d4f1f8d5a18a8f25d1456',   # 'Flush' expression
                'sad': 'ee42478539554380ab7f8adbabcc26f5',     # 'Confused' expression
                'neutral': 'f2ba3e8c825a4f4587186fa930e3e27a'  # 'C1' (base) expression
            }
        }
        
        # Create separate VTS instances for each VTuber
        self.vts_instances = {
            'rin': pyvts.vts(plugin_info=self.plugin_infos['rin'], port=8001),
            'biscuit': pyvts.vts(plugin_info=self.plugin_infos['biscuit'], port=8002)
        }
        self.connected = {
            'rin': False,
            'biscuit': False
        }
        
        # Add connection state tracking
        self._is_initialized = False
        self._last_connection_time = None
        self._reconnect_interval = 30  # seconds
        
        # Add reaction patterns based on actual logs
        self.reaction_patterns = {
            'rin': {
                # Standard voice reactions
                r'^Generating speech for rin:.*(?:happy|excited|great|amazing|laugh)': 'happy',
                r'^Generating speech for rin:.*(?:sad|sorry|unfortunate|upset)': 'sad',
                r'^Generating speech for rin:.*(?:neutral|normal|casual)': 'neutral',
                
                # Chat reactions
                r'^Agent rin responded:.*(?:giggles|smiles|happy)': 'happy',
                r'^Agent rin responded:.*(?:frowns|sad|upset)': 'sad',
                r'^Agent rin responded:.*(?:thinks|ponders)': 'neutral',
                
                # Battle/Trade reactions (preserved)
                r'^BUY_RIN.*success': 'happy',
                r'^SELL_RIN.*success': 'sad',
                r'^Error executing trade.*RIN': 'sad',
                
                # Phase transitions
                r'^Starting intro phase': 'happy',
                r'^Starting chat phase': 'neutral',
                r'^Starting battle phase': 'happy',
                r'^Starting conclusion phase': 'happy',
                
                # General interactions
                r'.*waves to rin.*': 'happy',
                r'.*greets rin.*': 'happy',
                r'.*thanks rin.*': 'happy'
            },
            'biscuit': {
                # Standard voice reactions
                r'^Generating speech for biscuit:.*(?:happy|excited|great|amazing|laugh)': 'happy',
                r'^Generating speech for biscuit:.*(?:sad|sorry|unfortunate|upset)': 'sad',
                r'^Generating speech for biscuit:.*(?:neutral|normal|casual)': 'neutral',
                
                # Chat reactions
                r'^Agent biscuit responded:.*(?:giggles|smiles|happy)': 'happy',
                r'^Agent biscuit responded:.*(?:frowns|sad|upset)': 'sad',
                r'^Agent biscuit responded:.*(?:thinks|ponders)': 'neutral',
                
                # Battle/Trade reactions (preserved)
                r'^BUY_BIZKIT.*success': 'happy',
                r'^SELL_BIZKIT.*success': 'sad',
                r'^Error executing trade.*BIZKIT': 'sad',
                
                # Phase transitions
                r'^Starting intro phase': 'happy',
                r'^Starting chat phase': 'neutral',
                r'^Starting battle phase': 'happy',
                r'^Starting conclusion phase': 'happy',
                
                # General interactions
                r'.*waves to biscuit.*': 'happy',
                r'.*greets biscuit.*': 'happy',
                r'.*thanks biscuit.*': 'happy'
            }
        }

    async def initialize(self):
        """Initial setup with token handling"""
        if not self._is_initialized:
            try:
                # Ensure token directory exists
                self.token_directory.mkdir(exist_ok=True)
                
                # Initialize connections for each VTuber
                for vtuber, info in self.plugin_infos.items():
                    token_path = info['authentication_token_path']
                    
                    # Try to load existing token
                    if token_path.exists():
                        with open(token_path, 'r') as f:
                            token = f.read().strip()
                            if token:
                                self.vts_instances[vtuber].token = token
                                logger.info(f"Loaded existing token for {vtuber}")
                    
                    try:
                        # Connect and authenticate
                        await self.vts_instances[vtuber].connect()
                        await self.vts_instances[vtuber].request_authenticate_token()
                        auth_response = await self.vts_instances[vtuber].request_authenticate()
                        
                        # Save new token
                        if auth_response and 'data' in auth_response and 'authenticationToken' in auth_response['data']:
                            new_token = auth_response['data']['authenticationToken']
                            with open(token_path, 'w') as f:
                                f.write(new_token)
                            logger.info(f"Saved new authentication token for {vtuber}")
                        
                        self.connected[vtuber] = True
                        logger.info(f"Successfully connected to VTube Studio for {vtuber}")
                        
                    except Exception as e:
                        logger.warning(f"Failed to connect {vtuber}: {e}")
                        self.connected[vtuber] = False
                
                self._is_initialized = True
                self._last_connection_time = datetime.now()
                
            except Exception as e:
                logger.error(f"Failed to initialize VTubeStudio Manager: {e}")
                raise

    async def ensure_connected(self):
        """Ensure connections are active, reconnect if needed"""
        try:
            if not self._is_initialized:
                await self.initialize()
                return

            # Check if we need to reconnect
            if self._last_connection_time:
                time_since_connect = (datetime.now() - self._last_connection_time).total_seconds()
                if time_since_connect > self._reconnect_interval:
                    logger.info("Refreshing VTubeStudio connections...")
                    await self.reconnect()

            # Verify connections
            for vtuber, vts in self.vts_instances.items():
                if not self.connected[vtuber]:
                    logger.warning(f"Reconnecting {vtuber}...")
                    await self.connect_single(vtuber)

        except Exception as e:
            logger.error(f"Error ensuring connections: {e}")
            raise

    async def connect_single(self, vtuber: str):
        """Connect single VTuber instance with better error handling"""
        try:
            vts = self.vts_instances[vtuber]
            info = self.plugin_infos[vtuber]
            token_path = info['authentication_token_path']
            
            # Try to load existing token
            if token_path.exists():
                with open(token_path, 'r') as f:
                    token = f.read().strip()
                    if token:
                        vts.token = token
                        logger.info(f"Loaded existing token for {vtuber}")
            
            # Connect
            await vts.connect()
            
            # Try to authenticate with existing token first
            try:
                auth_response = await vts.request_authenticate()
                if not auth_response or 'errorID' in auth_response:
                    raise Exception("Token invalid or expired")
            except Exception:
                # If existing token fails, request new one
                logger.info(f"Requesting new token for {vtuber}...")
                await vts.request_authenticate_token()
                auth_response = await vts.request_authenticate()
                
                # Save new token
                if auth_response and 'data' in auth_response and 'authenticationToken' in auth_response['data']:
                    new_token = auth_response['data']['authenticationToken']
                    with open(token_path, 'w') as f:
                        f.write(new_token)
                    logger.info(f"Saved new authentication token for {vtuber}")
            
            self.connected[vtuber] = True
            logger.info(f"Connected {vtuber} VTuber")
            
            # List available hotkeys after successful connection
            await self.list_detailed_hotkeys(vtuber)
            
        except Exception as e:
            logger.error(f"Error connecting {vtuber}: {e}")
            self.connected[vtuber] = False
            raise

    async def reconnect(self):
        """Reconnect all instances"""
        try:
            for vtuber in self.vts_instances:
                await self.connect_single(vtuber)
            self._last_connection_time = datetime.now()
        except Exception as e:
            logger.error(f"Error reconnecting: {e}")
            raise

    async def connect(self):
        """Initialize connections to both VTube Studio instances"""
        try:
            for vtuber, vts in self.vts_instances.items():
                logger.info(f"Connecting to VTube Studio for {vtuber}...")
                await vts.connect()
                self.connected[vtuber] = True
                
                # Request and use authentication token
                logger.info(f"Requesting authentication token for {vtuber}...")
                await vts.request_authenticate_token()
                
                logger.info(f"Authenticating {vtuber}...")
                await vts.request_authenticate()
                
                # List and log available hotkeys
                logger.info(f"Getting available hotkeys for {vtuber}...")
                response = await vts.request({
                    'apiName': 'VTubeStudioPublicAPI',
                    'apiVersion': '1.0',
                    'requestID': 'hotkey-list-request',
                    'messageType': 'HotkeysInCurrentModelRequest'
                })
                
                if response and 'data' in response and 'availableHotkeys' in response['data']:
                    hotkeys = response['data']['availableHotkeys']
                    logger.info(f"Available hotkeys for {vtuber}:")
                    for hotkey in hotkeys:
                        logger.info(f"  - {hotkey.get('name', 'Unknown')}: {hotkey.get('hotkeyID', 'No ID')}")
                else:
                    logger.warning(f"No hotkeys found for {vtuber}")
                    logger.debug(f"Full response: {response}")

                # Add custom parameters for expressions
                logger.info(f"Setting up custom parameters for {vtuber}...")
                parameters = [
                    f"{vtuber}_happy",
                    f"{vtuber}_sad",
                    f"{vtuber}_neutral"
                ]
                
                for param in parameters:
                    try:
                        param_data = {
                            "parameterName": param,
                            "defaultValue": 0,
                            "min": 0,
                            "max": 1,
                            "smoothing": 0
                        }
                        await vts.request({
                            'apiName': 'VTubeStudioPublicAPI',
                            'apiVersion': '1.0',
                            'requestID': 'parameter-creation',
                            'messageType': 'ParameterCreationRequest',
                            'data': param_data
                        })
                        logger.info(f"Added parameter: {param}")
                    except Exception as e:
                        logger.warning(f"Parameter {param} might already exist: {e}")
                
                logger.info(f"Successfully connected and set up {vtuber}")
                
        except Exception as e:
            logger.error(f"Failed to connect to VTube Studio: {e}")
            traceback.print_exc()
            raise

    async def list_hotkeys(self, vtuber: str) -> list:
        """List all available hotkeys for a VTuber"""
        try:
            vts = self.vts_instances.get(vtuber)
            if vts and self.connected[vtuber]:
                response = await vts.request(vts.vts_request.requestHotKeyList())
                logger.debug(f"Full hotkey response for {vtuber}: {response}")
                
                if response and 'data' in response and 'availableHotkeys' in response['data']:
                    hotkeys = response['data']['availableHotkeys']
                    for hotkey in hotkeys:
                        logger.info(f"Found hotkey for {vtuber}: {hotkey['name']} (ID: {hotkey['hotkeyID']})")
                    return hotkeys
                else:
                    logger.warning(f"No hotkeys found in response for {vtuber}: {response}")
                    return []
            return []
        except Exception as e:
            logger.error(f"Error listing hotkeys for {vtuber}: {e}")
            traceback.print_exc()
            return []

    async def trigger_hotkey(self, vtuber: str, hotkey_id: str):
        """Trigger a specific hotkey"""
        try:
            vts = self.vts_instances.get(vtuber)
            if vts and self.connected[vtuber]:
                await vts.request(vts.vts_request.requestTriggerHotKey(hotkey_id))
                logger.info(f"Triggered hotkey {hotkey_id} for {vtuber}")
        except Exception as e:
            logger.error(f"Error triggering hotkey: {e}")
            traceback.print_exc()

    async def cleanup(self):
        """Close all VTube Studio connections"""
        try:
            for vtuber, vts in self.vts_instances.items():
                if self.connected[vtuber]:
                    await vts.close()
                    self.connected[vtuber] = False
                    logger.info(f"VTube Studio connection closed for {vtuber}")
        except Exception as e:
            logger.error(f"Error closing VTube Studio connections: {e}")

    async def set_expression(self, vtuber: str, expression: str, value: float):
        """Set an expression parameter value"""
        try:
            vts = self.vts_instances.get(vtuber)
            if vts and self.connected[vtuber]:
                param_name = f"{vtuber}_{expression}"
                await vts.request(
                    vts.vts_request.requestSetCustomParameter(
                        param_name,
                        value
                    )
                )
                logger.info(f"Set {param_name} to {value}")
        except Exception as e:
            logger.error(f"Error setting expression: {e}")
            traceback.print_exc()

    async def trigger_expression(self, vtuber: str, expression: str):
        """Trigger a VTuber expression using hotkey"""
        try:
            if not self.connected[vtuber]:
                logger.warning(f"Cannot trigger expression - {vtuber} not connected")
                return

            # Get the expression ID from our mappings
            expression_id = self.expression_mappings[vtuber].get(expression)
            if not expression_id:
                logger.error(f"No expression ID found for {vtuber} -> {expression}")
                return

            # Trigger the hotkey via VTube Studio API
            vts = self.vts_instances[vtuber]
            await vts.request({
                'apiName': 'VTubeStudioPublicAPI',
                'apiVersion': '1.0',
                'requestID': 'trigger-hotkey',
                'messageType': 'HotkeyTriggerRequest',
                'data': {
                    'hotkeyID': expression_id
                }
            })
            logger.info(f"Triggered {expression} for {vtuber}")

            # Reset to neutral after a delay
            await asyncio.sleep(2)
            neutral_id = self.expression_mappings[vtuber]['neutral']
            await vts.request({
                'apiName': 'VTubeStudioPublicAPI',
                'apiVersion': '1.0',
                'requestID': 'trigger-hotkey',
                'messageType': 'HotkeyTriggerRequest',
                'data': {
                    'hotkeyID': neutral_id
                }
            })

        except Exception as e:
            logger.error(f"Error triggering expression: {e}")
            traceback.print_exc()

    async def list_detailed_hotkeys(self, vtuber: str):
        """List all available hotkeys with detailed information"""
        try:
            vts = self.vts_instances.get(vtuber)
            if vts and self.connected[vtuber]:
                response = await vts.request(vts.vts_request.requestHotKeyList())
                
                if response and 'data' in response and 'availableHotkeys' in response['data']:
                    hotkeys = response['data']['availableHotkeys']
                    logger.info(f"\nAvailable Hotkeys for {vtuber}:")
                    for hotkey in hotkeys:
                        logger.info(f"""
Name: {hotkey.get('name', 'Unnamed')}
Type: {hotkey.get('type', 'Unknown')}
File: {hotkey.get('file', 'No file')}
ID: {hotkey.get('hotkeyID', 'No ID')}
Description: {hotkey.get('description', 'No description')}
----------------------------------------""")
                    return hotkeys
                else:
                    logger.warning(f"No hotkeys found for {vtuber}")
                    return []
            return []
        except Exception as e:
            logger.error(f"Error listing hotkeys for {vtuber}: {e}")
            traceback.print_exc()            
            return []

    async def check_log_reaction(self, log_message: str):
        """Check if a log message should trigger a reaction"""
        try:
            import re
            for vtuber, patterns in self.reaction_patterns.items():
                for pattern, expression in patterns.items():
                    if re.search(pattern, log_message, re.IGNORECASE):
                        logger.info(f"Log matched pattern '{pattern}' for {vtuber} -> {expression}")
                        # Actually trigger the VTuber expression
                        await self.trigger_expression(vtuber, expression)
                        return  # Only trigger first matching reaction

        except Exception as e:
            logger.error(f"Error checking log reaction: {e}")
            traceback.print_exc()
