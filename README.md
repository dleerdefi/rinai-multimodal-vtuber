# RinAI Multi-Modal V-Tuber and Desktop Agent

**Key Features:**

*   **Multi-Modal AI:** Integrates speech-to-text, text-to-speech, large language models, and tool calling for rich and interactive conversations.
*   **Live Streaming Ready:** Designed for V-Tubing!  Operate with a live host (using speech-to-text) or autonomously, engaging directly with chat.
*   **Ultra-Fast and Stable Speech Processing:** Utilizes **Groq for Whisper AI**, delivering lightning-fast and reliable speech-to-text transcription.
*   **Tool-Calling Powerhouse:** Equipped with a growing suite of tools, including:
    *   **Twitter Agent:** Create and schedule tweets. (Powered by a customized [Eliza OS Twitter Agent Client](link-to-original-eliza-os-repo))
    *   **Advanced Scheduling Agent:** Schedule tweet posting and other background tasks.
    *   **Time Zone Conversion:** Instantly get time zones for anywhere in the world.
    *   **Global Weather Information:** Access real-time weather data for any location.
    *   **Perplexity Integration (Deep Web Queries):** Leverage Perplexity's powerful DeepSeek R1 API for web queries and in-depth research.
    *   **Cryptocurrency Price & Analytics:** Obtain live and historical crypto price data.
    *   **And more tools coming soon!**
*   **Advanced Chat Agent (Rin AI Inspired):**  Based on the [Rin AI Chat Agentic Chat Stack](link-to-rin-ai-repo) with latency optimizations:
    *   **Graph Memory:** Utilizes graph memory for context-aware and enriched responses.
    *   **Keyword-Based Intent Recognition:** Fast keyword extraction for memory relevance and tool call triggering (prioritizing speed).
    *   **Advanced Context Summarization:** Active summarization powered by LLMs for maintaining conversation context.
*   **Smart LLM Gateway:** Dynamically selects the optimal LLM based on task complexity:
    *   **Uncensored Role-Playing LLM:** For engaging and creative chat interactions.
    *   **Claude 3.5:** For tool calls and tasks requiring advanced reasoning and power.
*   **Streaming Architecture:**  Leverages streaming for both LLM responses and Text-to-Speech (where possible) to minimize latency and enhance real-time responsiveness.
*   **Open Source & Extensible:**  Built to be open and customizable. We encourage community contributions!

**Architecture Overview:**

RinAI is built upon a modular architecture:

*   **V-Tuber Front-End:**  [Describe your V-Tuber Avatar setup - e.g., YouTube Studio integration]
*   **Python WebSocket Server (Core Logic):**
    *   Handles WebSocket communication with the front-end.
    *   Orchestrates the multi-modal pipeline.
    *   Manages task scheduling and tool calls.
    *   Implements keyword-based intent recognition and memory management (Rin AI inspired).
    *   Integrates with the LLM Gateway.
*   **Twitter API Server (Node.js/TypeScript):**
    *   Based on a customized **Eliza OS Twitter Agent Client** ([link-to-your-forked-repo-if-you-have-one], original: [link-to-original-eliza-os-repo]).
    *   Provides an API for creating and scheduling tweets.
    *   [Mention authentication approach - e.g., Browser Cookies initially, aiming for Bearer Token/OAuth in future]
*   **LLM Gateway (Python):**
    *   Intelligently routes queries to either the Role-Playing LLM or Claude 3.5 based on task requirements.
*   **STT/TTS Pipeline:**
    *   **Speech-to-Text:** Groq for Whisper AI (ultra-fast streaming).
    *   **Text-to-Speech:** 11Labs (streaming capabilities).
*   **Rin AI Chat Agent Core:**  Leverages graph memory and context management inspired by the [Rin AI Chat Agentic Chat Stack](link-to-rin-ai-repo).
*   **Tool Modules:**  Individual Python modules for each tool (Weather, Timezone, Crypto, Perplexity, Trading [upcoming], etc.).

**Detailed Feature Breakdown:**

*   **Tweet Creation & Scheduling:**
    *   Voice-command driven tweet creation and scheduling.
    *   Interactive agentic process for refining tweet content (suggestion, revision, approval).
    *   Schedule tweets for posting throughout the day, leveraging real-time LLM content generation for up-to-date tweets.
    *   Powered by a customized Eliza OS Twitter Agent Client (separate setup required - see instructions below).

*   **Scheduling Agent (General Task Scheduling):**
    *   Extensible scheduling framework for managing background tasks beyond just tweets.
    *   Currently demonstrated with Twitter scheduling but designed for broader application (e.g., crypto trading scheduling in upcoming features).

*   **Tool Calling (Keyword-Triggered):**
    *   Fast keyword extraction triggers tool calls, minimizing latency compared to LLM-based intent classification for tools.
    *   Growing suite of tools for information retrieval, utility functions, and external service integrations:
        *   **Time Zone Converter:** Get time zone information and convert times between locations.
        *   **Weather Tool:**  Retrieve current weather conditions and forecasts for any location.
        *   **Crypto Price Tool:** Fetch real-time and historical cryptocurrency price data.
        *   **Perplexity Web Search (DeepSeek R1):** Conduct comprehensive web searches and access relevant, current information.

*   **Optimized Chat Agent (Latency-Focused Rin AI Inspired):**
    *   Prioritizes speed and responsiveness in chat interactions.
    *   Keyword-based approach for memory retrieval and tool call triggering minimizes LLM calls in core chat flow.
    *   Graph memory and advanced context summarization (powered by LLMs) for rich conversational history.

*   **Smart LLM Gateway:**
    *   Intelligent LLM routing for optimal performance and task suitability.
    *   Uncensored Role-Playing LLM for general chat and creative responses.
    *   Claude 3.5 for tasks demanding advanced reasoning, tool execution, and complex queries.

*   **Streaming for Low Latency:**
    *   End-to-end streaming pipeline (where APIs allow) for minimal latency:
        *   Groq Whisper STT streaming.
        *   Streaming LLM text generation.
        *   11Labs Streaming TTS.
    *   Resulting in a faster, more fluid, and real-time interactive experience.

**Tech Stack:**

*   **Python:** Core backend logic, WebSocket server, LLM Gateway, Tool Modules, Rin AI Chat Agent components.
*   **Node.js/TypeScript:** Twitter API Server (customized Eliza OS Twitter Agent Client).
*   **Large Language Models:**
    *   Uncensored Role-Playing LLM [Specify which one if you are recommending a specific model]
    *   Claude 3.5 (Anthropic)
*   **Speech-to-Text:** Groq for Whisper AI API
*   **Text-to-Speech:** 11Labs Streaming API
*   **Latency Optimized RinAI Chat Agent Framework:** Rin AI Chat Agentic Chat Stack ([link-to-rin-ai-repo]) - adapted and optimized.
*   **Twitter Agent Client:** Customized Eliza OS Twitter Agent Client with API server ([link-to-your-forked-repo-if-you-have-one], original: [link-to-original-eliza-os-repo]).

*   **Task Scheduling:** [Specify scheduling library - e.g., `schedule`, `APScheduler`, or `Celery`]
*   **Frontend Technologies:** [Describe your frontend tech - e.g., HTML, CSS, JavaScript, WebSocket API]
*   **V-Tubing Software:** [Vtube Studio, OBS]
*   **Audio Routing:**  FFmpeg, VoiceMeeter Banana (for audio management on Microsoft systems - initial focus).

**Upcoming Features:**

*   **Cryptocurrency Trading Agent:**  Voice-controlled scheduled crypto trading integration with on-chain trading agent (Uniswap V3, Base network initially).  [Mention plans for open-sourcing trading agent if applicable].
*   [Add any other planned features here]

**Getting Started (Microsoft-Based Setup - Initial Focus):**

**Please note:** Initial setup instructions are currently focused on Microsoft-based systems.  Documentation for other platforms may be added in the future.

1.  **Prerequisites:**
    *   [List software prerequisites: Python, Node.js, npm/yarn, Git, FFmpeg, Virtual Studio, VoiceMeeter Banana, Groq API Key, 11Labs API Key, Perplexity API Key (if using), Twitter API App credentials (if setting up Twitter tool)]
2.  **Clone the RinAI Repository:**
    ```bash
    git clone [Your Main RinAI Repo URL] rin-ai
    cd rin-ai
    ```

3.  **Set up Python Backend Environment:** [Detailed instructions for setting up Python environment, installing Python dependencies - link to a separate SETUP.md or similar]
4.  **Set up Twitter API Server (and Eliza OS Twitter Agent):** [Detailed instructions for setting up the Node.js Twitter API server, including cloning your forked Eliza OS repo, installing Node.js dependencies, configuring Twitter authentication - link to a separate TWITTER_SETUP.md or similar]
5.  **Configure API Keys and Secrets:** [Instructions on where to securely store API keys (Groq, 11Labs, Perplexity, Twitter API credentials), using environment variables or a config file]
6.  **Run the RinAI Backend:** [Instructions to start the Python WebSocket server]
7.  **Set up and Run the Frontend:** [Instructions for setting up and running the frontend UI]
8.  **Configure V-Tubing Software:** [Instructions for setting up Virtual Studio, VoiceMeeter Banana, and your chosen V-Tubing platform (e.g., YouTube Studio) for audio routing and avatar integration]
9.  **Start Interacting!**

**Detailed Setup Guides:**

*   [Link to detailed Python Backend Setup Guide (e.g., `SETUP.md`)]
*   [Link to detailed Twitter Agent API Server Setup Guide (e.g., `TWITTER_SETUP.md`)]
*   [Link to detailed Frontend Setup Guide (e.g., `FRONTEND_SETUP.md`)]
*   [Link to detailed V-Tubing Software Setup Guide (e.g., `VTUBING_SETUP.md`)]

**Open Source and Contributions:**

RinAI is an open-source project! We welcome contributions of all kinds:

*   **Code Contributions:** Bug fixes, new features, tool integrations, performance optimizations.
*   **Documentation Improvements:**  Clarity, completeness, translations.
*   **Bug Reports:**  Help us find and fix issues.
*   **Feature Requests:**  Share your ideas for new features and tools.
*   **Community Support:** Help other users in the community forums/channels.

Please see our `CONTRIBUTING.md` file for guidelines on how to contribute.

**License:**

[Specify your project's license - e.g., MIT License, Apache 2.0 License.  Add a LICENSE.md file in your repository with the full license text.]

**Questions and Support:**

For questions, bug reports, feature requests, or to connect with the community, please [link to your project's issue tracker, forum, Discord/Slack channel, etc.].

---

**We're excited to see what you build with RinAI!**