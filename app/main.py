from fastapi import FastAPI
import json
import gzip
import os
from fastapi import Response
from math import ceil
from pathlib import Path

app = FastAPI()


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
    response: Response = None,
):
    # Устанавливаем CORS заголовки
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Methods"] = "GET"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type"

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
