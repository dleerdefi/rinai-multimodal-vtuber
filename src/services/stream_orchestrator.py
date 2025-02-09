import asyncio
import logging
from typing import Optional
from src.managers.voice_manager import VoiceManager
from src.managers.speech_manager import SpeechManager
from src.managers.chat_manager import ChatManager
from src.services.llm_service import LLMService, ModelType, LLMProvider
import aiohttp
from datetime import datetime
import time
import os
from src.services.websocket_server import ChatWebSocketServer
from src.agents.rin.agent import RinAgent
from src.services.schedule_service import ScheduleService

logger = logging.getLogger(__name__)

class StreamOrchestrator:
    def __init__(self, config: dict):
        """Initialize stream orchestrator with direct LLM integration"""
        try:
            self.config = config
            self.running = True  # Set to True by default
            self.tts_queue = asyncio.Queue()
            
            # Add session management
            self.current_session_id = None
            
            # Initialize voice manager for streaming TTS
            self.voice_manager = VoiceManager(
                elevenlabs_key=config['elevenlabs_key'],
                voice_id=config['voice_id']
            )
            
            # Initialize LLM service
            self.llm_service = LLMService()
            
            # Set default model
            self.model_type = ModelType.SAO_10K_L31_70B_EURYALE_V2_2
            
            # Initialize chat manager if YouTube stream ID is provided
            self.chat_manager = None
            if config.get('enable_youtube_chat') and config.get('youtube_stream_id'):
                self.chat_manager = ChatManager(
                    video_id=config['youtube_stream_id']
                )
                logger.info(f"ChatManager initialized with stream ID: {config['youtube_stream_id']}")
            
            # Speech components
            self.speech_manager = None
            self.speech_enabled = False
            if config.get('enable_speech_input'):
                self.speech_manager = SpeechManager(
                    groq_key=config['groq_key'],
                    device_index=config.get('audio_device_index', 1)
                )

                self.speech_manager.set_message_callback(self.handle_host_message)
            
            # Initialize RinAgent with mongo_uri
            if 'mongo_uri' not in config:
                raise ValueError("MongoDB URI not found in config")
            self.agent = RinAgent(mongo_uri=config['mongo_uri'])
            
            # Initialize schedule service
            self.schedule_service = ScheduleService(config['mongo_uri'])
            
            logger.info("StreamOrchestrator initialization complete")
            
        except Exception as e:
            logger.error(f"Error initializing StreamOrchestrator: {e}")
            raise

    async def handle_chat_message(self, message: str, author: str = "YouTube Chat"):
        """Handle incoming chat messages from YouTube"""
        try:
            if not self.current_session_id:
                self.current_session_id = f"session_{int(time.time())}"
                await self.agent.start_new_session(self.current_session_id)
                logger.info(f"Created new session: {self.current_session_id}")
            
            # Always use livestream type for YouTube chat
            interaction_type = "livestream"
            
            # Broadcast user's message to WebSocket clients
            await self.ws_server.broadcast_message({
                'author': author,
                'content': message,
                'timestamp': datetime.now().isoformat()
            })
            
            # Route through RinAgent
            response = await self.agent.get_response(
                session_id=self.current_session_id,
                message=message,
                role=author,
                interaction_type="livestream"
            )
            
            logger.info(f"Got response from RinAgent: {response}")
            
            # Broadcast Rin's response to WebSocket clients
            if response:
                await self.ws_server.broadcast_message({
                    'author': 'Rin',
                    'content': response,
                    'timestamp': datetime.now().isoformat()
                })
            
            # Stream response to TTS
            if response and self.voice_manager:
                await self.voice_manager.say(response)
            
            return response
                
        except Exception as e:
            logger.error(f"Error handling chat message: {e}", exc_info=True)
            return None

    async def handle_host_message(self, message: str):
        """Handle messages from speech-to-text for host responses"""
        try:
            logger.info(f"Received host message from STT: {message}")
            
            if not self.current_session_id:
                self.current_session_id = f"session_{int(time.time())}"
                await self.agent.start_new_session(self.current_session_id)
                logger.info(f"Created new session: {self.current_session_id}")
            
            # Use session type from config
            interaction_type = "livestream" if self.config.get('session_type') == 'stream' else "local_agent"
            
            # Broadcast user's message to WebSocket clients
            await self.ws_server.broadcast_message({
                'author': 'Admin',
                'content': message,
                'timestamp': datetime.now().isoformat()
            })
            
            # Route through RinAgent
            response = await self.agent.get_response(
                session_id=self.current_session_id,
                message=message,
                role="host",
                interaction_type=interaction_type  # Use config-based type
            )
            
            logger.info(f"Got response from RinAgent: {response}")
            
            # Broadcast Rin's response to WebSocket clients
            if response:
                await self.ws_server.broadcast_message({
                    'author': 'Rin',
                    'content': response,
                    'timestamp': datetime.now().isoformat()
                })
            
            # Stream response to TTS
            if response and self.voice_manager:
                await self.voice_manager.say(response)
            
            return response
                
        except Exception as e:
            logger.error(f"Error handling host message: {e}")
            return None

    async def start(self):
        """Start all services"""
        try:
            # Initialize WebSocket server
            self.ws_server = ChatWebSocketServer()
            await self.ws_server.start()
            
            # Initialize RinAgent and create a session
            await self.agent.initialize()
            self.current_session_id = f"session_{int(time.time())}"
            await self.agent.start_new_session(self.current_session_id)
            
            logger.info(f"Created new session: {self.current_session_id}")
            
            logger.info("Starting services...")
            
            # Start YouTube chat monitoring
            if self.chat_manager:
                logger.info("Starting YouTube chat monitoring...")
                # Create task for chat monitoring
                self.chat_task = asyncio.create_task(
                    self.chat_manager.start_reading(self.handle_chat_message)
                )
            
            # Initialize speech manager but don't start recording
            if self.speech_manager:
                logger.info("Initializing speech input...")
                await self.speech_manager.initialize()
                # Don't auto-start recording, wait for Alt+S
            
            # Start schedule service
            await self.schedule_service.start()
            
            logger.info("All services started successfully")
            
            # Keep the service running
            while self.running:
                await asyncio.sleep(0.1)
                
        except Exception as e:
            logger.error(f"Error starting services: {e}")
            raise

    async def shutdown(self):
        """Shutdown all services gracefully"""
        logger.info("Shutting down services...")
        
        try:
            self.running = False  # Ensure running flag is set to False
            
            # Stop chat manager
            if self.chat_manager:
                self.chat_manager.shutdown()
                logger.info("Chat manager shutdown complete")
            
            # Stop speech manager
            if self.speech_manager:
                self.speech_manager.stop_recording()
                await self.speech_manager.shutdown()
                logger.info("Speech manager shutdown complete")
            
            # Cancel any pending tasks
            for task in asyncio.all_tasks():
                if task is not asyncio.current_task():
                    task.cancel()
                    try:
                        await task
                    except asyncio.CancelledError:
                        pass
            
            # Stop schedule service
            if hasattr(self, 'schedule_service'):
                await self.schedule_service.stop()
                logger.info("Schedule service shutdown complete")
            
            logger.info("All services shutdown complete")
            
        except Exception as e:
            logger.error(f"Error during shutdown: {e}")
            raise

    async def pause(self):
        """Pause all services"""
        try:
            if self.chat_manager:
                self.chat_manager.running = False
            if self.speech_manager:
                self.speech_manager.stop_recording()
            logger.info("Services paused")
        except Exception as e:
            logger.error(f"Error pausing services: {e}")

    async def resume(self):
        """Resume all services"""
        try:
            if self.chat_manager:
                self.chat_manager.running = True
            if self.speech_manager and self.speech_enabled:
                self.speech_manager.start_recording()
            logger.info("Services resumed")
        except Exception as e:
            logger.error(f"Error resuming services: {e}")

    async def toggle_speech_input(self):
        """Toggle speech input on/off"""
        try:
            if not self.speech_manager:
                logger.warning("Speech manager not initialized")
                return
                
            if self.speech_enabled:
                self.speech_manager.stop_recording()
                self.speech_enabled = False
                logger.info("Speech input disabled")
            else:
                self.speech_manager.start_recording()
                self.speech_enabled = True
                logger.info("Speech input enabled")
                
        except Exception as e:
            logger.error(f"Error toggling speech input: {e}")
            raise

    async def _initialize_speech_manager(self, config):
        """Initialize speech manager with config"""
        try:
            logger.info("Initializing speech manager...")
            
            device_index = config.get('audio_device_index', 1)
            
            self.speech_manager = SpeechManager(
                groq_key=config['groq_key'],
                device_index=device_index
            )

            await self.speech_manager.initialize()  # Make sure this is awaited
            
            logger.info("Speech manager initialized successfully")
            
        except Exception as e:
            logger.error(f"Error initializing speech manager: {e}")
            raise

    async def handle_speech_input(self, text: str, author: str):
        """Handle transcribed speech input"""
        try:
            logger.info(f"💬 {author}: {text}")
            
            # Broadcast to WebSocket clients
            await self.ws_server.broadcast_message({
                'author': author,
                'content': text,
                'timestamp': datetime.now().isoformat()
            })
            
            # Process message with author info
            if self.chat_manager:
                await self.chat_manager.process_message(text, author)
            
        except Exception as e:
            logger.error(f"Error handling speech input: {e}")
            raise 