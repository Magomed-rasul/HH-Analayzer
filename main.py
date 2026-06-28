import os
from fastapi import FastAPI, Request, Form, Body
from fastapi.responses import HTMLResponse, StreamingResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from services.hh_client import HHClient
from services.analyzer import analyze_vacancies
from dotenv import load_dotenv
import csv
import io

load_dotenv()

app = FastAPI(title="HH Analyzer")
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")

HH_CLIENT_ID = os.environ.get("HH_CLIENT_ID")
HH_CLIENT_SECRET = os.environ.get("HH_CLIENT_SECRET")

hh_client = HHClient(client_id=HH_CLIENT_ID, client_secret=HH_CLIENT_SECRET)
_cached_token = hh_client.access_token


def _save_env(client_id: str, client_secret: str):
    env_path = os.path.join(os.path.dirname(__file__), ".env")
    lines = []
    if os.path.exists(env_path):
        with open(env_path, "r") as f:
            lines = [l for l in f.readlines() if not l.startswith("HH_CLIENT_ID") and not l.startswith("HH_CLIENT_SECRET") and not l.startswith("HH_ACCESS_TOKEN") and l.strip()]
    lines.append(f"HH_CLIENT_ID={client_id}")
    lines.append(f"HH_CLIENT_SECRET={client_secret}")
    lines.append(f"HH_ACCESS_TOKEN=")
    with open(env_path, "w") as f:
        f.write("\n".join(lines) + "\n")

AREAS = {
    "1": "Москва",
    "2": "Санкт-Петербург",
    "3": "Екатеринбург",
    "4": "Новосибирск",
    "78": "Казань",
    "88": "Краснодар",
    "76": "Ростов-на-Дону",
    "72": "Уфа",
    "45": "Волгоград",
    "73": "Самара",
    "36": "Тюмень",
    "49": "Воронеж",
    "68": "Красноярск",
    "66": "Пермь",
    "54": "Омск",
    "69": "Саратов",
    "79": "Тольятти",
    "22": "Барнаул",
    "35": "Ижевск",
    "71": "Тула",
    "50": "Иркутск",
    "39": "Калининград",
    "24": "Кемерово",
    "47": "Нижний Новгород",
    "34": "Челябинск",
    "77": "Ярославль",
}

SCHEDULES = {
    "remote": "Удалённо",
    "fullDay": "Полный день",
    "flexible": "Гибкий график",
    "shift": "Сменный график",
    "flyInFlyOut": "Вахтовый метод",
}


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    has_api = hh_client.access_token is not None
    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={
            "areas": AREAS,
            "schedules": SCHEDULES,
            "has_api": has_api,
        },
    )


@app.get("/api/settings")
async def get_settings():
    return {"has_api": hh_client.access_token is not None}


@app.get("/api/vacancy/{vacancy_id}/description")
async def get_vacancy_description(vacancy_id: int):
    desc = hh_client._fetch_vacancy_description(vacancy_id)
    return {"description": desc}


@app.get("/api/responses")
async def get_responses(text: str = "", area: str = "", schedule: str = "", title_only: bool = False, exclude_ids: str = ""):
    excluded = set(exclude_ids.split(",")) if exclude_ids else set()
    results = {}
    for sort in ["relevance", "salary_desc", "salary_asc"]:
        scraped = hh_client._scrape_search_page_sorted(text, area or None, schedule or None, False, sort=sort)
        for vid, sv in scraped.items():
            if str(vid) not in excluded and str(vid) not in results:
                rc = sv.get("responsesCount")
                if rc is not None:
                    results[str(vid)] = rc
    return results


@app.post("/api/analyze")
async def analyze(items: list = Body(...)):
    stats = analyze_vacancies(items)
    return stats


@app.post("/api/settings")
async def save_settings(
    client_id: str = Form(...),
    client_secret: str = Form(...),
):
    global hh_client, _cached_token
    _save_env(client_id, client_secret)
    hh_client = HHClient(client_id=client_id, client_secret=client_secret)
    _cached_token = hh_client.access_token
    return {"has_api": hh_client.access_token is not None}


@app.post("/api/settings/connect")
async def connect_api():
    global hh_client, _cached_token
    if _cached_token:
        hh_client = HHClient(client_id=HH_CLIENT_ID, client_secret=HH_CLIENT_SECRET)
        hh_client.access_token = _cached_token
        hh_client.session.headers["Authorization"] = f"Bearer {_cached_token}"
    else:
        hh_client = HHClient(client_id=HH_CLIENT_ID, client_secret=HH_CLIENT_SECRET)
        _cached_token = hh_client.access_token
    return {"has_api": hh_client.access_token is not None}


@app.post("/api/settings/disconnect")
async def disconnect_api():
    global hh_client
    hh_client = HHClient()
    return {"has_api": False}


@app.post("/search")
async def search(
    text: str = Form(...),
    area: str = Form(""),
    schedule: str = Form(""),
    title_only: bool = Form(False),
):
    try:
        results = hh_client.search_vacancies(text=text, area=area or None, schedule=schedule or None, page_limit=5, title_only=title_only)
        stats = analyze_vacancies(results["items"])
        return {"results": results, "stats": stats}
    except Exception as e:
        return {"error": str(e)}


@app.post("/export")
async def export(
    text: str = Form(...),
    area: str = Form(""),
    schedule: str = Form(""),
    title_only: bool = Form(False),
):
    results = hh_client.search_vacancies(text=text, area=area or None, schedule=schedule or None, page_limit=5, title_only=title_only)

    hh_client.fetch_descriptions_batch(results["items"])

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["ID", "Название", "Компания", "Город", "Опыт", "График", "Зарплата от", "Зарплата до", "Навыки", "Описание", "Отклики", "Ссылка"])

    for v in results["items"]:
        salary = v.get("salary") or {}
        skills = ", ".join(v.get("key_skills", []))
        salary_from = salary.get("from", "")
        salary_to = salary.get("to", "")
        writer.writerow([
            v.get("id"),
            v.get("name"),
            v.get("employer"),
            v.get("area"),
            v.get("experience"),
            v.get("schedule"),
            salary_from,
            salary_to,
            skills,
            v.get("description", ""),
            v.get("responses_count", ""),
            v.get("url"),
        ])

    output.seek(0)
    return StreamingResponse(
        io.BytesIO(output.getvalue().encode("utf-8-sig")),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=hh_vacancies.csv"},
    )


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
