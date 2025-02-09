import logging
import sounddevice as sd
import numpy as np
import wave
import threading
import queue
import time
import tempfile
import os
from typing import Optional, Callable
import asyncio
from datetime import datetime
from dotenv import load_dotenv
from src.utils.audio_chunking_code import transcribe_single_chunk
from groq import Groq
from pydub import AudioSegment
import keyboard
import io
from inspect import stack

logger = logging.getLogger(__name__)

class SpeechManager:
    def __init__(self, groq_key: str, device_index: int = 1):
        """Initialize SpeechManager with Groq Whisper"""
        try:
            # Get keys from environment
            load_dotenv()
            self.groq_key = os.getenv('GROQ_API_KEY')
            
            if not self.groq_key:
                raise ValueError("Missing required environment variable: GROQ_API_KEY")
            
            # Initialize Groq client
            self.groq_client = Groq(api_key=self.groq_key, max_retries=3)
            
            # Audio setup
            self.device_index = device_index
            self.audio_queue = queue.Queue()
            self.buffer_size = 0
            self.max_buffer_size = 32000  # About 2 seconds at 16kHz
            self.is_recording = False
            self.callback_fn = None
            self.sample_rate = 16000
            self.stream = None
            self.main_loop = None
            self.last_transcription = None  # Track last transcription
            self.last_transcription_time = 0  # Track timing
            self.author_name = "Voice Input"
            
            # Adjust these parameters for better speech detection
            self.silence_threshold = 300  # Increase threshold
            self.silence_duration = 2.0   # Increase duration
            self.min_phrase_duration = 1.0  # Minimum duration
            self.max_phrase_duration = 30.0  # Maximum duration
            
            # Initialize audio stream settings
            self.channels = 1
            self.dtype = np.int16
            
            self.audio_data = []
            
            logger.info("SpeechManager initialized successfully")
            logger.info("Speech input ready (Alt+S to start/stop recording)")
            
        except Exception as e:
            logger.error(f"Error initializing SpeechManager: {e}")
            raise

    async def initialize(self):
        """Initialize speech manager"""
        try:
            logger.info("Initializing speech manager...")
            
            # Test audio device
            try:
                device_info = sd.query_devices(self.device_index, 'input')
                logger.info(f"Using audio device: {device_info['name']}")
            except Exception as e:
                logger.error(f"Error accessing audio device {self.device_index}: {e}")
                raise
                
            # Store the event loop
            self.main_loop = asyncio.get_running_loop()
            logger.info("Speech manager initialized successfully")
            # Don't start recording here
            
        except Exception as e:
            logger.error(f"Error initializing speech manager: {e}")
            raise

    def _process_audio(self):
        """Process accumulated audio data"""
        try:
            # Get audio data
            audio_data = []
            while not self.audio_queue.empty():
                audio_data.append(self.audio_queue.get())
            
            if not audio_data:  # Skip if no audio data
                return
            
            audio_data = np.concatenate(audio_data)
            
            # Check audio level
            audio_level = np.abs(audio_data).mean()
            if audio_level < 0.01:
                logger.debug("Very low audio level detected")
                return

            # Convert to WAV for Groq transcription
            wav_buffer = io.BytesIO()
            with wave.open(wav_buffer, 'wb') as wav_file:
                wav_file.setnchannels(1)
                wav_file.setsampwidth(2)
                wav_file.setframerate(self.sample_rate)
                wav_file.writeframes((audio_data * 32767).astype(np.int16).tobytes())
            
            # Reset buffer position
            wav_buffer.seek(0)
            
            # Transcribe using Groq
            result = self.groq_client.audio.transcriptions.create(
                file=("audio.wav", wav_buffer),
                model="distil-whisper-large-v3-en",
                response_format="verbose_json",
                language="en",
                temperature=0.0,
                prompt="Transcribe the following English speech exactly as heard. If no clear speech is detected, return empty text."
            )
            
            if result and hasattr(result, 'text'):
                transcribed_text = result.text.strip()
                
                # Process the transcription to clean up repetitions
                transcribed_text = self._process_transcription(transcribed_text)
                
                current_time = time.time()
                
                # Only process if we have text and it's different from last transcription
                # or enough time has passed (3 seconds)
                if (transcribed_text and 
                    (transcribed_text != self.last_transcription or 
                     current_time - self.last_transcription_time > 3)):
                    
                    logger.info(f"Transcribed: {transcribed_text}")
                    
                    if self.callback_fn and self.main_loop:
                        # Only pass the text to the callback
                        asyncio.run_coroutine_threadsafe(
                            self.callback_fn(transcribed_text),
                            self.main_loop
                        )
                    
                    # Update last transcription
                    self.last_transcription = transcribed_text
                    self.last_transcription_time = current_time
            
        except Exception as e:
            logger.error(f"Error processing audio: {e}")
        finally:
            if 'wav_buffer' in locals():
                wav_buffer.close()

    def _audio_callback(self, indata, frames, time, status):
        """Callback for audio data"""
        try:
            if status:
                logger.warning(f"Audio callback status: {status}")
            if not self.is_recording:
                logger.debug("Callback received but recording is False")
                return
                
            # Add audio data to queue
            self.audio_queue.put(indata.copy())
            self.buffer_size += len(indata)
            
            # Process when buffer is full
            if self.buffer_size >= self.max_buffer_size:
                logger.debug(f"Buffer full (size: {self.buffer_size}), processing audio")
                self._process_audio()
                self.buffer_size = 0
                
        except Exception as e:
            logger.error(f"Error in audio callback: {e}")

    def start_recording(self):
        """Start recording audio"""
        try:
            if self.is_recording:
                logger.warning("Already recording")
                return
                
            logger.info("Starting audio recording...")
            self.is_recording = True  # Set flag before starting stream
            
            def audio_callback(indata, frames, time, status):
                if status:
                    logger.warning(f"Audio callback status: {status}")
                if not self.is_recording:
                    return
                    
                # Add audio data to queue
                self.audio_queue.put(indata.copy())
                self.buffer_size += len(indata)
                
                # Process when buffer is full
                if self.buffer_size >= self.max_buffer_size:
                    self._process_audio()
                    self.buffer_size = 0
            
            # Start the stream
            self.stream = sd.InputStream(
                device=self.device_index,
                channels=1,
                samplerate=self.sample_rate,
                callback=audio_callback,
                dtype=np.float32,
                blocksize=8192  # Increased block size
            )
            self.stream.start()
            
            logger.info("Audio recording started successfully")
            
        except Exception as e:
            logger.error(f"Error starting recording: {e}")
            self.is_recording = False
            raise

    def stop_recording(self):
        """Stop recording"""
        try:
            logger.info("Stopping recording (called from: %s)", stack()[1].function)  # Log caller
            self.is_recording = False
            
            # Stop and close the stream
            if self.stream is not None:
                self.stream.stop()
                self.stream.close()
                self.stream = None
            
            # Clear the audio queue
            while not self.audio_queue.empty():
                self.audio_queue.get()
            
            self.buffer_size = 0
            logger.info("Stopped recording")
            
        except Exception as e:
            logger.error(f"Error stopping recording: {e}")
        finally:
            self.is_recording = False

    def set_message_callback(self, callback_fn: Callable[[str], None]):
        """Set callback for transcribed messages"""
        self.callback_fn = callback_fn

    async def shutdown(self):
        """Shutdown speech manager"""
        try:
            logger.info("Shutting down speech manager...")
            
            # Stop recording if active
            if self.is_recording:
                self.stop_recording()
                
            logger.info("Speech manager shutdown complete")
            
        except Exception as e:
            logger.error(f"Error during speech manager shutdown: {e}")
            raise 

    def _process_transcription(self, text: str) -> str:
        """Clean up transcribed text to remove repetitions and stutters"""
        if not text:
            return text
            
        # Convert to lowercase for processing
        text = text.lower()
        
        # Remove duplicate words that appear more than twice in sequence
        words = text.split()
        cleaned_words = []
        prev_word = None
        repeat_count = 0
        
        for word in words:
            if word == prev_word:
                repeat_count += 1
                if repeat_count <= 2:  # Allow max 2 repetitions
                    cleaned_words.append(word)
            else:
                repeat_count = 1
                cleaned_words.append(word)
            prev_word = word
        
        # Rejoin and capitalize first letter
        cleaned_text = ' '.join(cleaned_words)
        cleaned_text = cleaned_text.capitalize()
        
        logger.debug(f"Original text: {text}")
        logger.debug(f"Cleaned text: {cleaned_text}")
        
        return cleaned_text 