# beparasync/main.py

import os
from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from routes.tools        import router as tools_router
from routes.agents       import router as agents_router
from routes.files        import router as files_router
from routes.environments import router as environments_router

load_dotenv()

app = FastAPI(title="parasync backend", version="0.2.0")

_raw_origins = os.getenv("ALLOWED_ORIGINS", "http://localhost:3000")
origins = [o.strip() for o in _raw_origins.split(",") if o.strip()]

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=False,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Content-Type", "Authorization", "Accept"],
)

app.include_router(tools_router)
app.include_router(agents_router)
app.include_router(files_router)
app.include_router(environments_router)


@app.get("/health")
def health():
    return {"status": "ok"}