from fastapi import FastAPI
import json
import gzip
import os

app = FastAPI()


def load_data(category: str):
    path = f"releases/{category}.json"
    with gzip.open(path, "rt") as f:
        return json.load(f)


@app.get("/{category}")
async def get_movie(category: str, page: int = 1, per_page: int = 20):
    data = load_data(category)
    start = (page - 1) * per_page
    return data[start : start + per_page]
