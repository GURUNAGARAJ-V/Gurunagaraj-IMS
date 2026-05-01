from fastapi import FastAPI, BackgroundTasks, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from typing import List, Optional
import redis.asyncio as aioredis
import motor.motor_asyncio
import asyncpg
import uuid
import json
import time
import logging
from datetime import datetime
from tenacity import retry, stop_after_attempt, wait_exponential

# --- Observability: Structured Logging Setup ---
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
logger = logging.getLogger("Zeotap-IMS")

app = FastAPI(
    title="Mission-Critical IMS - Zeotap",
    description="High-throughput Incident Management System with Debouncing and strict RCA workflows.",
    version="1.0.0"
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

redis_client = None
mongo_client = None
pg_pool = None

# --- Security: Hardened Data Models ---
class Signal(BaseModel):
    component_id: str = Field(..., max_length=50, description="The ID of the failing component")
    severity: str = Field(..., max_length=20, description="Alert severity level")
    payload: dict = Field(..., description="The raw error trace data")

class RCAForm(BaseModel):
    root_cause: str = Field(..., min_length=3, max_length=500)
    fix_applied: str = Field(..., min_length=10, max_length=2000)

# --- Validation Logic (State Pattern) ---
class RCAValidator:
    @staticmethod
    def validate(rca: RCAForm) -> tuple[bool, str]:
        if not rca.root_cause.strip():
            return False, "Root cause cannot be empty whitespace."
        if len(rca.fix_applied.strip()) < 10:
            return False, "Fix applied details are too brief. Please elaborate for compliance."
        return True, "Valid"

# --- Initialization ---
@app.on_event("startup")
async def startup():
    global redis_client, mongo_client, pg_pool
    logger.info("Initializing Database Connections...")
    
    redis_client = aioredis.Redis(host='localhost', port=6379, db=0, decode_responses=True)
    mongo_client = motor.motor_asyncio.AsyncIOMotorClient("mongodb://localhost:27017")
    
    try:
        pg_pool = await asyncpg.create_pool(user='zeotap_user', password='zeotap_password', database='ims_db', host='localhost')
        async with pg_pool.acquire() as conn:
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS work_items (
                    incident_id VARCHAR PRIMARY KEY,
                    component_id VARCHAR,
                    severity VARCHAR,
                    status VARCHAR,
                    start_time TIMESTAMP,
                    end_time TIMESTAMP,
                    rca JSONB,
                    mttr_minutes FLOAT
                )
            """)
        logger.info("All systems operational. Databases connected successfully.")
    except Exception as e:
        logger.error(f"Database connection failed: {str(e)}")

# --- Database Writers with Retry Logic ---
@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10))
async def save_to_datalake(signal_dict: dict):
    db = mongo_client.ims_datalake
    signal_dict['ingest_time'] = datetime.utcnow().isoformat()
    await db.raw_signals.insert_one(signal_dict)

@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10))
async def create_work_item(incident_id: str, signal: Signal):
    start_time = datetime.now()
    async with pg_pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO work_items (incident_id, component_id, severity, status, start_time)
            VALUES ($1, $2, $3, 'OPEN', $4)
        """, incident_id, signal.component_id, signal.severity, start_time)
    
    cache_payload = {
        "incident_id": incident_id, 
        "component_id": signal.component_id,
        "severity": signal.severity, 
        "status": "OPEN", 
        "start_time": start_time.strftime("%Y-%m-%d %H:%M:%S")
    }
    await redis_client.hset("hotpath_incidents", incident_id, json.dumps(cache_payload))
    logger.warning(f"🚨 NEW INCIDENT GENERATED: {incident_id} on {signal.component_id}")

# --- API Endpoints ---
@app.post("/ingest", status_code=202)
async def ingest_signal(signal: Signal, background_tasks: BackgroundTasks):
    current_second = int(time.time())
    rate_key = f"rate_limit:{current_second}"
    req_count = await redis_client.incr(rate_key)
    if req_count == 1: 
        await redis_client.expire(rate_key, 2)
    if req_count > 5000:
        logger.error("Rate limit exceeded! Shedding load.")
        raise HTTPException(status_code=429, detail="Backpressure Active: Rate Limit Exceeded")

    debounce_key = f"debounce:{signal.component_id}"
    existing_incident_id = await redis_client.get(debounce_key)
    
    if not existing_incident_id:
        incident_id = f"INC-{str(uuid.uuid4())[:8].upper()}"
        await redis_client.set(debounce_key, incident_id, ex=10) 
        background_tasks.add_task(create_work_item, incident_id, signal)
    else:
        incident_id = existing_incident_id
        
    signal_data = signal.dict()
    signal_data["incident_id"] = incident_id
    background_tasks.add_task(save_to_datalake, signal_data)
    
    return {"status": "accepted", "incident_id": incident_id, "debounced": bool(existing_incident_id)}

@app.get("/incidents")
async def get_incidents():
    cached_data = await redis_client.hgetall("hotpath_incidents")
    return [json.loads(val) for val in cached_data.values()]

@app.get("/incidents/{incident_id}/logs")
async def get_incident_logs(
    incident_id: str, 
    limit: int = Query(50, description="Max logs to return"), 
    skip: int = Query(0, description="Logs to skip for pagination")
):
    db = mongo_client.ims_datalake
    cursor = db.raw_signals.find({"incident_id": incident_id}, {"_id": 0}).sort("ingest_time", -1).skip(skip).limit(limit)
    return await cursor.to_list(length=limit)

@app.post("/incidents/{incident_id}/close")
async def close_incident(incident_id: str, rca: RCAForm):
    is_valid, error_msg = RCAValidator.validate(rca)
    if not is_valid:
        logger.info(f"RCA Validation failed for {incident_id}: {error_msg}")
        raise HTTPException(status_code=400, detail=error_msg)
        
    end_time = datetime.now()
    async with pg_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT start_time FROM work_items WHERE incident_id = $1", incident_id)
        if not row: 
            raise HTTPException(status_code=404, detail="Incident Not Found")
            
        mttr_minutes = (end_time - row['start_time']).total_seconds() / 60.0
        
        await conn.execute("""
            UPDATE work_items SET status = 'CLOSED', end_time = $1, rca = $2, mttr_minutes = $3
            WHERE incident_id = $4
        """, end_time, json.dumps(rca.dict()), mttr_minutes, incident_id)
        
    cached_str = await redis_client.hget("hotpath_incidents", incident_id)
    if cached_str:
        incident_data = json.loads(cached_str)
        incident_data["status"] = "CLOSED"
        incident_data["mttr_minutes"] = round(mttr_minutes, 2)
        await redis_client.hset("hotpath_incidents", incident_id, json.dumps(incident_data))
        
    logger.info(f"✅ INCIDENT RESOLVED: {incident_id} | MTTR: {round(mttr_minutes, 2)} mins")    
    return {"status": "Success", "mttr_minutes": mttr_minutes}
