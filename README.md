# 🤖 Telegram AI Chat Bot

A feature-rich Telegram bot powered by AI, offering intelligent conversations, task management, reminders, translations, and image generation capabilities — completely free to host on Hugging Face Spaces and Render.

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Python 3.9+](https://img.shields.io/badge/python-3.9+-blue.svg)](https://www.python.org/downloads/)
[![Telegram Bot API](https://img.shields.io/badge/Telegram%20Bot%20API-Latest-blue.svg)](https://core.telegram.org/bots/api)

---

## ✨ Features

### 🧠 **AI-Powered Conversations**
- Intelligent chat with context memory and conversation history
- Automatic summarization of long conversations to maintain context
- Vision capabilities: analyze and discuss images
- Support for both private chats and group conversations with @mentions
- Persian/Farsi language support with UTF-8 encoding

### 📝 **Todo Management System**
- Create tasks with priority levels (High, Medium, Normal)
- Categorize tasks (Work, Personal, General)
- Set custom deadlines with Iran timezone (UTC+3:30) support
- Automatic deadline expiration notifications
- Collaborative task management in groups
- Interactive inline keyboard for task completion/deletion

### ⏰ **Smart Reminder System**
- Schedule reminders with flexible time formats:
  - Relative: `45m`, `2h`, `3d`
  - Absolute clock: `18:30` (Iran time)
  - Full datetime: `2026-07-15 10:00`
- Custom recurrence intervals: daily, weekly, or custom (e.g., `12h`, `3d`)
- Mention targets: notify yourself or all group members
- Snooze and dismiss functionality

### 🌐 **Translation Service**
- Professional translation between 25+ languages
- OCR + translation for images
- Context-aware and culturally accurate translations
- Support for technical terminology
- Automatic source language detection

### 🎨 **AI Image Generation**
- Generate high-quality images from text prompts
- Powered by Agnes Image 2.0 Flash model
- 1024x1024 resolution output

### ️ **Built-in Protection**
- Rate limiting (per-user and global)
- Anti-spam protection with automatic muting
- Thread-safe SQLite database with WAL mode
- Self-healing infinity polling

---

## 📋 Requirements

- Python 3.9 or higher
- Telegram Bot Token (from [@BotFather](https://t.me/botfather))
- AI API Key (see API Configuration below)

---

## 🔌 API Configuration

This bot uses the **agnes-ai.com API** for AI capabilities. You can easily adapt it to use any other compatible API endpoint:

**Current API Provider:**
- Website: [https://agnes-ai.com/](https://agnes-ai.com/)
- Sign up to get your API key
- Set the `AGNES_API_KEY` environment variable

**Using Alternative APIs:**
You can use any OpenAI-compatible API by modifying these environment variables:
- `CHAT_API_BASE` - Your chat API endpoint
- `IMAGE_API_BASE` - Your image generation API endpoint  
- `CHAT_MODEL` - Your preferred chat model name
- `IMAGE_MODEL` - Your preferred image model name

Compatible with: OpenAI, Anthropic, Google AI, Azure OpenAI, and other OpenAI-compatible endpoints.

---

## 🚀 Quick Start

### 1. Clone the Repository

```bash
git clone https://github.com/mahdikiani-2007/telegram-ai-chat-bot.git
cd telegram-ai-chat-bot
```

### 2. Install Dependencies

```bash
pip install -r requirements.txt
```

### 3. Configure Environment Variables

Copy the example environment file and fill in your credentials:

```bash
cp .env.example .env
```

Edit `.env` with your preferred text editor:

```env
# Required
TELEGRAM_TOKEN=your_telegram_bot_token_here
AGNES_API_KEY=your_agnes_api_key_here

# Optional (defaults provided)
CHAT_API_KEY=your_chat_api_key_here
IMAGE_API_KEY=your_image_api_key_here
CHAT_API_BASE=https://router.bynara.id/v1
AGNES_BASE=https://apihub.agnes-ai.com/v1
CHAT_MODEL=mimo-v2.5-free
IMAGE_MODEL=agnes-image-2.0-flash
```

**How to get your credentials:**
- **TELEGRAM_TOKEN**: Talk to [@BotFather](https://t.me/botfather) on Telegram and create a new bot
- **AGNES_API_KEY**: Sign up at [Agnes AI](https://agnes-ai.com) to get your API key (or use your own API provider)

### 4. Run the Bot

```bash
python bot.py
```

The bot will start and begin listening for messages!

---

## ☁️ Free Deployment (Hugging Face Spaces + Render)

### Option 1: Hugging Face Spaces (Recommended)

1. Create a new Space on [Hugging Face](https://huggingface.co/spaces)
2. Choose **Docker** as the SDK
3. Upload your code files
4. Set environment variables in Space Settings → Variables
5. The persistent storage directory `/data` will be automatically available

### Option 2: Render

1. Create a new Web Service on [Render](https://render.com)
2. Connect your GitHub repository
3. Set Build Command: `pip install -r requirements.txt`
4. Set Start Command: `python bot.py`
5. Add environment variables in the Render dashboard
6. Deploy!

**Note:** The bot includes a health check server on port 7860 for uptime monitoring.

---

## 📚 Available Commands

### General Commands

| Command | Description | Example |
|---------|-------------|---------|
| `/start` | Display welcome message and bot introduction | `/start` |
| `/reset` | Clear chat history and conversation memory (admin only in groups) | `/reset` |

### AI Features

| Command | Description | Example |
|---------|-------------|---------|
| **Chat** | Just send a message or mention the bot in groups | `@botname explain quantum physics` |
| `/pic [prompt]` | Generate an AI image from text description | `/pic a sunset over mountains` |
| `/tr [lang] [text]` | Translate text to target language | `/tr en سلام دنیا` |
| `/tr` (on image) | OCR and translate text from image | Caption photo with `/tr en` |

### Task Management

| Command | Description | Example |
|---------|-------------|---------|
| `/todo [task]` | Create a new todo task (interactive flow) | `/todo Buy groceries` |
| `/todos` | Display active todo list with management buttons | `/todos` |

### Reminders

| Command | Description | Example |
|---------|-------------|---------|
| `/remind [text]` | Create a new reminder (interactive flow) | `/remind Team meeting` |

---

## 🎯 Usage Examples

### Creating a Todo Task

```
/todo Finish project report
```

The bot will guide you through:
1. Select priority (High/Medium/Normal)
2. Choose category (Work/Personal/General)
3. Set deadline (optional)

### Setting a Reminder

```
/remind Call dentist
```

Interactive flow:
1. Choose trigger time (10m, 30m, 1h, 3h, tomorrow 9AM, or custom)
2. Select mention target (only me / all members)
3. Set recurrence (one-time, daily, weekly, or custom interval)

### Translation

```
# Text translation
/tr en این یک متن فارسی است

# Image translation (send photo with caption)
/tr en
```

### Image Generation

```
/pic a futuristic city at night with neon lights
```

---

## 🗄️ Database Schema

The bot uses SQLite with the following tables:

- **messages**: Conversation history
- **summaries**: Compressed conversation summaries
- **todos**: Task management data
- **reminders**: Scheduled reminders
- **chat_tracking**: Active chats tracking

All data is stored in `chat_history.db` (auto-created on first run).

---

## 🔧 Configuration Options

### Environment Variables (Advanced)

```env
# Memory & Behavior Settings
RECENT_HISTORY=8              # Number of recent messages kept verbatim
SUMMARIZE_THRESHOLD=40        # Message count before summarization
SUMMARIZE_BATCH=30            # Messages compressed per summary pass
STREAM_EDIT_INTERVAL=1.2      # Seconds between streaming updates

# Data Storage (for Hugging Face Spaces)
DATA_DIR=/data                # Persistent storage directory
```

---

## 🛠️ Development

### Project Structure

```
telegram-ai-chat-bot/
├── bot.py                 # Main bot application
├── requirements.txt       # Python dependencies
├── .env.example          # Environment variables template
├── .gitignore           # Git ignore rules
├── README.md            # This file (English)
├── README-fa.md         # Persian documentation
└── LICENSE              # MIT License
```

### Key Components

- **ChatRepository**: Thread-safe SQLite database layer
- **AgnesClient**: AI model interaction (chat, summary, images)
- **RateLimiter**: Per-user and global rate limiting
- **SpamTracker**: Anti-spam burst protection
- **Reminder Scheduler**: Background daemon for timed notifications

---

## 🤝 Contributing

Contributions are welcome! Please feel free to submit a Pull Request.

1. Fork the repository
2. Create your feature branch (`git checkout -b feature/amazing-feature`)
3. Commit your changes (`git commit -m 'Add some amazing feature'`)
4. Push to the branch (`git push origin feature/amazing-feature`)
5. Open a Pull Request

---

## 📜 License

This project is licensed under the MIT License - see the [LICENSE](LICENSE) file for details.

---

## 🙏 Acknowledgments

- [pyTelegramBotAPI](https://github.com/eternnoir/pyTelegramBotAPI) - Telegram Bot API wrapper
- [Agnes AI](https://agnes-ai.com) - Default AI API provider (you can use any compatible API)
- [Hugging Face](https://huggingface.co) - Free hosting platform
- [Render](https://render.com) - Deployment platform

---

## 📞 Support

If you encounter any issues or have questions:

1. Check the [Issues](https://github.com/mahdikiani-2007/telegram-ai-chat-bot/issues) page
2. Create a new issue with detailed description
3. Contact the maintainer: [@moai_artwork](https://t.me/moai_artwork)

---

## ⚠️ Security Notice

- **Never commit** `.env` file or `chat_history.db` to version control
- Keep your `TELEGRAM_TOKEN` and API keys confidential
- Regularly update dependencies for security patches
- Use environment variables for all sensitive configuration

---

## 🌟 Star History

If you find this project useful, please consider giving it a star ⭐

---

**Made with ❤️ by [Moai](https://t.me/moai_artwork)**
