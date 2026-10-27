"""Run the leaderboard locally using GITHUB_TOKEN from the environment."""
import os

import uvicorn

if __name__ == "__main__":
    if not os.getenv("GITHUB_TOKEN", "").strip():
        raise SystemExit("Missing GITHUB_TOKEN. Set it in your environment before starting the app.")
    uvicorn.run("main:app", host="127.0.0.1", port=8774)
