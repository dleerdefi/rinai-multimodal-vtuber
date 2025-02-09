import asyncio
import os
import sys
import tempfile
import wave
from pathlib import Path

# Add src directory to Python path
src_path = str(Path(__file__).parent.parent)
if src_path not in sys.path:
    sys.path.append(src_path)

from dotenv import load_dotenv
import sounddevice as sd
import numpy as np
from rich.console import Console
from groq import Groq

console = Console()

async def record_and_transcribe(duration=5, sample_rate=16000, device_index=1):
    """Record audio and transcribe using Groq Whisper"""
    temp_wav = None
    try:
        console.print(f"[yellow]Recording for {duration} seconds...[/]")
        console.print("[yellow]Speak clearly when you see 'Recording...'[/]")
        
        # Add a small delay before recording
        await asyncio.sleep(0.5)
        console.print("[bold green]Recording...[/]")
        
        # Record audio
        recording = sd.rec(
            int(duration * sample_rate),
            samplerate=sample_rate,
            channels=1,
            dtype=np.float32,
            device=device_index
        )
        sd.wait()
        
        # Check if there's enough audio signal
        audio_level = np.abs(recording).mean()
        if audio_level < 0.01:  # Adjust threshold as needed
            console.print("[yellow]Warning: Very low audio level detected. Please speak louder.[/]")
        
        # Create temp file
        temp_wav = tempfile.NamedTemporaryFile(suffix='.wav', delete=False)
        temp_wav_path = temp_wav.name
        temp_wav.close()
        
        # Save to WAV file
        with wave.open(temp_wav_path, 'wb') as wav_file:
            wav_file.setnchannels(1)
            wav_file.setsampwidth(2)
            wav_file.setframerate(sample_rate)
            wav_file.writeframes((recording * 32767).astype(np.int16).tobytes())
        
        # Initialize Groq client
        client = Groq(api_key=os.getenv('GROQ_API_KEY'))
        
        # Try the English-optimized model
        console.print(f"\n[cyan]Transcribing with distil-whisper-large-v3-en...[/]")
        with open(temp_wav_path, 'rb') as audio_file:
            transcript = client.audio.transcriptions.create(
                file=(os.path.basename(temp_wav_path), audio_file),
                model="distil-whisper-large-v3-en",  # English-optimized model
                response_format="verbose_json",
                language="en",
                temperature=0.0,
                prompt="Transcribe the following English speech exactly as heard. If no speech is detected, return empty text. Do not make assumptions or add words that aren't clearly spoken."
            )
        
        # Print metadata for debugging
        if isinstance(transcript, dict):
            segments = transcript.get('segments', [])
        else:
            segments = getattr(transcript, 'segments', [])
        
        total_confidence = 0
        segment_count = 0
        
        for segment in segments:
            console.print(f"\n[cyan]Segment Metadata:[/]")
            if isinstance(segment, dict):
                confidence = segment.get('avg_logprob', 0)
                no_speech = segment.get('no_speech_prob', 1)
                compression = segment.get('compression_ratio', 0)
                text = segment.get('text', '').strip()
                
                total_confidence += confidence
                segment_count += 1
                
                console.print(f"Confidence: {confidence}")
                console.print(f"No Speech Prob: {no_speech}")
                console.print(f"Compression Ratio: {compression}")
                console.print(f"Text: {text}")
                
                # Warning for potentially hallucinated segments
                if confidence < -1 or no_speech > 0.5:
                    console.print("[yellow]Warning: This segment might be unreliable[/]")
        
        # Get the transcription text
        if isinstance(transcript, dict):
            text = transcript.get('text', '').strip()
        else:
            text = getattr(transcript, 'text', '').strip()
        
        # Only return text if we have reasonable confidence
        if segment_count > 0:
            avg_confidence = total_confidence / segment_count
            if avg_confidence < -1:
                console.print("[yellow]Warning: Low confidence in transcription[/]")
                if not text:
                    return "No clear speech detected"
        
        return text if text else "No clear speech detected"
            
    except Exception as e:
        console.print(f"[bold red]Error: {str(e)}[/]")
        return None
        
    finally:
        if temp_wav is not None:
            try:
                os.unlink(temp_wav.name)
            except Exception as e:
                console.print(f"[yellow]Warning: Could not delete temp file: {e}[/]")

async def test_stt():
    try:
        load_dotenv()
        
        if not os.getenv('GROQ_API_KEY'):
            console.print("[bold red]Error: GROQ_API_KEY not found in environment variables[/]")
            return
        
        # List available audio devices
        devices = sd.query_devices()
        console.print("\n[cyan]Available Audio Input Devices:[/]")
        for i, device in enumerate(devices):
            if device['max_input_channels'] > 0:
                console.print(f"[green]{i}: {device['name']}[/]")
        
        # Get device selection
        device_index = int(input("\nEnter the number of your microphone device: "))
        
        while True:
            console.print("\n[bold cyan]Speech-to-Text Test[/]")
            console.print("[yellow]Press Enter to start recording (5 seconds)[/]")
            console.print("[yellow]Or type 'q' to quit[/]")
            
            choice = input()
            if choice.lower() == 'q':
                break
            
            # Record and transcribe
            results = await record_and_transcribe(
                duration=5,
                device_index=device_index
            )
            
            if results:
                console.print("\n[bold green]Transcription Results:[/]")
                console.print(f"[white]{results}[/]")
                
    except Exception as e:
        console.print(f"[bold red]Error: {str(e)}[/]")

if __name__ == "__main__":
    try:
        asyncio.run(test_stt())
    except KeyboardInterrupt:
        console.print("\n[yellow]Test terminated by user[/]")
    except Exception as e:
        console.print(f"[bold red]Fatal error: {str(e)}[/]") 