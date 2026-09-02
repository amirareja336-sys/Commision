from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from .database import Base, engine
from .routes.export import router as export_router
from .routes.records import router as records_router

Base.metadata.create_all(bind=engine)

app = FastAPI(title="Commission Records API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(records_router, prefix="/api")
app.include_router(export_router, prefix="/api")

@app.get("/health")
def health_check() -> dict[str, str]:
    return {"status": "ok"}
