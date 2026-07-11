# beparasync/main.py
import os
from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware

from routes.tools        import router as tools_router
from routes.agents       import router as agents_router
from routes.files        import router as files_router
from routes.environments import router as environments_router
from routes.agent_config import router as agent_config_router
from routes.chat         import router as chat_router

load_dotenv()

app = FastAPI(title="parasync backend", version="0.4.0")

_raw_origins = os.getenv("ALLOWED_ORIGINS", "http://localhost:3000")
origins = [o.strip() for o in _raw_origins.split(",") if o.strip()]

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=False,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Content-Type", "Authorization", "Accept"],
)


@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    origin  = request.headers.get("origin", "*")
    allowed = origin if origin in origins else (origins[0] if origins else "*")
    return JSONResponse(
        status_code=500,
        content={"detail": str(exc)},
        headers={"Access-Control-Allow-Origin": allowed},
    )


app.include_router(tools_router)
app.include_router(agents_router)
app.include_router(files_router)
app.include_router(environments_router)
app.include_router(agent_config_router)
app.include_router(chat_router)


@app.get("/health")
def health():
    return {"status": "ok"}