import os
import re
from datetime import datetime
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
import requests

from core.lookup import lookup_product, make_session

app = FastAPI(title="Smart Barcode & QR Code Food Tracker API")

# Enable CORS for development flexibility
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Global in-memory cache to avoid repeated external API lookups for the same barcodes during runtime
LOOKUP_CACHE = {}

# File path for scan logs
LOG_FILE = "scanned_products.txt"

def append_to_log(barcode: str, status: str, source: str, product_name: str):
    """Appends a scan entry to scanned_products.txt matching the desktop app format."""
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    entry = (f"[{timestamp}] Barcode: {barcode} | Status: {status.upper()} | "
             f"DbSource: {source} | Product: {product_name}\n")
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(entry)
        print(f"[API Log Saved] {entry.strip()}")
    except Exception as e:
        print(f"[API Error] Failed to write log: {e}")

@app.get("/api/lookup")
async def get_barcode_info(barcode: str = Query(..., description="The barcode to lookup")):
    # Clean barcode
    clean_code = re.sub(r"\s+", "", barcode)
    if not clean_code:
        raise HTTPException(status_code=400, detail="Barcode cannot be empty")

    # Check cache
    if clean_code in LOOKUP_CACHE:
        print(f"[Cache Hit] Barcode {clean_code} found in cache")
        return LOOKUP_CACHE[clean_code]

    # Perform cascading lookup
    print(f"[API Lookup] Querying cascade for barcode: {clean_code}")
    try:
        session = make_session()
        record = lookup_product(clean_code, session, trace=True)
    except Exception as e:
        print(f"[API Error] Lookup failed: {e}")
        raise HTTPException(status_code=500, detail=f"Internal lookup error: {str(e)}")

    if record:
        status = "found"
        product_name = record.get("display", record.get("name", "Unknown Product"))
        source = record.get("source", "Unknown")
        response_data = {
            "status": "found",
            "barcode": clean_code,
            "record": record,
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }
    else:
        status = "not_found"
        product_name = "Product Not Found"
        source = "None"
        response_data = {
            "status": "not_found",
            "barcode": clean_code,
            "record": None,
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }

    # Store in cache
    LOOKUP_CACHE[clean_code] = response_data

    # Log to file
    append_to_log(clean_code, status, source, product_name)

    return response_data

@app.get("/api/history")
async def get_scan_history(limit: int = Query(25, description="Number of recent scans to return")):
    """Parses scanned_products.txt and returns the most recent scan records."""
    if not os.path.exists(LOG_FILE):
        return []

    history = []
    log_pattern = re.compile(
        r"^\[(?P<timestamp>[^\]]+)\]\s+Barcode:\s+(?P<barcode>\S+)\s+\|\s+Status:\s+(?P<status>\S+)\s+\|\s+DbSource:\s+(?P<source>[^|]+?)\s+\|\s+Product:\s+(?P<product>.+)$"
    )

    try:
        with open(LOG_FILE, "r", encoding="utf-8") as f:
            lines = f.readlines()

        # Parse from the end of the file to get newest first
        for line in reversed(lines):
            line = line.strip()
            if not line:
                continue
            match = log_pattern.match(line)
            if match:
                gd = match.groupdict()
                history.append({
                    "timestamp": gd["timestamp"],
                    "barcode": gd["barcode"],
                    "status": gd["status"].lower(),
                    "source": gd["source"].strip(),
                    "product": gd["product"].strip()
                })
                if len(history) >= limit:
                    break
    except Exception as e:
        print(f"[API Error] Failed to read history: {e}")
        raise HTTPException(status_code=500, detail="Failed to read scan history")

    return history

# Serve Frontend static files.
# Make sure we mount this AFTER defining API endpoints so that they are not overridden.
# If static folder does not exist, we will create it.
public_dir = os.path.join(os.path.dirname(__file__), "public")
if not os.path.exists(public_dir):
    os.makedirs(public_dir)

app.mount("/", StaticFiles(directory=public_dir, html=True), name="static")
