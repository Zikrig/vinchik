import uvicorn

from config import settings
from webapp import create_app

app = create_app()

if __name__ == "__main__":
    uvicorn.run(
        "web:app",
        host=settings.web_host,
        port=settings.web_port,
        reload=False,
    )
