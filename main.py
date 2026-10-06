"""PocketSmart AI - FastAPI backend."""
import hashlib, hmac, io, json, os, secrets, sqlite3, time
from typing import Optional

import jwt
from dotenv import load_dotenv
from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse
from fastapi.security import OAuth2PasswordRequestForm
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from PIL import Image

import gemini_utils as ai

load_dotenv()
SECRET_KEY = os.getenv("SECRET_KEY", "dev-secret-change-me")
TOKEN_TTL = 60 * 60 * 8
DB = "pocketsmart.db"

app = FastAPI(title="PocketSmart AI")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")


# ---------- database ----------
def db():
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    return con


@app.on_event("startup")
def startup():
    with db() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY, username TEXT UNIQUE, email TEXT, pw TEXT);
        CREATE TABLE IF NOT EXISTS history(id INTEGER PRIMARY KEY, user_id INTEGER, category TEXT,
            inputs TEXT, result TEXT, created REAL);""")


# ---------- auth ----------
def hash_pw(pw, salt=None):
    salt = salt or secrets.token_hex(8)
    return salt + "$" + hashlib.pbkdf2_hmac("sha256", pw.encode(), salt.encode(), 120_000).hex()


def check_pw(pw, stored):
    return hmac.compare_digest(hash_pw(pw, stored.split("$")[0]), stored)


def make_token(user):
    return jwt.encode({"sub": user["username"], "uid": user["id"], "exp": time.time() + TOKEN_TTL}, SECRET_KEY, "HS256")


def current_user(request: Request):
    token = request.cookies.get("access_token") or (request.headers.get("authorization", "")[7:])
    try:
        uid = jwt.decode(token, SECRET_KEY, ["HS256"])["uid"]
    except Exception:
        raise HTTPException(401, "Not authenticated")
    with db() as c:
        row = c.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
    if not row:
        raise HTTPException(401, "Not authenticated")
    return dict(row)


@app.exception_handler(HTTPException)
async def http_exc(request: Request, exc: HTTPException):
    from fastapi.responses import JSONResponse
    if exc.status_code == 401 and "text/html" in request.headers.get("accept", ""):
        return RedirectResponse("/login", 303)
    return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)


def render(request, name, user=None, **ctx):
    return templates.TemplateResponse(name, {"request": request, "user": user, **ctx})


def optional_user(request: Request):
    try:
        return current_user(request)
    except HTTPException:
        return None


# ---------- public pages ----------
@app.get("/")
def index(request: Request):
    return render(request, "index.html", optional_user(request))


@app.get("/testimonials")
def testimonials(request: Request):
    return render(request, "testimonials.html", optional_user(request))


@app.get("/register")
def register_page(request: Request):
    return render(request, "register.html")


@app.post("/register")
def register(request: Request, username: str = Form(...), email: str = Form(...), password: str = Form(...)):
    try:
        with db() as c:
            c.execute("INSERT INTO users(username,email,pw) VALUES(?,?,?)", (username.strip(), email, hash_pw(password)))
    except sqlite3.IntegrityError:
        return render(request, "register.html", error="Username already taken.")
    return RedirectResponse("/login", 303)


@app.get("/login")
def login_page(request: Request):
    return render(request, "login.html")


def _authenticate(username, password):
    with db() as c:
        row = c.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
    return dict(row) if row and check_pw(password, row["pw"]) else None


@app.post("/login")
def login(request: Request, username: str = Form(...), password: str = Form(...)):
    user = _authenticate(username, password)
    if not user:
        return render(request, "login.html", error="Invalid username or password.")
    resp = RedirectResponse("/dashboard", 303)
    resp.set_cookie("access_token", make_token(user), httponly=True, max_age=TOKEN_TTL, samesite="lax")
    return resp


@app.post("/token")
def token(form: OAuth2PasswordRequestForm = Depends()):
    user = _authenticate(form.username, form.password)
    if not user:
        raise HTTPException(400, "Incorrect username or password")
    return {"access_token": make_token(user), "token_type": "bearer"}


@app.get("/logout")
def logout():
    resp = RedirectResponse("/login", 303)
    resp.delete_cookie("access_token")
    return resp


# ---------- session ----------
@app.get("/session-info")
def session_info(user=Depends(current_user)):
    return {"user_id": user["id"], "username": user["username"], "logged_in": True}


@app.get("/session-data")
def session_data(user=Depends(current_user)):
    with db() as c:
        rows = c.execute("SELECT category, COUNT(*) n FROM history WHERE user_id=? GROUP BY category", (user["id"],)).fetchall()
    return {"username": user["username"], "queries_by_category": {r["category"]: r["n"] for r in rows}}


# ---------- dashboard & history ----------
def _history(uid, limit=None):
    q = "SELECT * FROM history WHERE user_id=? ORDER BY created DESC" + (f" LIMIT {limit}" if limit else "")
    with db() as c:
        return [dict(r, inputs=json.loads(r["inputs"]), result=json.loads(r["result"])) for r in c.execute(q, (uid,))]


@app.get("/dashboard")
def dashboard(request: Request, user=Depends(current_user)):
    return render(request, "dashboard.html", user, recent=_history(user["id"], 5))


@app.get("/history")
def history(request: Request, user=Depends(current_user)):
    return render(request, "history.html", user, rows=_history(user["id"]))


@app.get("/recommendations-details/{rid}")
def details(rid: int, request: Request, user=Depends(current_user)):
    with db() as c:
        r = c.execute("SELECT * FROM history WHERE id=? AND user_id=?", (rid, user["id"])).fetchone()
    if not r:
        raise HTTPException(404, "Not found")
    return render(request, "results.html", user, category=r["category"], result=json.loads(r["result"]),
                  inputs=json.loads(r["inputs"]))


def _save(user, category, inputs, result):
    with db() as c:
        c.execute("INSERT INTO history(user_id,category,inputs,result,created) VALUES(?,?,?,?,?)",
                  (user["id"], category, json.dumps(inputs), json.dumps(result), time.time()))


# ---------- planners ----------
@app.get("/home-planner")
def home_planner(request: Request, user=Depends(current_user)):
    return render(request, "home_planner.html", user)


@app.post("/generate-home")
def generate_home(request: Request, budget: float = Form(..., gt=0), style: str = Form("Modern"),
                  rooms: list[str] = Form([]), lights: int = Form(0), fans: int = Form(0),
                  dining_tables: int = Form(0), beds: int = Form(0), user=Depends(current_user)):
    qty = {"lights": lights, "ceiling_fans": fans, "dining_tables": dining_tables, "beds": beds}
    result = ai.home_recommendations(budget, style, rooms, qty)
    inputs = {"budget": budget, "style": style, "rooms": rooms, **qty}
    _save(user, "home", inputs, result)
    return render(request, "results.html", user, category="home", result=result, inputs=inputs)


@app.get("/party-planner")
def party_planner(request: Request, user=Depends(current_user)):
    return render(request, "party_planner.html", user)


@app.post("/generate-party")
def generate_party(request: Request, budget: float = Form(..., gt=0), guests: int = Form(..., gt=0),
                   event_type: str = Form("Birthday"), venue: str = Form("Home"), city: str = Form(""),
                   user=Depends(current_user)):
    result = ai.party_recommendations(budget, guests, event_type, venue, city)
    inputs = {"budget": budget, "guests": guests, "event_type": event_type, "venue": venue, "city": city}
    _save(user, "party", inputs, result)
    return render(request, "results.html", user, category="party", result=result, inputs=inputs)


@app.get("/jewelry-planner")
def jewelry_planner(request: Request, user=Depends(current_user)):
    return render(request, "jewelry_planner.html", user)


@app.post("/generate-jewelry")
async def generate_jewelry(request: Request, budget: float = Form(..., gt=0), occasion: str = Form("Wedding"),
                           style: str = Form("Traditional"), outfit: Optional[UploadFile] = File(None),
                           user=Depends(current_user)):
    image = None
    if outfit and outfit.filename:
        try:
            image = Image.open(io.BytesIO(await outfit.read())).convert("RGB")
        except Exception:
            raise HTTPException(400, "Uploaded file is not a valid image")
    result = ai.jewelry_recommendations(budget, occasion, style, image)
    inputs = {"budget": budget, "occasion": occasion, "style": style, "image_used": image is not None}
    _save(user, "jewelry", inputs, result)
    return render(request, "results.html", user, category="jewelry", result=result, inputs=inputs)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="127.0.0.1", port=8000, reload=True)
