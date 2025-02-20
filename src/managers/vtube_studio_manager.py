# Not integrated into stream orchestrator yet

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
        # Create a dedicated tokens directory
        self.token_directory = Path("tokens")
        
        # Only include the 'rin' vtuber in this build
        self.plugin_infos = {
            'rin': {
                'plugin_name': 'StreamBattleManager_Rin',
                'developer': 'TradingBattleSystem',
                'authentication_token_path': self.token_directory / 'token_rin.txt'
            }
        }
        
        # Expression mappings for 'rin' (used to trigger different reactions)
        self.expression_mappings = {
            'rin': {
                'happy': '309cf4907d624bfcbfaa12cef9523047',   # e.g. "naughty" animation
                'sad': '11917e017b184a3f9ee5e44894b9f09d',     # e.g. "cry" expression
                'neutral': '71c1e26018f44dd98220425d770f58e8'  # reset (RemoveAllExpressions)
            }
        }
        
        # Create VTS instance only for 'rin'
        self.vts_instances = {
            'rin': pyvts.vts(plugin_info=self.plugin_infos['rin'], port=8001)
        }
        self.connected = {
            'rin': False
        }
        
        self._is_initialized = False
        self._last_connection_time = None
        self._reconnect_interval = 30  # seconds
        
        # Reaction patterns for 'rin'
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
                # Phase transitions and general interactions
                r'^Starting intro phase': 'happy',
                r'^Starting chat phase': 'neutral',
                r'^Starting conclusion phase': 'happy',
                r'.*waves to rin.*': 'happy',
                r'.*greets rin.*': 'happy',
                r'.*thanks rin.*': 'happy'
            }
        }

    async def initialize(self):
        """Initial setup with token handling and VTube Studio connection for 'rin'"""
        if not self._is_initialized:
            try:
                # Ensure token directory exists
                self.token_directory.mkdir(exist_ok=True)
                
                # Initialize connection for each vtuber (only 'rin' in this case)
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
                        # Connect using pyvts (which uses a WebSocket connection under the hood)
                        await self.vts_instances[vtuber].connect()
                        await self.vts_instances[vtuber].request_authenticate_token()
                        auth_response = await self.vts_instances[vtuber].request_authenticate()
                        
                        # Save new token if available
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
        """Ensure connection is active and reconnect if needed"""
        try:
            if not self._is_initialized:
                await self.initialize()
                return

            if self._last_connection_time:
                time_since_connect = (datetime.now() - self._last_connection_time).total_seconds()
                if time_since_connect > self._reconnect_interval:
                    logger.info("Refreshing VTubeStudio connection...")
                    await self.reconnect()

            # Verify the connection for 'rin'
            if not self.connected.get('rin'):
                logger.warning("Reconnecting rin...")
                await self.connect_single('rin')

        except Exception as e:
            logger.error(f"Error ensuring connection: {e}")
            traceback.print_exc()

    async def connect_single(self, vtuber: str):
        """Reconnect a single vtuber"""
        try:
            info = self.plugin_infos.get(vtuber)
            if not info:
                logger.error(f"No plugin info for {vtuber}")
                return

            token_path = info['authentication_token_path']
            if token_path.exists():
                with open(token_path, 'r') as f:
                    token = f.read().strip()
                    if token:
                        self.vts_instances[vtuber].token = token
                        logger.info(f"Loaded existing token for {vtuber}")

            await self.vts_instances[vtuber].connect()
            await self.vts_instances[vtuber].request_authenticate_token()
            auth_response = await self.vts_instances[vtuber].request_authenticate()
            
            if auth_response and 'data' in auth_response and 'authenticationToken' in auth_response['data']:
                new_token = auth_response['data']['authenticationToken']
                with open(token_path, 'w') as f:
                    f.write(new_token)
                logger.info(f"Saved new authentication token for {vtuber}")

            self.connected[vtuber] = True
            self._last_connection_time = datetime.now()
            logger.info(f"Successfully reconnected to VTube Studio for {vtuber}")
        except Exception as e:
            logger.error(f"Error reconnecting {vtuber}: {e}")
            traceback.print_exc()

    async def reconnect(self):
        """Reconnect all vtubers (only 'rin' in this build)"""
        for vtuber in self.plugin_infos.keys():
            await self.connect_single(vtuber)

    async def list_hotkeys(self, vtuber: str) -> list:
        """List all available hotkeys for a vtuber"""
        try:
            vts = self.vts_instances.get(vtuber)
            if vts and self.connected.get(vtuber):
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
            if vts and self.connected.get(vtuber):
                await vts.request(vts.vts_request.requestTriggerHotKey(hotkey_id))
                logger.info(f"Triggered hotkey {hotkey_id} for {vtuber}")
        except Exception as e:
            logger.error(f"Error triggering hotkey: {e}")
            traceback.print_exc()

    async def cleanup(self):
        """Close the VTube Studio connection for rin"""
        try:
            for vtuber, vts in self.vts_instances.items():
                if self.connected.get(vtuber):
                    await vts.close()
                    self.connected[vtuber] = False
                    logger.info(f"VTube Studio connection closed for {vtuber}")
        except Exception as e:
            logger.error(f"Error closing VTube Studio connections: {e}")

    async def set_expression(self, vtuber: str, expression: str, value: float):
        """Set an expression parameter value for a vtuber"""
        try:
            vts = self.vts_instances.get(vtuber)
            if vts and self.connected.get(vtuber):
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
        """Trigger a vtuber expression using hotkey"""
        try:
            if not self.connected.get(vtuber):
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

            # Reset to neutral after a short delay
            await asyncio.sleep(2)
            neutral_id = self.expression_mappings[vtuber]['neutral']
            await vts.request({
                'apiName': 'VTubeStudioPublicAPI',
                'apiVersion': '1.0',
                'requestID': 'trigger-hotkey-reset',
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
        """Check if a log message should trigger a reaction based on keyword patterns"""
        try:
            import re
            for vtuber, patterns in self.reaction_patterns.items():
                for pattern, expression in patterns.items():
                    if re.search(pattern, log_message, re.IGNORECASE):
                        logger.info(f"Log matched pattern '{pattern}' for {vtuber} -> {expression}")
                        # Trigger the expression for the matched reaction
                        await self.trigger_expression(vtuber, expression)
                        return  # Trigger only the first matching reaction
        except Exception as e:
            logger.error(f"Error checking log reaction: {e}")
            traceback.print_exc()
