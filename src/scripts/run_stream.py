import asyncio
import json
from pathlib import Path
from dotenv import load_dotenv

import sounddevice as sd
import keyboard
import threading
import time
import signal
import sys
import os
from datetime import datetime

# Update the path resolution
current_dir = os.path.dirname(os.path.abspath(__file__))
project_root = os.path.dirname(os.path.dirname(current_dir))  # Points to rinai-multimodal-vtuber
sys.path.append(project_root)

# Import after sys.path modification
from src.utils.logging_config import setup_logging
from src.services.stream_orchestrator import StreamOrchestrator
from src.utils.keyboard_handler import KeyboardHandler
from rich.prompt import Confirm, IntPrompt

# Set up logging
console = setup_logging()

# Global variables
orchestrator = None
stop_event = threading.Event()

def signal_handler(signum, frame):
    """Handle shutdown signals"""
    console.print("\n[yellow]Received shutdown signal. Cleaning up...")
    stop_event.set()

def load_config():
    """Load configuration from config.json with environment variable substitution"""
    try:
        config_path = Path(__file__).parent.parent / 'config' / 'config.json'
        console.print(f"[cyan]Loading config from: {config_path}")
        
        with open(config_path) as f:
            config_str = f.read()
            
            # Replace environment variables
            for key, value in os.environ.items():
                config_str = config_str.replace(f"${{{key}}}", value)
            
            config = json.loads(config_str)
            
        # Transform config for StreamOrchestrator
        stream_config = {
            'elevenlabs_key': config['keys'][0]['EL_key'],
            'voice_id': config['EL_data'][0]['rin_voice'],
            'groq_key': config['keys'][0]['GROQ_API_KEY'],
            'novita_key': config['keys'][0]['NOVITA_API_KEY'],
            'mongo_uri': config['mongodb']['uri'],
            'enable_speech_input': config.get('enable_speech_input', False),
            'enable_youtube_chat': config.get('enable_youtube_chat', False),
            'youtube_stream_id': config.get('youtube_stream_id')
        }
        
        console.print("[green]Configuration loaded successfully")
        return stream_config
        
    except Exception as e:
        console.print(f"[red]Error loading config: {e}")
        raise

async def main():
    global orchestrator
    
    try:
        # Load config and env vars
        load_dotenv()
        config = load_config()
        
        console.print("[bold cyan]Starting VTuber Stream Services[/]")
        
        # Setup input sources
        config = setup_input_sources(config)
        
        if not (config['enable_youtube_chat'] or config['enable_speech_input']):
            console.print("[yellow]No input sources enabled. Exiting...")
            return
        
        # Initialize orchestrator with config
        orchestrator = StreamOrchestrator(config)
        
        # Initialize and start keyboard handler
        keyboard_handler = KeyboardHandler(orchestrator)
        keyboard_handler.start()
        
        console.print("\n[bold green]Services Started![/]")
        console.print("[yellow]Available Commands:[/]")
        console.print("  [yellow]Alt+S: Toggle speech input[/]")
        console.print("  [yellow]Alt+P: Pause/Resume all services[/]")
        console.print("  [yellow]Alt+Q: Quit[/]")
        
        # Start the orchestrator
        await orchestrator.start()
        
        # Main service loop
        while not keyboard_handler.is_stopped():
            if keyboard_handler.is_paused():
                await orchestrator.pause()
                while keyboard_handler.is_paused() and not keyboard_handler.is_stopped():
                    await asyncio.sleep(0.1)
                if not keyboard_handler.is_stopped():
                    await orchestrator.resume()
            await asyncio.sleep(0.1)
        
        # Proper shutdown sequence
        console.print("[yellow]Initiating shutdown sequence...")
        keyboard_handler.stop()
        if orchestrator:
            await orchestrator.shutdown()
        console.print("[green]Shutdown complete!")
        
    except Exception as e:
        console.print(f"[bold red]Critical error: {e}")
        if orchestrator:
            await orchestrator.shutdown()
        raise

def setup_input_sources(config):
    """Setup input sources based on user selection"""
    console.print("\n[bold cyan]Input Source Selection[/]")
    
    # Session Type Selection
    session_type = "local"  # Default to local
    
    is_stream = Confirm.ask(
        "[cyan]Is this a streaming session?[/]"
    )
    
    if is_stream:
        session_type = "stream"
        # YouTube Chat Selection
        config['enable_youtube_chat'] = Confirm.ask(
            "[cyan]Enable YouTube chat monitoring?[/]"
        )
        
        if config['enable_youtube_chat']:
            if not config.get('youtube_stream_id'):
                stream_id = input("\nEnter YouTube Stream ID: ").strip()
                config['youtube_stream_id'] = stream_id
                console.print("[green]YouTube chat monitoring will start automatically[/]")
        
        # Speech Input Selection
        config['enable_speech_input'] = Confirm.ask(
            "[cyan]Enable host speech input?[/]"
        )
        
        if config['enable_speech_input']:
            console.print("\n[cyan]Available Audio Input Devices:[/]")
            devices = sd.query_devices()
            for i, device in enumerate(devices):
                if device['max_input_channels'] > 0:
                    console.print(f"[green]{i}: {device['name']}[/]")
            
            device_index = IntPrompt.ask(
                "\nEnter the number of your microphone device",
                default=1
            )
            config['audio_device_index'] = device_index
    else:
        # Local agent mode
        console.print("[green]Running in local agent mode[/]")
        config['enable_speech_input'] = True  # Always enable speech for local mode
        config['enable_youtube_chat'] = False
        
        # Setup microphone for local mode
        console.print("\n[cyan]Available Audio Input Devices:[/]")
        devices = sd.query_devices()
        for i, device in enumerate(devices):
            if device['max_input_channels'] > 0:
                console.print(f"[green]{i}: {device['name']}[/]")
        
        device_index = IntPrompt.ask(
            "\nEnter the number of your microphone device",
            default=1
        )
        config['audio_device_index'] = device_index
    
    # Add session metadata to config
    config['session_type'] = session_type
    config['session_metadata'] = {
        'type': session_type,
        'youtube_enabled': config.get('enable_youtube_chat', False),
        'speech_enabled': config.get('enable_speech_input', False),
        'stream_id': config.get('youtube_stream_id'),
        'created_at': datetime.utcnow().isoformat()
    }
    
    return config

if __name__ == "__main__":
    # Set up signal handlers
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    
    try:
        # Run main async function
        asyncio.run(main())
    except KeyboardInterrupt:
        console.print("[yellow]Keyboard interrupt received...")
    except Exception as e:
        console.print(f"[bold red]Fatal error: {e}")
    finally:
        # Ensure clean exit
        console.print("[green]Exiting...")
        sys.exit(0) 