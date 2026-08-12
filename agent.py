"""Caspian message gateway integration.

Set ``your_agent_logic`` to call your actual agent.  The same handler services
every Caspian channel that is connected below.
"""

import os
from pathlib import Path

from caspian_sdk import CommClient
from llm_service import FeatherlessLLM
from storage import StudentPilotStore
from studentpilot_service import StudentPilotService


def get_env_setting(name: str) -> str:
    """Read a setting from the process environment or local ignored .env file."""
    if value := os.getenv(name):
        return value

    env_path = Path(__file__).with_name(".env")
    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            key, separator, value = line.partition("=")
            if separator and key.strip() == name:
                return value.strip().strip('"').strip("'")

    raise RuntimeError(f"Missing required environment variable: {name}")


client = CommClient()
llm = FeatherlessLLM(api_key=get_env_setting("FEATHERLESS_API_KEY"))
store = StudentPilotStore()
studentpilot = StudentPilotService(llm, store)


def your_agent_logic(text: str, conversation_id: str, user_id: str) -> str:
    """Use isolated, persistent context for every channel conversation."""
    return studentpilot.respond(conversation_id, user_id, text)


@client.on_message
def handle(message):
    """Reply on the originating channel while preserving its conversation."""
    sender = message.sender
    user_id = sender.get("address", "unknown") if isinstance(sender, dict) else str(sender)
    answer = your_agent_logic(message.text, message.conversation_id, user_id)
    message.reply(answer)


if __name__ == "__main__":
    telegram = client.connect_telegram(bot_token=get_env_setting("TELEGRAM_BOT_TOKEN"))
    print(f"Telegram bot connected: {telegram['address']}")
    # `client.behavior_prompt()` can be appended to your agent's system prompt.
    client.listen()
