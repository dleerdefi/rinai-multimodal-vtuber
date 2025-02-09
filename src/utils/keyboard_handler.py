import keyboard
import threading
import asyncio
import time
from typing import Optional
from rich.console import Console

console = Console()

class KeyboardHandler:
    def __init__(self, orchestrator):
        self.orchestrator = orchestrator
        self.stopped = False
        self.paused = False
        self.speech_enabled = False
        self.main_loop = asyncio.get_event_loop()
        self.processing_tool = False
        self._speech_lock = threading.Lock()  # Add lock for speech state
        self._last_toggle_time = 0  # Add debounce timestamp
        
        # Set up keyboard hooks
        keyboard.add_hotkey('alt+q', self.stop)
        keyboard.add_hotkey('alt+p', self.toggle_pause)
        keyboard.add_hotkey('alt+s', self.toggle_speech)
        
    def stop(self):
        """Stop all services and set stop flag"""
        print("\nQuit command received...")
        self.stopped = True
        self.orchestrator.running = False  # Signal orchestrator to stop
        
    def is_stopped(self):
        return self.stopped

    def start(self):
        """Start keyboard handler thread"""
        self._thread = threading.Thread(
            target=self._handle_commands,
            daemon=True
        )
        self._thread.start()

    def _handle_commands(self):
        """Handle keyboard commands for controlling the stream"""
        while not self.stopped:
            try:
                time.sleep(0.1)  # Prevent CPU overuse
            except Exception as e:
                console.print(f"[red]Keyboard handler error: {e}")

    def is_paused(self) -> bool:
        """Check if pause event is set"""
        return self.paused

    def toggle_pause(self):
        """Toggle pause state"""
        self.paused = not self.paused
        console.print(f"[yellow]Services {'paused' if self.paused else 'resumed'}[/]")

    def toggle_speech(self):
        """Toggle speech input"""
        try:
            with self._speech_lock:
                # Don't disable speech when processing tool commands
                if self.processing_tool:
                    console.print("[yellow]Tool processing in progress - speech remains enabled[/]")
                    return
                
                if self.orchestrator:
                    asyncio.run_coroutine_threadsafe(
                        self.orchestrator.toggle_speech_input(),
                        self.main_loop
                    )
        except Exception as e:
            console.print(f"[red]Error toggling speech: {e}")

    def is_speech_enabled(self):
        """Check if speech input is enabled"""
        return self.speech_enabled

    def set_tool_processing(self, processing: bool):
        """Set tool processing state without affecting speech"""
        with self._speech_lock:
            self.processing_tool = processing
            # Don't modify speech state here - let it continue recording

    def set_tool_processing(self, processing: bool):
        """Set tool processing state"""
        with self._speech_lock:  # Use lock here too
            self.processing_tool = processing
            # Don't disable speech when tool processing starts
            if not processing and self.speech_enabled:
                # Only re-enable if it was enabled before
                asyncio.run_coroutine_threadsafe(
                    self.orchestrator.toggle_speech_input(),
                    self.main_loop
                ) 