from fastapi import FastAPI
from backend.app.routes import webhooks

app = FastAPI(title="C-HAWQ AI Backend")

# Register the webhook routes
app.include_router(webhooks.router)