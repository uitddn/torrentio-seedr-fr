import os
import asyncio
import re
from urllib.parse import quote, unquote
import httpx
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse
from seedrcc import AsyncSeedr

# ==========================================
# SECURE CONFIGURATION (Loaded from Environment)
# ==========================================
SEEDR_EMAIL = os.getenv("SEEDR_EMAIL")
SEEDR_PASSWORD = os.getenv("SEEDR_PASSWORD")

if not SEEDR_EMAIL or not SEEDR_PASSWORD:
    raise ValueError("Missing SEEDR_EMAIL or SEEDR_PASSWORD environment variables!")

MAX_SIZE_GB = 3.95  # Safe ceiling for 4 GB free Seedr storage
TORRENTIO_BASE = "https://torrentio.strem.fun"
VIDEO_EXTENSIONS = (".mp4", ".mkv", ".avi", ".mov", ".webm", ".m4v")

# (Rest of your code remains identical...)

app = FastAPI(title="torrentio-seedr-free")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

MANIFEST = {
    "id": "org.custom.torrentio.seedr.free",
    "version": "1.0.0",
    "name": "torrentio-seedr-free",
    "description": "Fetches Torrentio streams under 4GB and plays them via free Seedr.cc cloud",
    "resources": ["stream"],
    "types": ["movie", "series"],
    "idPrefixes": ["tt"],
}


def parse_size_gb(title: str) -> float | None:
    match = re.search(r"(\d+(?:\.\d+)?)\s*(GB|MB)", title, re.IGNORECASE)
    if not match:
        return None
    value = float(match.group(1))
    unit = match.group(2).upper()
    return value if unit == "GB" else value / 1024.0


def build_magnet(stream: dict) -> str | None:
    info_hash = stream.get("infoHash")
    if not info_hash:
        return None
    magnet = f"magnet:?xt=urn:btih:{info_hash}"
    for source in stream.get("sources", []):
        if source.startswith("tracker:"):
            tracker_url = source.replace("tracker:", "", 1)
            magnet += f"&tr={quote(tracker_url)}"
    return magnet


async def find_video_file(client: AsyncSeedr, folder_id: str | int = "0") -> dict | None:
    contents = await client.list_contents(folder_id=str(folder_id))
    candidates = []

    for f in contents.files:
        if f.name.lower().endswith(VIDEO_EXTENSIONS):
            candidates.append({"id": f.folder_file_id, "name": f.name, "size": f.size})

    for folder in contents.folders:
        nested = await find_video_file(client, folder.id)
        if nested:
            candidates.append(nested)

    if not candidates:
        return None
    return max(candidates, key=lambda x: x["size"])


async def clear_seedr_storage(client: AsyncSeedr):
    contents = await client.list_contents()
    for t in contents.torrents:
        await client.delete_torrent(t.id)
    for folder in contents.folders:
        await client.delete_folder(folder.id)
    for f in contents.files:
        await client.delete_file(f.folder_file_id)


@app.get("/manifest.json")
async def get_manifest():
    return MANIFEST


@app.get("/stream/{media_type}/{media_id}.json")
async def get_streams(request: Request, media_type: str, media_id: str):
    torrentio_url = f"{TORRENTIO_BASE}/stream/{media_type}/{media_id}.json"

    async with httpx.AsyncClient(timeout=15.0) as http:
        resp = await http.get(torrentio_url)
        if resp.status_code != 200:
            return {"streams": []}
        data = resp.json()

    base_url = str(request.base_url).rstrip("/")
    filtered_streams = []

    for item in data.get("streams", []):
        title = item.get("title", "")
        size_gb = parse_size_gb(title)

        if size_gb is not None and size_gb > MAX_SIZE_GB:
            continue

        magnet = build_magnet(item)
        if not magnet:
            continue

        play_url = f"{base_url}/play?magnet={quote(magnet)}"

        filtered_streams.append({
            "name": f"Seedr Free ⚡\n{item.get('name', 'Torrentio')}",
            "title": title,
            "url": play_url,
            "behaviorHints": {"notWebReady": False}
        })

    return {"streams": filtered_streams}


@app.get("/play")
async def resolve_and_play(magnet: str = Query(...)):
    magnet_uri = unquote(magnet)

    async with AsyncSeedr.from_password(SEEDR_EMAIL, SEEDR_PASSWORD) as client:
        await clear_seedr_storage(client)

        added = await client.add_torrent(magnet_uri)
        if not added.result:
            raise HTTPException(status_code=400, detail=f"Seedr rejected torrent: {added.error}")

        video_file = None
        for _ in range(45):
            await asyncio.sleep(2)
            video_file = await find_video_file(client)
            if video_file:
                break

        if not video_file:
            raise HTTPException(status_code=504, detail="Timed out waiting for Seedr download.")

        file_details = await client.fetch_file(video_file["id"])
        return RedirectResponse(url=file_details.url, status_code=302)


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 7000))
    uvicorn.run(app, host="0.0.0.0", port=port)