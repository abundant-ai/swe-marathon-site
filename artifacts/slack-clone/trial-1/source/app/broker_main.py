import asyncio
from .broker import run_broker

if __name__ == "__main__":
    try:
        asyncio.run(run_broker())
    except KeyboardInterrupt:
        pass
