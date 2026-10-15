# Employee Attendance & Analytics API

A FastAPI application for employee management, attendance tracking, and workforce analytics using MongoDB.

## Requirements
- Python 3.12
- MongoDB
- Dependencies listed in `requirements.txt`

## Setup
1. Create and activate a Python virtual environment.
2. Install dependencies:

   `pip install -r requirements.txt`

3. Configure `MONGO_URI` and `MONGO_DB` as environment variables.

## Run

`python -m uvicorn app.main:app --reload`

Open `http://127.0.0.1:8000/docs` to explore the API.

## Main features
- Employee management
- Attendance punch-in, punch-out, and regularization
- Monthly and department analytics
- Late-arrival leaderboard
- Database health check and query explain endpoint