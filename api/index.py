import re
from datetime import datetime
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
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

# Global in-memory cache to avoid repeated external API lookups for the same barcodes
LOOKUP_CACHE = {}

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
        response_data = {
            "status": "found",
            "barcode": clean_code,
            "record": record,
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }
    else:
        response_data = {
            "status": "not_found",
            "barcode": clean_code,
            "record": None,
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }

    # Store in cache
    LOOKUP_CACHE[clean_code] = response_data
    
    # Print lookup result (visible in Vercel function logs)
    print(f"[Lookup Success] Barcode: {clean_code} | Status: {response_data['status'].upper()}")

    return response_data
