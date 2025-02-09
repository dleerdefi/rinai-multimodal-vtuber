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
            self.speech_enabled = not self.speech_enabled
            if self.orchestrator:
                # Use run_coroutine_threadsafe since we're in a different thread
                asyncio.run_coroutine_threadsafe(
                    self.orchestrator.toggle_speech_input(),
                    self.main_loop
                )
            console.print(f"[yellow]Speech input {'enabled' if self.speech_enabled else 'disabled'}[/]")
        except Exception as e:
            console.print(f"[red]Error toggling speech: {e}")

    def is_speech_enabled(self):
        """Check if speech input is enabled"""
        return self.speech_enabled 