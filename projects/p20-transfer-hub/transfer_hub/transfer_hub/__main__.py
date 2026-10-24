import uvicorn

from .config import Settings

settings = Settings.from_env()

if __name__ == "__main__":
    uvicorn.run("transfer_hub.main:app", host=settings.bind, port=settings.port)
