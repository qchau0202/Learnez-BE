#!/usr/bin/env python3
"""Seed the LMS with realistic curriculum data for AI training context.

Usage:
    python -m ml.data.seed_curriculum
"""

import asyncio
import sys
from pathlib import Path

# Ensure we can import from app/ml
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app.core.database import get_supabase
from ml.data.curriculum_catalog import CURRICULUM_DATA

async def seed_academic_data():
    svc = get_supabase(service_role=True)
    if not svc:
        print("Error: Missing SUPABASE_SERVICE_ROLE_KEY.")
        return

    # 1. Identify a lecturer to own these courses
    lecturers = svc.table("users").select("user_id").eq("role_id", 2).limit(1).execute().data
    if not lecturers:
        print("Warning: No lecturer found in Supabase. Please create a lecturer account first.")
        return
    lecturer_id = lecturers[0]["user_id"]
    print(f"Using lecturer_id: {lecturer_id}")

    for course_info in CURRICULUM_DATA:
        print(f"Seeding Course: {course_info['title']} ({course_info['code']})...")
        
        # Insert Course
        course_res = svc.table("courses").insert({
            "title": course_info["title"],
            "description": course_info["description"],
            "lecturer_id": lecturer_id,
            # Optional: custom codes can be stored in description or a metadata field if supported
        }).execute()
        
        if not course_res.data:
            continue
        
        course_id = course_res.data[0]["id"]

        for mod_info in course_info["modules"]:
            # Insert Module
            mod_res = svc.table("modules").insert({
                "course_id": course_id,
                "title": mod_info["title"],
                "description": mod_info["description"]
            }).execute()
            
            if not mod_res.data:
                continue
            
            module_id = mod_res.data[0]["id"]

            for assign_info in mod_info.get("assignments", []):
                # Insert Assignment
                svc.table("assignments").insert({
                    "module_id": module_id,
                    "title": assign_info["title"],
                    "description": assign_info["description"],
                    "total_score": assign_info.get("score", 100),
                    "is_graded": True,
                    "uploaded_by": lecturer_id
                }).execute()

    print("Curriculum seeding completed.")
    print("Next Steps:")
    print("1. Run sync: python -m ml.data.provision_real_students --count 10")
    print("2. Backfill features: python -m ml.data.backfill_weekly_features --use-raw-range")

if __name__ == "__main__":
    asyncio.run(seed_academic_data())