import asyncio
from .irc import run_irc

if __name__ == "__main__":
    try:
        asyncio.run(run_irc())
    except KeyboardInterrupt:
        pass
