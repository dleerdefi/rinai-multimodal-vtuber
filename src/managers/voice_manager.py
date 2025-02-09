import asyncio
import websockets
import json
import base64
import subprocess
import shutil
from elevenlabs import Voice, VoiceSettings
from elevenlabs.client import ElevenLabs
from typing import Optional, AsyncIterator
import logging
import os

logger = logging.getLogger(__name__)

class VoiceManager:
    def __init__(self, elevenlabs_key: str, voice_id: Optional[str] = None):
        """Initialize VoiceManager with ElevenLabs"""
        try:
            self.api_key = elevenlabs_key
            self.voice_id = voice_id
            
            # Initialize ElevenLabs client for non-streaming operations
            self.client = ElevenLabs(api_key=elevenlabs_key)
            logger.info("ElevenLabs client initialized successfully")
            
            # Check mpv installation for streaming
            if not self._is_installed("mpv"):
                logger.warning("mpv not found, necessary to stream audio. Install instructions: https://mpv.io/installation/")
            
            # Initialize voice settings
            try:
                voices_response = self.client.voices.get_all()
                voices_list = voices_response.voices
                
                voice = next((v for v in voices_list if v.voice_id == self.voice_id), None)
                if voice:
                    logger.info(f"Found requested voice: {voice.name}")
                    self.voice = Voice(
                        voice_id=voice.voice_id,
                        settings=VoiceSettings(
                            stability=0.5,  # Adjusted for streaming
                            similarity_boost=0.8  # Adjusted for streaming
                        )
                    )
                else:
                    raise ValueError(f"Voice ID {self.voice_id} not found")

            except Exception as e:
                logger.error(f"Error getting voices: {e}")
                raise

        except Exception as e:
            logger.error(f"Error initializing ElevenLabs client: {e}")
            raise

    def _is_installed(self, lib_name: str) -> bool:
        """Check if a system library is installed"""
        return shutil.which(lib_name) is not None

    async def _text_chunker(self, chunks: AsyncIterator[str]) -> AsyncIterator[str]:
        """Split text into chunks, ensuring to not break sentences."""
        splitters = (".", ",", "?", "!", ";", ":", "—", "-", "(", ")", "[", "]", "}", " ")
        buffer = ""

        async for text in chunks:
            if not text:
                continue
                
            if buffer.endswith(splitters):
                yield buffer + " "
                buffer = text
            elif text.startswith(splitters):
                yield buffer + text[0] + " "
                buffer = text[1:]
            else:
                buffer += text

        if buffer:
            yield buffer + " "

    async def _stream_audio(self, audio_stream: AsyncIterator[bytes]):
        """Stream audio data using mpv player"""
        if not self._is_installed("mpv"):
            raise ValueError("mpv not found, necessary to stream audio")

        mpv_process = subprocess.Popen(
            ["mpv", "--no-cache", "--no-terminal", "--", "fd://0"],
            stdin=subprocess.PIPE, 
            stdout=subprocess.DEVNULL, 
            stderr=subprocess.DEVNULL
        )

        logger.info("Started streaming audio")
        try:
            async for chunk in audio_stream:
                if chunk and mpv_process.stdin:
                    mpv_process.stdin.write(chunk)
                    mpv_process.stdin.flush()
        finally:
            if mpv_process.stdin:
                mpv_process.stdin.close()
            mpv_process.wait()

    async def _stream_tts(self, text_iterator: AsyncIterator[str]):
        """Stream text to speech using ElevenLabs websocket API"""
        uri = f"wss://api.elevenlabs.io/v1/text-to-speech/{self.voice_id}/stream-input?model_id=eleven_flash_v2_5"

        async with websockets.connect(uri) as websocket:
            try:
                # Initialize stream with voice settings and generation config
                await websocket.send(json.dumps({
                    "text": " ",  # First message must be a space
                    "voice_settings": {
                        "stability": 0.5,
                        "similarity_boost": 0.8
                    },
                    "generation_config": {
                        "chunk_length_schedule": [120, 160, 250, 290]  # Default schedule
                    },
                    "xi_api_key": self.api_key,
                }))

                async def listen():
                    """Listen to websocket for audio data"""
                    while True:
                        try:
                            message = await websocket.recv()
                            data = json.loads(message)
                            if data.get("audio"):
                                yield base64.b64decode(data["audio"])
                            elif data.get('isFinal'):
                                break
                        except websockets.exceptions.ConnectionClosed:
                            logger.warning("Connection closed")
                            break

                # Start streaming audio
                listen_task = asyncio.create_task(self._stream_audio(listen()))

                # Send text chunks
                async for text in self._text_chunker(text_iterator):
                    if not text.strip():
                        continue
                    await websocket.send(json.dumps({"text": text}))

                # Send final message with flush to ensure all text is processed
                await websocket.send(json.dumps({"text": " ", "flush": True}))
                
                # Send end of stream
                await websocket.send(json.dumps({"text": ""}))

                # Wait for audio to finish
                await listen_task

            except Exception as e:
                logger.error(f"Error in TTS stream: {e}")
                raise

    def _chunk_text(self, text: str, chunk_size: int = 500) -> list[str]:
        """Split text into manageable chunks at sentence boundaries"""
        # Clean the text first
        text = self._clean_text(text)
        
        # Split into chunks at sentence boundaries
        chunks = []
        sentences = text.split('. ')
        current_chunk = ''
        
        for sentence in sentences:
            # Add period back if it was removed by split
            if not sentence.endswith('.'):
                sentence += '.'
                
            # If adding this sentence would exceed chunk size, start new chunk
            if len(current_chunk) + len(sentence) > chunk_size and current_chunk:
                chunks.append(current_chunk.strip())
                current_chunk = sentence
            else:
                if current_chunk:
                    current_chunk += ' ' + sentence
                else:
                    current_chunk = sentence
        
        # Add the last chunk if there is one
        if current_chunk:
            chunks.append(current_chunk.strip())
            
        return chunks

    async def say(self, text: str):
        """Convert text to speech and stream it continuously"""
        try:
            if not text:
                logger.warning("Empty text received, skipping TTS")
                return

            # Split into chunks
            chunks = self._chunk_text(text)
            
            # Create a single text iterator for all chunks
            async def text_iterator():
                for chunk in chunks:
                    logger.info(f"Streaming chunk: {chunk[:50]}...")
                    yield chunk

            # Stream all chunks in one connection
            await self._stream_tts(text_iterator())
            
        except Exception as e:
            logger.error(f"Error in TTS pipeline: {e}")
            raise

    def handle_host_response(self, response_data: dict):
        """Process host response for TTS"""
        try:
            if not isinstance(response_data, dict):
                logger.error(f"Invalid response data type: {type(response_data)}")
                return

            speech_text = self._extract_chat_response(response_data)
            
            if speech_text:
                # Clean the text before TTS
                cleaned_text = self._clean_text(speech_text)
                
                # Create a preview string safely
                preview = cleaned_text[:50] + "..." if len(cleaned_text) > 50 else cleaned_text
                logger.info(f"Processing TTS for response: {preview}")
                
                # Use asyncio to run the streaming TTS
                asyncio.create_task(self.say(cleaned_text))
            else:
                logger.debug(f"No speech text found in response: {response_data}")
                
        except Exception as e:
            logger.error(f"Error handling host response for TTS: {e}")
            raise

    def _clean_text(self, text: str) -> str:
        """Clean text for TTS processing"""
        if not isinstance(text, str):
            logger.error(f"Invalid text type: {type(text)}")
            return str(text)
        
        cleaned = text.replace('*', '')
        cleaned = cleaned.replace('~', '')
        return cleaned

    def _extract_chat_response(self, data: dict) -> Optional[str]:
        """Extract response text from nested chat response"""
        try:
            if not isinstance(data, dict):
                return None
                
            if 'response' in data and isinstance(data['response'], str):
                return data['response']
                
            if 'data' in data:
                return self._extract_chat_response(data['data'])
                
            if 'response' in data and isinstance(data['response'], dict):
                return self._extract_chat_response(data['response'])
                
            return None
            
        except Exception as e:
            logger.error(f"Error extracting chat response: {e}")
            return None 