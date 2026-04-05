from fastapi import FastAPI
from contextlib import asynccontextmanager
from db.database import init_db
from api.routes import router
from scheduler import start_scheduler
import uvicorn
import structlog

structlog.configure(
    processors=[
        structlog.processors.JSONRenderer()
    ]
)

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    await init_db()
    start_scheduler()
    yield
    # Shutdown

app = FastAPI(title="MassGrowth SaaS", lifespan=lifespan)

app.include_router(router, prefix="/api")

@app.get("/")
async def root():
    return {"message": "MassGrowth API is running. Use /docs for Swagger."}

if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
