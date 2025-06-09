# main.py

import os
import time
import base64
from typing import List
from fastapi import FastAPI, Request, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, constr
from dotenv import load_dotenv
import requests
import openai
from supabase import create_client, Client

load_dotenv()

app = FastAPI()

# Restrict CORS in production
app.add_middleware(
    CORSMiddleware,
    allow_origins=["https://your-frontend.com"],  # Replace with actual frontend domain
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 🔐 Load secrets
OPENAI_API = os.getenv("OPENAI_API")
HUGGINGFACE_API = os.getenv("HUGGINGFACE_API")
TAVUS_API = os.getenv("TAVUS_API")
TAVUS_REPLICA_ID = os.getenv("TAVUS_REPLICA_ID")
REVENUECAT_API = os.getenv("REVENUECAT_API")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

openai.api_key = OPENAI_API
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)


# 📄 Request models
class LessonRequest(BaseModel):
    userId: constr(min_length=1)
    username: constr(min_length=1)
    topic: constr(min_length=2)
    grade: constr(min_length=1)
    language: constr(min_length=2)


class QuestionRequest(BaseModel):
    question: constr(min_length=5)


class AuthRequest(BaseModel):
    email: constr(min_length=5)
    password: constr(min_length=6)


class CheckoutRequest(BaseModel):
    plan: str
    userId: str


# 🧠 HuggingFace image generator
def generate_images(prompt: str, count: int = 2) -> List[str]:
    images = []
    for _ in range(count):
        res = requests.post(
            "https://api-inference.huggingface.co/models/runwayml/stable-diffusion-v1-5",
            headers={"Authorization": f"Bearer {HUGGINGFACE_API}"},
            json={"inputs": prompt}
        )
        if res.status_code == 200 and res.headers.get("content-type") == "image/png":
            img_base64 = base64.b64encode(res.content).decode("utf-8")
            images.append(f"data:image/png;base64,{img_base64}")
    return images


# ✅ RevenueCat subscription check
def check_subscription(user_id: str) -> bool:
    try:
        res = requests.get(
            f"https://api.revenuecat.com/v1/subscribers/{user_id}",
            headers={"Authorization": REVENUECAT_API}
        )
        if res.status_code != 200:
            return False
        entitlements = res.json().get("subscriber", {}).get("entitlements", {})
        return any(entitlements.values())
    except:
        return False


# 🎓 Lesson generator endpoint
@app.post("/generate_lesson")
async def generate_lesson(req: LessonRequest, request: Request):
    token = request.headers.get("Authorization")
    if not token:
        raise HTTPException(status_code=401, detail="Missing token")

    try:
        user = supabase.auth.get_user(token.replace("Bearer ", ""))
    except Exception:
        raise HTTPException(status_code=401, detail="Invalid or expired token")

    lesson_count = supabase.table("lessons").select("id").eq("user_id", req.userId).execute()

    if len(lesson_count.data or []) >= 3 and not check_subscription(req.userId):
        raise HTTPException(status_code=403, detail="Free plan limit reached")

    # Generate script
    prompt = f"Write a 1-minute lesson script for topic '{req.topic}', for grade {req.grade}, in {req.language}."
    script_response = openai.ChatCompletion.create(
        model="gpt-4",
        messages=[{"role": "user", "content": prompt}],
        max_tokens=500
    )
    script = script_response.choices[0].message.content.strip()

    # Generate Tavus video
    tavus_res = requests.post(
        "https://api.tavusapi.com/v2/videos",
        headers={"x-api-key": TAVUS_API},
        json={"replica_id": TAVUS_REPLICA_ID, "script": script}
    )
    if tavus_res.status_code != 200:
        raise HTTPException(status_code=500, detail="Tavus video creation failed")
    video_id = tavus_res.json().get("video_id")

    # Poll for video URL
    video_url = None
    for _ in range(10):
        time.sleep(3)
        status_res = requests.get(
            f"https://api.tavusapi.com/v2/videos/{video_id}",
            headers={"x-api-key": TAVUS_API}
        )
        video_url = status_res.json().get("hosted_url")
        if video_url:
            break
    if not video_url:
        raise HTTPException(status_code=500, detail="Tavus video processing timeout")

    images = generate_images(script)

    # Generate quiz
    quiz_prompt = f"Create 3 multiple choice questions from:\n{script}"
    quiz_response = openai.ChatCompletion.create(
        model="gpt-4",
        messages=[{"role": "user", "content": quiz_prompt}],
        max_tokens=500
    )
    quiz = quiz_response.choices[0].message.content.strip()

    supabase.table("lessons").insert({
        "user_id": req.userId,
        "topic": req.topic,
        "script": script,
        "video_url": video_url,
        "images": images,
        "quiz": quiz
    }).execute()

    return {
        "username": req.username,
        "topic": req.topic,
        "script": script,
        "video_url": video_url,
        "images": images,
        "quiz": quiz
    }


# ❓ Ask doubt
@app.post("/ask_doubt")
async def ask_doubt(q: QuestionRequest):
    response = openai.ChatCompletion.create(
        model="gpt-4",
        messages=[{"role": "user", "content": q.question}],
        max_tokens=300
    )
    return {"answer": response.choices[0].message.content.strip()}


# 🔐 Signup
@app.post("/signup")
async def signup(data: AuthRequest):
    res = supabase.auth.sign_up({"email": data.email, "password": data.password})
    return {"user": res.user.id, "email": res.user.email}


# 🔐 Login
@app.post("/login")
async def login(data: AuthRequest):
    res = supabase.auth.sign_in_with_password({"email": data.email, "password": data.password})
    return {
        "user": {"id": res.user.id, "email": res.user.email},
        "access_token": res.session.access_token
    }


# 💰 Pricing plans
@app.get("/pricing")
async def pricing():
    return {
        "plans": [
            {"name": "Starter", "price": 60, "features": ["3 free lessons", "Basic support"]},
            {"name": "Pro", "price": 100, "features": ["Unlimited lessons", "Priority support"]}
        ]
    }


# 🧾 Checkout mock
@app.post("/create_checkout")
async def create_checkout(data: CheckoutRequest):
    return {"checkout_url": f"https://mock-checkout.com/{data.plan}_plan?user_id={data.userId}"}


# 📦 Stripe webhook mock
@app.post("/stripe_webhook")
async def stripe_webhook(event: dict):
    uid = event.get("data", {}).get("object", {}).get("metadata", {}).get("user_id", "unknown_user")
    print(f"✅ MOCK STRIPE: Subscribed user {uid}")
    return {"status": "ok"}
