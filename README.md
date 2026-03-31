# Learnez-BE

FastAPI backend for an e-learning LMS. The project is organized by domain modules and is designed to support IAM, course management, assessment workflows, activity tracking, file storage, and AI-assisted learning features.

## Tech Stack

- Python
- FastAPI
- Uvicorn
- MongoDB with `motor`
- Supabase
- Scikit-learn, pandas, numpy, joblib

## Project Structure

```
Learnez-BE/
|-- app/
|   |-- api/
|   |   |-- iam/
|   |   |-- course/
|   |   |-- assessment/
|   |   |-- activity/
|   |   |-- storage/
|   |   |-- deps.py
|   |   |-- router.py
|   |-- core/
|   |   |-- config.py
|   |   |-- database.py
|   |   |-- security.py
|   |-- models/
|   |-- schemas/
|   |-- services/
|   |   |-- iam/
|   |   |-- course/
|   |   |-- assessment/
|   |   |-- activity/
|   |   |-- storage/
|   |   |-- ai/
|   |-- utils/
|-- ml/
|   |-- data/
|   |-- training/
|-- main.py
|-- requirements.txt
```

## Current Modules

- IAM: authentication and account management
- Course: course, content, and enrollment flows
- Assessment: attendance, assignments, grading, notifications
- Activity: activity tracking and analytics
- Storage: file handling endpoints
- AI: learning path recommendation, dropout prediction, competency analysis

## API Overview

The application mounts a versioned API router at `/api`.

Main routes currently include:

- `GET /`
- `GET /health`
- `/api/auth/*`
- `/api/accounts/*`
- `/api/courses/*`
- `/api/content/*`
- `/api/enrollment/*`
- `/api/attendance/*`
- `/api/assignments/*`
- `/api/grading/*`
- `/api/notifications/*`
- `/api/activity/*`
- `/api/analytics/*`
- `/api/storage/*`

Some endpoint files are still scaffolds and contain placeholder implementations.

## Environment Variables

Copy `.env.example` to `.env` in the project root and fill in the values:

```bash
copy .env.example .env
```

Current example values:

```env
MONGODB_URI=YOUR_MONGODB_URI
MONGODB_DB=YOUR_MONGODB_DB_NAME
SUPABASE_SERVICE_ROLE_KEY=YOUR_SUPABASE_SERVICE_ROLE_KEY
SUPABASE_URL=YOUR_SUPABASE_URL
SUPABASE_ANON_KEY=YOUR_SUPABASE_ANON_KEY
JWT_SECRET=YOUR_JWT_SECRET
```

Optional config supported by `app/core/config.py`:

```env
JWT_ALGORITHM=HS256
JWT_EXPIRE_MINUTES=60
```

## Installation

```bash
python -m venv .venv
```

Windows:

```bash
.venv\Scripts\activate
```

Install dependencies:

```bash
pip install -r requirements.txt
```

## Run The Server

```bash
uvicorn main:app --reload
```

Default local URLs:

- API root: `http://127.0.0.1:8000/`
- Health check: `http://127.0.0.1:8000/health`
- Swagger UI: `http://127.0.0.1:8000/docs`
- ReDoc: `http://127.0.0.1:8000/redoc`

## Database Bootstrap

At startup, the app initializes:

- a MongoDB client
- the MongoDB database handle
- an optional Supabase client

These are attached to `app.state` inside the FastAPI lifespan hook in `main.py`.

## Notes

- MongoDB is the primary configured database connection in the current codebase.
- Supabase is optional and only created when its environment variables are provided.
- The repo already includes scaffolding for service and ML modules even where endpoint logic is still incomplete.

## Dependencies

Main packages from `requirements.txt`:

- `fastapi`
- `uvicorn[standard]`
- `python-multipart`
- `motor`
- `pymongo`
- `supabase`
- `scikit-learn`
- `pandas`
- `numpy`
- `joblib`
