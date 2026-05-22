import asyncio
import os

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles


BASE_DIR = os.path.dirname(os.path.abspath(__file__))

app = FastAPI(title="Disposal Stock Monitor")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET"],
    allow_headers=["*"],
)
app.mount("/static", StaticFiles(directory=os.path.join(BASE_DIR, "static")), name="static")


@app.get("/")
@app.get("/disposal")
async def get_disposal_page():
    with open(os.path.join(BASE_DIR, "static", "disposal.html"), "r", encoding="utf-8") as f:
        return HTMLResponse(f.read(), headers={"Cache-Control": "no-cache, no-store, must-revalidate"})


@app.get("/api/disposal/disposed")
async def api_disposal_disposed(refresh: bool = False):
    try:
        from disposal_checker import fetch_disposed_stocks

        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, fetch_disposed_stocks, refresh)
    except Exception as e:
        return {"error": str(e), "disposed": {}}


@app.get("/api/disposal/stock/{stock_id}")
async def api_disposal_stock(stock_id: str):
    try:
        from disposal_checker import check_single_stock

        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, check_single_stock, stock_id)
    except Exception as e:
        return {"error": str(e)}


if __name__ == "__main__":
    import uvicorn
    import webbrowser
    from threading import Timer

    url = "http://127.0.0.1:8001"
    print(f"Starting disposal monitor at {url}")
    Timer(1.0, lambda: webbrowser.open(url)).start()
    uvicorn.run("disposal_app:app", host="127.0.0.1", port=8001, reload=False)
