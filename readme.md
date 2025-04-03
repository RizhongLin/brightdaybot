# BrightDayBot

A Slack bot that records and wishes Slack workspace members a happy birthday with AI-generated personalized messages.

## Features

- **Birthday Recording**: Users can set their birthdays via DM to the bot
- **Birthday Announcements**: Automatic birthday celebrations in a designated channel
- **AI-Generated Messages**: Personalized birthday wishes using OpenAI
- **Admin Commands**: Statistics, user management, and settings
- **Reminders**: Automatically remind users who haven't set their birthday

## Project Structure

```plaintext
brightdaybot/
├── app.py                 # Main entry point
├── config.py              # Configuration and environment settings
├── llm_wrapper.py         # OpenAI integration for messages
├── handlers/              # Slack event and command handlers
│   ├── command_handler.py # Command processing logic
│   └── event_handler.py   # Event handling logic
├── services/              # Core functionality
│   ├── birthday.py        # Birthday management logic
│   └── scheduler.py       # Scheduling functionality
└── utils/                 # Helper modules
    ├── date_utils.py      # Date handling functions
    ├── slack_utils.py     # Slack API wrapper functions
    └── storage.py         # Birthday storage functions
```

## Setup Instructions

Follow these steps to set up BrightDayBot.

### 1. Create a Slack App

1. Go to [https://api.slack.com/apps](https://api.slack.com/apps) and click "Create New App"
2. Choose "From scratch" and give it a name (e.g., "BrightDayBot")
3. Select your workspace
4. Under "Add features and functionality":
   - Enable "Socket Mode"
   - Enable "Event Subscriptions" and subscribe to:
     - `message.im` (for direct messages)
     - `team_join` (for new user onboarding)
   - Add "Bot Token Scopes" under "OAuth & Permissions":
     - `chat:write`
     - `users:read`
     - `users.profile:read`
     - `im:history`
     - `im:write`
     - `channels:read`
     - `groups:read`
     - `mpim:read`
     - `users:read.email`
5. Install the app to your workspace and copy the bot token (`xoxb-...`)
6. Generate an app-level token with `connections:write` scope and copy it (`xapp-...`)

### 2. Install Dependencies

This bot has been tested with Python 3.12, but might work with earlier versions.

Before running the bot, make sure you have generated SSL certificates within your Python installation.

```bash
# Create and activate a virtual environment
python3 -m venv venv
source venv/bin/activate

# Install dependencies
pip install -r requirements.txt
```

### 3. Configure Environment Variables

Create a .env file in the project root:

```python
SLACK_APP_TOKEN="xapp-your-app-token"
SLACK_BOT_TOKEN="xoxb-your-bot-token"
BIRTHDAY_CHANNEL_ID="C0123456789"
OPENAI_API_KEY="sk-your-openai-api-key"
OPENAI_MODEL="gpt-4o"  # Optional: defaults to gpt-4o
```

### 4. Running the Bot

Execute the main Python script:

```bash
python app.py
```

The bot will:

- Create a `birthdays.txt` file to store user birthdays
- Generate an `app.log` file for logging
- Check for today's birthdays at startup
- Schedule daily birthday checks at 8:00 AM UTC

## Usage

### User Commands

DM the bot with any of these commands:

- `help` - Show help information
- `add DD/MM` - Add birthday without year
- `add DD/MM/YYYY` - Add birthday with year
- `remove` - Remove your birthday
- `check` - Check your saved birthday
- `check @user` - Check someone else's birthday
- `test` - See a test birthday message

Or simply send a date in `DD/MM` or `DD/MM/YYYY` format.

### Admin Commands

- `admin list` - List configured admin users
- `admin add USER_ID` - Add a user as admin
- `admin remove USER_ID` - Remove admin privileges
- `list` - List upcoming birthdays
- `list all` - List all birthdays by month
- `stats` - View birthday statistics
- `remind [message]` - Send reminders to users without birthdays
- `config` - View command permissions
- `config COMMAND true/false` - Change command permissions

## Customization

### Changing Birthday Message Style

Edit the templates in llm_wrapper.py to customize:

- `TEMPLATE` - System prompt for AI-generated messages
- `BACKUP_MESSAGES` - Fallback templates when AI is unavailable
- `BIRTHDAY_INTROS`, `BIRTHDAY_MIDDLES`, etc. - Components for template messages

### Schedule Configuration

Change when birthday checks run by modifying `DAILY_CHECK_TIME` in config.py.

## License

See [LICENSE](LICENSE) for details.
