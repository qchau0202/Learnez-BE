#!/usr/bin/env python3
"""Sync real course metadata from MongoDB Syllabi back to Supabase.

This script ensures that the LMS (Supabase) and the AI layer (Mongo) 
share the same course codes and descriptions for the RAG pipeline.

Usage:
    python -m ml.data.sync_syllabi_to_supabase
"""

import asyncio
import sys
from pathlib import Path

# Ensure we can import from app/ml
if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app.core.database import get_mongo_ai_db, get_supabase

async def sync_syllabi_to_supabase():
    ai_db = get_mongo_ai_db()
    sb = get_supabase(service_role=True)
    
    if not sb:
        print("Error: Supabase client not configured.")
        return

    # 1. Identify a lecturer (role_id=2) to own these courses
    lecturers = sb.table("users").select("user_id").eq("role_id", 2).limit(1).execute().data
    if not lecturers:
        print("Warning: No lecturer found in Supabase. Please create a lecturer account first.")
        return
    lecturer_id = lecturers[0]["user_id"]
    print(f"Using lecturer_id: {lecturer_id}")

    # 3. Fetch all crawled syllabi from MongoDB
    cursor = ai_db["syllabi"].find({})
    syllabi = await cursor.to_list(length=None)
    print(f"Found {len(syllabi)} syllabi in MongoDB.")

    upsert_count = 0
    for syllabus in syllabi:
        code = syllabus["course_code"]
        title = syllabus["title"]
        
        # 4. Check if course exists in Supabase by title (or code if column exists)
        existing = sb.table("courses").select("id").eq("title", title).execute().data
        
        payload = {
            "title": title,
            "description": syllabus.get("description"),
            "lecturer_id": lecturer_id,
            # Custom mapping: if your schema has a course_code column, add it here:
            # "course_code": code 
        }

        try:
            if existing:
                course_id = existing[0]["id"]
                sb.table("courses").update(payload).eq("id", course_id).execute()
            else:
                sb.table("courses").insert(payload).execute()
            upsert_count += 1
            if upsert_count % 10 == 0:
                print(f"Synced {upsert_count}/{len(syllabi)} courses...")
        except Exception as e:
            print(f"Failed to sync {title}: {e}")

    print(f"Successfully synchronized {upsert_count} courses to Supabase.")

if __name__ == "__main__":
    asyncio.run(sync_syllabi_to_supabase())