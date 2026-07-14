import uvicorn
import os

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    is_dev = os.environ.get("ENV", "production").lower() == "development"
    uvicorn.run("app.main:app", host="0.0.0.0", port=port, reload=is_dev)
