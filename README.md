# RinAI Multimodal V-Tuber & Desktop Agent

![RinAI Multimodal UX](https://github.com/dleerdefi/rinai-multimodal-vtuber/blob/main/assets/images/RinAI%20Multimodal%20UX%20Example.png)

🤖 An open-source AI V-Tuber and desktop agent that combines speech processing, LLMs, and tool automation. Features include:

- 🎙️ Real-time STT/TTS with Groq & 11Labs
- 🐦 Twitter scheduling & automation
- 🧠 GraphRAG memory system
- 🔧 Extensible tool framework
- 🎮 VTube Studio integration
- 💬 YouTube chat interaction
- 💸 ElizaOS Twitter Client Integration

Built with Python, TypeScript, and modern AI services. Perfect for V-Tubing or as a powerful desktop assistant.

## Architecture Overview
![RinAI Architecture](https://github.com/dleerdefi/rinai-multimodal-vtuber/blob/main/assets/images/RinAI%20Multimodal%20Vtuber%20Diagram.png)

## ElizaOS Twitter Client Integration
This project uses a custom fork of the [ElizaOS Twitter Client](https://github.com/elizaOS/agent-twitter-client) that we've enhanced with an API server layer. Our version ([agent-twitter-client](https://github.com/dleerdefi/agent-twitter-client)) provides a REST API interface for the RinAI agent stack to schedule and manage tweets without requiring Twitter API keys.

**Key Features:**

*   **Multimodal AI:** Integrates speech-to-text, text-to-speech, large language models, and tool calling for rich and interactive conversations.
*   **Live Streaming Ready:** Designed for V-Tubing! Operate fully autonomously, engaging directly with chat or with a live host using speech-to-text.
*   **Desktop Agent:** Operate fully autonomously, engaging directly with chat or with a live host using speech-to-text.
*   **Ultra-Fast Speech Processing:** Utilizes Groq for Whisper AI, delivering lightning-fast and reliable speech-to-text transcription.
*   **Tool-Calling Powerhouse:** Equipped with tools including:
    *   **Twitter Agent:** Create and schedule tweets
    *   **Task Scheduling Agent:** Schedule tweet posting and other background tasks
    *   **Perplexity Integration:** Leverage Perplexity's DeepSeek R1 API for web queries
    *   **Cryptocurrency Price & Analytics:** Obtain live and historical crypto price data
*   **Advanced Chat Agent:**  Based on the [Rin AI Chat Agentic Chat Stack](https://github.com/dleerdefi/peak-ai-agent-stack):
    *   **GraphRAG Memory:** Graph-based memory for context-aware responses
    *   **Keyword-Based Intent Recognition:** Fast keyword extraction for memory relevance
    *   **Advanced Context Summarization:** Active summarization for maintaining conversation context
*   **Smart LLM Gateway:** Dynamically selects optimal LLM based on task complexity
*   **Streaming Architecture:** End-to-end streaming for minimal latency
*   **Open Source & Extensible:** Built to be customizable with community contributions welcome

**Tech Stack:**

*   **Backend:** Python, Node.js/TypeScript
*   **LLMs:** Role-Playing LLM, Claude 3.5
*   **Speech Processing:** Groq Whisper AI (STT), 11Labs (TTS)
*   **Frontend:** [Vtube Studio, OBS]
*   **Audio:** FFmpeg, VoiceMeeter Banana (Windows)

**Getting Started (Windows):**

1. **System Requirements:**
   * Windows 10/11
   * [VoiceMeeter Banana](https://vb-audio.com/Voicemeeter/banana.htm)
   * [VTube Studio](https://store.steampowered.com/app/1325860/VTube_Studio/)
   * [OBS Studio](https://obsproject.com/)
   * [FFmpeg](https://ffmpeg.org/download.html) (Add to PATH)

2. **Development Prerequisites:**
   * [Python 3.10+](https://www.python.org/downloads/)
   * [Node.js 18+](https://nodejs.org/)
   * [Git](https://git-scm.com/downloads)

3. **API Keys Required:**
   * Groq (Speech-to-Text)
   * 11Labs (Text-to-Speech)
   * Perplexity (Web Queries)

4. **Installation:**
   ```bash
   # Clone main repository
   git clone [https://github.com/dleerdefi/rinai-multimodal-vtuber] rinai-multimodal-vtuber
   cd rinai-multimodal-vtuber

   # Setup Python environment
   python -m venv venv
   source venv/bin/activate  # On Windows: .\venv\Scripts\activate
   pip install -r requirements.txt

   # Setup Twitter API Client
   git clone [Your Forked ElizaOS Twitter Client Repo URL] twitter-client
   cd twitter-client
   npm install
   ```

5. **Starting the Services:**

   a. Start the Twitter API Server:
   ```bash
   cd twitter-client
   npx ts-node server.ts  # Or the correct server startup file
   ```
   - Verify the server is running at [http://localhost:3000](http://localhost:3000)

   b. Start the Main RinAI Server:
   ```bash
   cd rinai-multimodal-vtuber
   python src/scripts/run_stream.py
   ```
   Follow the prompts to:
   - Choose between streaming or local agent mode
   - Select your microphone device
   - Enable/disable YouTube chat (if streaming)

   c. Access the Web Interface:
   - Open your browser to [http://localhost:8765](http://localhost:8765)
   - You should see the retro-style chat interface
   - Messages will appear as they're processed

   d. Available Hotkeys:
   - `Alt+S`: Toggle speech input
   - `Alt+P`: Pause/Resume all services
   - `Alt+Q`: Quit

Each service needs to run in its own terminal window. Make sure MongoDB and Neo4j are running before starting the services.

**Open Source and Contributions:**

We welcome contributions! To get started:
1. Fork this repository
2. Create a new branch for your feature/fix
3. Submit a Pull Request

**License:**

MIT License - see [LICENSE](LICENSE) file for details