from fastapi import FastAPI
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.middleware.cors import CORSMiddleware
import json
import gzip
import os
from math import ceil
from pathlib import Path

app = FastAPI()


app.add_middleware(GZipMiddleware, minimum_size=1000)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def load_data(category: str):
    home_path = Path.home()
    path = home_path / f"releases/{category}.json"
    with gzip.open(path, "rt") as f:
        return json.load(f)


@app.get("/{category}")
async def get_movie(
    category: str,
    page: int = 1,
    per_page: int = 20,
):

    all_data = load_data(category)
    total_results = len(all_data)
    total_pages = ceil(total_results / per_page)

    start = (page - 1) * per_page
    results = all_data[start : start + per_page]

    return {
        "page": page,
        "results": results,
        "total_pages": total_pages,
        "total_results": total_results,
    }
