import subprocess
import webbrowser

SKILL = {
    "name": "open_youtube",
    "trigger": r"(open|launch|go to) youtube",
    "description": "Opens YouTube in your default browser",
    "handler": open_youtube,
}


async def open_youtube(transcript: str, manager) -> str:
    webbrowser.open("https://youtube.com")
    return "Opening YouTube for you."
