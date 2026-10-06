# PocketSmart AI
Budget & recommendation assistant (FastAPI + Gemini + Jinja2) for Home, Party and Jewelry planning.

## Run
```bash
python -m venv venv && source venv/bin/activate   # Windows: venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env     # add GEMINI_API_KEY (https://aistudio.google.com/app/apikey)
python main.py           # http://127.0.0.1:8000
```
Without an API key the app still runs and returns default budget-share suggestions.

## Structure
- `main.py` – routes, auth (JWT cookie), SQLite history
- `gemini_utils.py` – prompts, Gemini calls (text + image), fallbacks, platform links
- `templates/`, `static/` – UI

## Routes
`/register /login /logout /token /session-info /session-data /dashboard /history /recommendations-details/{id}`
`/home-planner → /generate-home`, `/party-planner → /generate-party`, `/jewelry-planner → /generate-jewelry`
