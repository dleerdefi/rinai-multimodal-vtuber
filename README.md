
## Setup & Installation

1. **Clone this repository**:
   ```bash
   git clone https://github.com/yourusername/my-rin-vtuber-stack.git
   cd my-rin-vtuber-stack
   ```

2. **Create and activate a virtual environment**:
   ```bash
   python -m venv venv
   source venv/bin/activate  # Linux/Mac
   # or
   venv\Scripts\activate     # Windows
   ```

3. **Install dependencies**:
   ```bash
   pip install -r requirements.txt
   ```

## 🛠 **5. Handling System-Level Dependencies**

Certain functionalities require external tools or system-level dependencies.

### **a. Installing `mpv`**

The `VoiceManager` relies on `mpv` for streaming audio. Ensure `mpv` is installed on your system.

**Installation Instructions:**

- **Windows:**
  1. Download the latest `mpv` binary from [mpv-win-build](https://sourceforge.net/projects/mpv-player-windows/).
  2. Extract the contents and add the `mpv` directory to your system's `PATH`.

- **macOS:**
  ```bash
  brew install mpv
  ```

- **Linux (Debian/Ubuntu):**
  ```bash
  sudo apt update
  sudo apt install mpv
  ```

**Verification:**

```bash
mpv --version
```

### **b. Installing `ffmpeg`**

The `VoiceManager` uses `ffmpeg` for audio processing. Ensure `ffmpeg` is installed on your system.

**Installation Instructions:**

- **Windows:**
  - Download the latest `ffmpeg` binary from [FFmpeg official site](https://ffmpeg.org/download.html).
  - Extract the contents and add the `bin` directory to your system's `PATH`.

- **macOS:**
  ```bash
  brew install ffmpeg
  ```

4. **Set up environment variables**:
   - Copy `.env.example` to `.env` (if provided), or create your own `.env`.
   - Include keys for ElevenLabs, OpenAI, YouTube, Twitter, etc.

5. **Configure your [config.json](config/config.json)**:
   - Update the keys, TTS voice IDs, YouTube stream IDs, etc.

## Usage

1. **Start the Orchestrator**:
   ```bash
   python src/run_stream_v2.py
   ```
   - Follow prompts to enable YouTube chat or speech input.
   - The application will connect to relevant APIs (e.g., ElevenLabs, OpenAI) based on your `.env` and `config.json`.

2. **Keyboard Commands**:
   - **Alt+S**: Toggle speech input recording on/off.
   - **Alt+P**: Pause/resume all services (YouTube chat, speech).
   - **Alt+Q**: Quit the application.

3. **WebSocket Connections**:
   - By default, a WebSocket server runs on localhost:8765/ws.
   - Connect any frontend to this address to receive real-time chat and TTS messages.

## Environment Variables

- **OPENAI_API_KEY**: Required for LLM requests to OpenAI (if used).  
- **ELEVENLABS_API_KEY**: Required for ElevenLabs text-to-speech.  
- **GROQ_API_KEY**: Required for the Groq speech manager.  
- **TWITTER_API_KEY / SECRET**: Required for the ElizaOS Twitter agent.  
- **...etc...**

*Note:* Some services (like Twitter) may require additional keys & tokens (e.g., Access Token, Access Token Secret).

## Extending the Stack

- **GraphRAG Integration**: You can enable advanced memory and knowledge retrieval by connecting a Neo4j instance.  
- **Tool Orchestration**: Add new tools under `src/tools/`, register them in `orchestrator.py`, and define triggers in your agent logic.  
- **Custom Chat UIs**: Use the WebSocket server to build interactive browser-based or OBS-based overlays.

## License

[MIT License](LICENSE) © 2025 dleerdefi & Aggregated AI

---

Enjoy building with the **Rin AI Vtuber Stack**!