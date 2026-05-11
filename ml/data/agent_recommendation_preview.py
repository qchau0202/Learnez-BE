#!/usr/bin/env python3
"""RAG Agent Preview: Combining Risk Scores with real TDTU Syllabus data.

Usage:
    python -m ml.data.agent_recommendation_preview
"""

import asyncio
import os
import sys
from pathlib import Path
from dotenv import load_dotenv
from google import genai

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app.core.database import get_mongo_ai_db, get_supabase
from ml.data.contracts import RecommendationDocument

# Load environment variables from .env file
load_dotenv()

async def generate_recommendation_context():
    ai_db = get_mongo_ai_db()
    sb = get_supabase(service_role=True)

    # 1. Fetch a high-risk student to demonstrate
    risk = await ai_db["risk_scores"].find_one({"risk_level": "high"})
    if not risk:
        print("No high-risk students found. Run inference first.")
        return
    
    user_id = risk["user_id"]
    course_id = risk["course_id"]

    # 2. Get student and course details from Supabase with existence checks
    user_res = sb.table("users").select("full_name").eq("user_id", user_id).execute()
    course_res = sb.table("courses").select("title").eq("id", course_id).execute()
    
    if not user_res.data or not course_res.data:
        print(f"Error: Missing identity data for user {user_id} or course {course_id}")
        return
        
    user = user_res.data[0]
    course = course_res.data[0]

    # 3. Get the real TDTU Syllabus from MongoDB (The RAG Context)
    # Using case-insensitive regex for more reliable matching of Vietnamese titles
    syllabus = await ai_db["syllabi"].find_one({
        "title": {"$regex": f"^{course['title'].strip()}$", "$options": "i"}
    })

    print("=" * 60)
    print(f"AI ANALYSIS FOR: {user['full_name']}")
    print(f"COURSE: {course['title']}")
    print(f"RISK LEVEL: {risk['risk_level'].upper()} ({risk['risk_score']:.2f})")
    print("-" * 60)
    
    if syllabus:
        print("ACADEMIC CONTEXT (From TDTU Syllabus):")
        print(f"Course Learning Outcomes (CLOs):")
        for clo in syllabus.get("clos", [])[:3]:
            print(f"  - {clo.get('description')}")
        
        print(f"\nTarget Topics:")
        for topic in syllabus.get("topics", [])[:2]:
            print(f"  - Week {topic.get('week')}: {topic.get('title')}")

        # This is what you would send to Gemini/OpenRouter
        print("-" * 60)
        print("GENERATED PROMPT FOR LLM:")
        # Priority 4: Structured Prompt
        prompt = f"""
        [STUDENT PROFILE]
        Name: {user['full_name']}
        Status: At risk in {course['title']}

        [RISK SIGNALS]
        Alert metrics: {', '.join([f"{f['feature']} ({f['value']})" for f in risk['top_factors']])}
        Risk Score: {risk['risk_score']:.2f}

        [COURSE CONTEXT]
        Description: {syllabus.get('description', 'Standard TDTU course')}
        CLOs: {', '.join([c['description'] for c in syllabus.get('clos', [])])}
        Upcoming Topic: {syllabus['topics'][0]['title'] if syllabus.get('topics') else 'General Review'}

        [INSTRUCTION]
        Bạn là cố vấn học tập AI tại TDTU. Hãy tạo kế hoạch can thiệp 3 bước cá nhân hóa.
        Yêu cầu: Phản hồi Markdown bằng tiếng Việt, giọng văn chuyên nghiệp, tập trung vào các khái niệm kỹ thuật cụ thể.
        """
        print(prompt)
        
        # Actual LLM Call (Optional/Demo)
        api_key = os.environ.get("GEMINI_API_KEY")
        if api_key:
            api_key = api_key.strip().strip("'").strip('"')
            print("-" * 60)
            print("CALLING GEMINI API...")
            try:
                client = genai.Client(api_key=api_key)

                response = await client.aio.models.generate_content(
                    model='gemini-2.5-flash', 
                    contents=prompt
                )
                print("\nAI RECOMMENDATION:")
                if response and hasattr(response, 'text'):
                    print(response.text)
                else:
                    print(str(response))
            except genai.errors.ClientError as e:
                print(f"\nAPI Error (404/Not Found): {e}")
                print("Tip: This often means 'gemini-1.5-flash' isn't available for your API key's version.")
                print("Try running: pip install -U google-genai")
                print("Or check your API key at: https://aistudio.google.com/app/apikey")
            except Exception as e:
                print(f"\nError calling Gemini API: {e}")
        else:
            print("\n(Skipping API call: GEMINI_API_KEY not found in environment)")
    else:
        print("⚠️  RAG MISSING: No syllabus found in MongoDB for this course title.")
        print(f"Note: Your MongoDB 'syllabi' collection contains TDTU CS courses,")
        print(f"but this student is enrolled in '{course['title']}'.")

if __name__ == "__main__":
    asyncio.run(generate_recommendation_context())