

import os
import httpx
import asyncio
import base64
import logging
from dotenv import load_dotenv
from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import openai

# === Load Environment Variables ===
load_dotenv()

# === Logging Setup ===
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# === API Keys ===
openai.api_key = os.getenv("OPENAI_API_KEY")
HUGGINGFACE_API_KEY = os.getenv("HUGGINGFACE_API_KEY")
ELEVENLABS_API_KEY = os.getenv("ELEVENLABS_API_KEY")
ELEVENLABS_VOICE_ID = os.getenv("ELEVENLABS_VOICE_ID")
TAVUS_API_KEY = os.getenv("TAVUS_API_KEY")
TAVUS_BASE_URL = "https://tavusapi.com/v2"

# === FastAPI Setup ===
app = FastAPI(title="Live AI Tutor")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Replace with frontend domain in production
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# === Request Models ===
class LessonRequest(BaseModel):
    topic: str
    grade_level: str
    language: str
    user_id: str

class DoubtRequest(BaseModel):
    topic: str
    doubt: str
    user_id: str

# === Tavus Helpers ===
async def create_tavus_user_if_not_exists(user_id: str):
    headers = {
        "Authorization": f"Bearer {TAVUS_API_KEY}",
        "Content-Type": "application/json"
    }
    payload = {
        "user_id": user_id,
        "name": user_id,
        "email": f"{user_id.lower().replace(' ', '_')}@autogenerate.tavus"
    }
    url = f"{TAVUS_BASE_URL}/users"
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(url, headers=headers, json=payload)
            if response.status_code in [200, 201, 409]:
                return True
            logger.error("Tavus user creation failed: %s", response.text)
    except Exception as e:
        logger.exception("Exception creating Tavus user: %s", e)
    return False

async def generate_tavus_video(user_id: str, script: str) -> str:
    await create_tavus_user_if_not_exists(user_id)
    headers = {
        "Authorization": f"Bearer {TAVUS_API_KEY}",
        "Content-Type": "application/json"
    }
    payload = {
        "user_id": user_id,
        "script": script,
        "language": "en",
        "style": "tutor"
    }
    create_url = f"{TAVUS_BASE_URL}/videos"

    try:
        async with httpx.AsyncClient(timeout=60) as client:
            create_response = await client.post(create_url, headers=headers, json=payload)
            create_response.raise_for_status()
            video_data = create_response.json()
            video_id = video_data.get("id")
            if not video_id:
                return ""

            status_url = f"{TAVUS_BASE_URL}/videos/{video_id}"
            for _ in range(20):
                await asyncio.sleep(2)
                status_response = await client.get(status_url, headers=headers)
                if status_response.status_code == 200:
                    video_info = status_response.json()
                    if video_info.get("status") == "completed":
                        return video_info.get("video_url") or video_info.get("asset_url", "")
    except Exception as e:
        logger.exception("Error generating Tavus video: %s", e)

    return ""

# === AI Generation ===
async def generate_lesson(topic: str, grade: str, language: str) -> str:
    prompt = f"Teach '{topic}' to a grade {grade} student in {language}. Use bullet points and examples."
    try:
        response = await openai.ChatCompletion.acreate(
            model="gpt-4",
            messages=[{"role": "user", "content": prompt}]
        )
        return response.choices[0].message.content.strip()
    except Exception as e:
        logger.exception("OpenAI lesson generation error: %s", e)
        return ""

async def generate_script(topic: str, lesson: str, language: str) -> str:
    prompt = f"Create a short 2-minute video narration script teaching '{topic}' to a student in {language}:\n{lesson}"
    try:
        response = await openai.ChatCompletion.acreate(
            model="gpt-4",
            messages=[{"role": "user", "content": prompt}]
        )
        return response.choices[0].message.content.strip()
    except Exception as e:
        logger.exception("OpenAI script generation error: %s", e)
        return ""

async def generate_quiz(script: str, language: str) -> str:
    prompt = f"Generate 5 multiple-choice quiz questions (4 options each) in {language} based on this script:\n{script}"
    try:
        response = await openai.ChatCompletion.acreate(
            model="gpt-4",
            messages=[{"role": "user", "content": prompt}]
        )
        return response.choices[0].message.content.strip()
    except Exception as e:
        logger.exception("OpenAI quiz generation error: %s", e)
        return ""

async def generate_audio_base64(text: str) -> str:
    url = f"https://api.elevenlabs.io/v1/text-to-speech/{ELEVENLABS_VOICE_ID}"
    headers = {
        "Accept": "audio/mpeg",
        "Content-Type": "application/json",
        "xi-api-key": ELEVENLABS_API_KEY,
    }
    payload = {
        "text": text,
        "voice_settings": {"stability": 0.5, "similarity_boost": 0.75}
    }
    try:
        async with httpx.AsyncClient(timeout=60) as client:
            response = await client.post(url, headers=headers, json=payload)
            if response.status_code == 200:
                return base64.b64encode(response.content).decode("utf-8")
    except Exception as e:
        logger.exception("ElevenLabs audio error: %s", e)
    return ""

async def generate_sd_image_base64(prompt: str) -> str:
    API_URL = "https://api-inference.huggingface.co/models/stabilityai/stable-diffusion-2"
    headers = {"Authorization": f"Bearer {HUGGINGFACE_API_KEY}"}
    try:
        async with httpx.AsyncClient(timeout=60) as client:
            response = await client.post(API_URL, headers=headers, json={"inputs": prompt})
            if response.status_code == 200:
                return base64.b64encode(response.content).decode("utf-8")
    except Exception as e:
        logger.exception("Image generation error: %s", e)
    return ""

async def breakdown_script(script: str) -> list:
    parts = [line.strip("•- ").strip() for line in script.split('\n') if line.strip()]
    results = []
    for line in parts:
        audio = await generate_audio_base64(line)
        image = await generate_sd_image_base64(line)
        results.append({
            "text": line,
            "image_base64": image,
            "audio_base64": audio
        })
    return results

async def resolve_doubt(doubt: str, topic: str, language: str) -> str:
    prompt = f"A student has this doubt in {language} about the topic '{topic}': {doubt}. Please answer simply."
    try:
        response = await openai.ChatCompletion.acreate(
            model="gpt-4",
            messages=[{"role": "user", "content": prompt}]
        )
        return response.choices[0].message.content.strip()
    except Exception as e:
        logger.exception("OpenAI doubt resolution error: %s", e)
        return "Sorry, I couldn't understand the doubt."

# === API Endpoints ===
@app.post("/lesson/start")
async def start_lesson(req: LessonRequest):
    lesson = await generate_lesson(req.topic, req.grade_level, req.language)
    script = await generate_script(req.topic, lesson, req.language)
    quiz = await generate_quiz(script, req.language)
    visuals_audio = await breakdown_script(script)
    tavus_url = await generate_tavus_video(req.user_id, script)

    return {
        "lesson_text": lesson,
        "video_script": script,
        "quiz": quiz,
        "live_teaching": visuals_audio,
        "tavus_video_url": tavus_url
    }

@app.post("/lesson/doubt")
async def answer_doubt(req: DoubtRequest):
    answer = await resolve_doubt(req.doubt, req.topic, language="English")
    return {"answer": answer}

@app.get("/video")
async def get_video(user_id: str = Query(...), script: str = Query(...)):
    """Optional endpoint to generate a video from user_id and script (if not using /lesson/start)"""
    video_url = await generate_tavus_video(user_id, script)
    return {"video_url": video_url}

@app.get("/health")
def health():
    return {"status": "ok"} 
