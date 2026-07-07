# main.py
import os
from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from routes.tools import router as tools_router
from routes.agents import router as agents_router

load_dotenv()

app = FastAPI(title="parasync backend", version="0.1.0")

# CORS — must be added before routers
# ALLOWED_ORIGINS in .env: comma-separated, e.g. http://localhost:3000,http://localhost:3001
_raw_origins = os.getenv("ALLOWED_ORIGINS", "http://localhost:3000")
origins = [o.strip() for o in _raw_origins.split(",") if o.strip()]

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=False,   # set to False unless you're sending cookies — fetch with JSON doesn't need it
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Content-Type", "Authorization", "Accept"],
)

app.include_router(tools_router)
app.include_router(agents_router)


@app.get("/health")
def health():
    return {"status": "ok"}