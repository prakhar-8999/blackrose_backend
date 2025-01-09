from fastapi import FastAPI, HTTPException, Depends, WebSocket
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm
from pydantic import BaseModel
from typing import Optional, List
import jwt
import random
import asyncio
import aiosqlite
import pandas as pd
import csv
import shutil
from datetime import datetime, timedelta
import os
from pathlib import Path

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

SECRET_KEY = "qwsdhytrdesdfghjkjhgfdfgyuik"
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_DAYS = 365

DATABASE_PATH = "app.db"
CSV_PATH = "backend_table.csv"
BACKUP_PATH = "backend_table.backup.csv"

class Token(BaseModel):
    access_token: str
    token_type: str

class User(BaseModel):
    username: str
    password: str

class CSVRecord(BaseModel):
    user: str
    broker: str
    API_key: str
    API_secret: str
    pnl: float
    margin: float
    max_risk: float

    class Config:
        from_attributes = True

async def init_db():
    async with aiosqlite.connect(DATABASE_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS sessions (
                username TEXT PRIMARY KEY,
                token TEXT
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS random_numbers (
                timestamp TEXT PRIMARY KEY,
                value REAL
            )
        """)
        await db.commit()

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="token")

def create_access_token(data: dict):
    to_encode = data.copy()
    expire = datetime.utcnow() + timedelta(days=ACCESS_TOKEN_EXPIRE_DAYS)
    to_encode.update({"exp": expire})
    encoded_jwt = jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)
    return encoded_jwt

async def get_current_user(token: str = Depends(oauth2_scheme)):
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        username: str = payload.get("sub")
        if username is None:
            raise HTTPException(status_code=401)
        return username
    except jwt.JWTError:
        raise HTTPException(status_code=401)

async def generate_random_numbers():
    try:
        async with aiosqlite.connect(DATABASE_PATH) as db:
            timestamp = datetime.now().isoformat()
            value = random.random()
            await db.execute(
                "INSERT INTO random_numbers (timestamp, value) VALUES (?, ?)",
                (timestamp, value)
            )
            await db.commit()
        await asyncio.sleep(1)
    except Exception as e:
        print(f"Error generating random number: {e}")
        await asyncio.sleep(1)

class FileLock:
    def __init__(self):
        self.locked = False

    async def acquire(self):
        while self.locked:
            await asyncio.sleep(0.1)
        self.locked = True

    async def release(self):
        self.locked = False

file_lock = FileLock()

@app.post("/token")
async def login(form_data: OAuth2PasswordRequestForm = Depends()):
    access_token = create_access_token(data={"sub": form_data.username})
    async with aiosqlite.connect(DATABASE_PATH) as db:
        await db.execute(
            "INSERT OR REPLACE INTO sessions (username, token) VALUES (?, ?)",
            (form_data.username, access_token)
        )
        await db.commit()
    return {"access_token": access_token, "token_type": "bearer"}

@app.websocket("/ws/random-numbers")
async def websocket_endpoint(websocket: WebSocket, token: str):
    await websocket.accept()
    try:
        await get_current_user(token)
        while True:
            async with aiosqlite.connect(DATABASE_PATH) as db:
                await generate_random_numbers()
                async with db.execute("SELECT timestamp, value FROM random_numbers ORDER BY timestamp DESC LIMIT 1") as cursor:
                    row = await cursor.fetchone()
                    if row:
                        await websocket.send_json({
                            "timestamp": datetime.now().isoformat(),
                            "value": random.random()
                        })
                    else:
                        print("Cannot generate timestamp!")
                        await websocket.close()
            await asyncio.sleep(1)
    except Exception as e:
        print(f"WebSocket error: {e}")
        await websocket.close()

@app.get("/csv")
async def get_csv(current_user: str = Depends(get_current_user)):
    try:
        df = pd.read_csv(CSV_PATH)
        return df.to_dict('records')
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/csv")
async def create_record(record: CSVRecord, current_user: str = Depends(get_current_user)):
    await file_lock.acquire()
    try:
        shutil.copy2(CSV_PATH, BACKUP_PATH)
        df = pd.read_csv(CSV_PATH)
        new_row = pd.DataFrame([record.dict()])
        df = pd.concat([df, new_row], ignore_index=True)
        df.to_csv(CSV_PATH, index=False)
        return {"message": "Record created successfully"}
    finally:
        await file_lock.release()

@app.put("/csv/{user_id}")
async def update_record(user_id: str, record: CSVRecord, current_user: str = Depends(get_current_user)):
    await file_lock.acquire()
    try:
        shutil.copy2(CSV_PATH, BACKUP_PATH)
        df = pd.read_csv(CSV_PATH)
        user_mask = df['user'] == user_id
        if not user_mask.any():
            raise HTTPException(status_code=404, detail="Record not found")
        record_dict = record.dict()
        print(record_dict)
        for col in df.columns:
            if col in record_dict:
                print(col)
                print(record_dict[col])
                df.loc[user_mask, col] = record_dict[col]
        df.to_csv(CSV_PATH, index=False)
        return {"message": "Record update   d successfully"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        await file_lock.release()

@app.delete("/csv/{record_id}")
async def delete_record(record_id: str, current_user: str = Depends(get_current_user)):
    await file_lock.acquire()
    try:
        shutil.copy2(CSV_PATH, BACKUP_PATH)
        df = pd.read_csv(CSV_PATH)
        if record_id not in df["user"].astype(str).values:
            raise HTTPException(status_code=404, detail="Record not found")
        df = df[df["user"].astype(str) != record_id]
        df.to_csv(CSV_PATH, index=False)
        return {"message": "Record deleted successfully"}
    finally:
        await file_lock.release()

@app.post("/restore")
async def restore_backup(current_user: str = Depends(get_current_user)):
    if not os.path.exists(BACKUP_PATH):
        raise HTTPException(status_code=404, detail="No backup file found")
    await file_lock.acquire()
    try:
        shutil.copy2(BACKUP_PATH, CSV_PATH)
        return {"message": "Backup restored successfully"}
    finally:
        await file_lock.release()

@app.on_event("startup")
async def startup_event():
    await init_db()
    if not os.path.exists(CSV_PATH):
        pd.DataFrame(columns=["id", "data"]).to_csv(CSV_PATH, index=False)