import pytchat
from pytchat import LiveChat, SpeedCalculator
from typing import Callable
import time
import logging
import asyncio
from dotenv import load_dotenv
import os

logger = logging.getLogger(__name__)

class ChatManager:
    def __init__(self, video_id: str):
        """Initialize chat manager"""
        load_dotenv()
        self.video_id = video_id
        self.chat = pytchat.create(video_id=video_id)
        self.speed_chat = pytchat.create(
            video_id=video_id, 
            processor=SpeedCalculator(capacity=100)
        )
        self.running = False
        self.message_buffer = []
        self.last_process_time = time.time()
        self.PROCESS_INTERVAL = 1.0  # Process messages every second
        

    async def process_messages(self, message_handler: Callable):
        """Process messages asynchronously"""
        self.running = True
        logger.info(f"Starting chat processing for video ID: {self.video_id}")
        
        while self.running and self.chat.is_alive():
            try:
                chat_data = self.chat.get()
                current_time = time.time()
                
                if chat_data:
                    for c in chat_data.sync_items():
                        try:
                            # Add message to buffer with metadata
                            self.message_buffer.append({
                                'message': c.message,
                                'author': c.author.name,
                                'interaction_type': 'livestream'
                            })
                            
                            # Process buffer if enough time has passed
                            if current_time - self.last_process_time >= self.PROCESS_INTERVAL:
                                if self.message_buffer:
                                    # Process all messages in buffer
                                    for msg_data in self.message_buffer:
                                        logger.info(f"Processing chat message from {msg_data['author']}")
                                        await message_handler(
                                            msg_data['message'],
                                            msg_data['author']
                                        )
                                    self.message_buffer.clear()
                                    self.last_process_time = current_time
                            
                        except Exception as e:
                            logger.error(f"Error processing chat message: {e}")
                        
                        # Check chat speed
                        if self.speed_chat.get() >= 100:
                            logger.warning(f"Chat speed: {self.speed_chat.get()} messages/minute")
                            logger.warning("Chat speed limit reached. Terminating chat connection.")
                            self.shutdown()
                            return
                
                await asyncio.sleep(0.1)
                
            except Exception as e:
                logger.error(f"Error in message processing loop: {e}")
                await asyncio.sleep(1)  # Wait before retrying
                
        logger.info("Chat processing stopped")

    async def start_reading(self, message_handler: Callable):
        """Start reading chat messages"""
        logger.info("Starting chat monitoring...")
        await self.process_messages(message_handler)

    def shutdown(self):
        """Gracefully shutdown the chat manager"""
        self.running = False
        if self.chat:
            self.chat.terminate()
        if self.speed_chat:
            self.speed_chat.terminate()
        logger.info("Chat manager shutdown complete")