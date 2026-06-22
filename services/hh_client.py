import requests
import re
import json
import time
import threading
from typing import Optional
from concurrent.futures import ThreadPoolExecutor, as_completed
from services.analyzer import extract_skills_from_text, extract_keywords_from_title

_skills_cache = {}
_cache_lock = threading.Lock()
_last_request_time = 0
_request_lock = threading.Lock()

CHROME_UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"


def _rate_limit(min_interval: float = 1.0):
    global _last_request_time
    with _request_lock:
        elapsed = time.time() - _last_request_time
        if elapsed < min_interval:
            time.sleep(min_interval - elapsed)
        _last_request_time = time.time()


HH_API_BASE = "https://api.hh.ru"

EXPERIENCE_MAP = {
    "noExperience": "Без опыта",
    "between1And3": "1-3 года",
    "between3And6": "3-6 лет",
    "moreThan6": "Более 6 лет",
}

SCHEDULE_MAP = {
    "fullDay": "Полный день",
    "remote": "Удалённо",
    "flexible": "Гибкий график",
    "shift": "Сменный график",
    "flyInFlyOut": "Вахтовый метод",
}


class HHClient:
    def __init__(self, client_id: Optional[str] = None, client_secret: Optional[str] = None):
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": CHROME_UA,
            "Accept": "application/vnd.hh+json",
        })
        self.scrape_session = requests.Session()
        self.scrape_session.headers.update({
            "User-Agent": CHROME_UA,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
        })
        self.access_token = None

        if client_id and client_secret:
            self._get_token(client_id, client_secret)

    def _get_token(self, client_id: str, client_secret: str):
        response = self.session.post(f"{HH_API_BASE}/token", data={
            "grant_type": "client_credentials",
            "client_id": client_id,
            "client_secret": client_secret,
        })
        if response.status_code == 200:
            self.access_token = response.json().get("access_token")
            self.session.headers["Authorization"] = f"Bearer {self.access_token}"

    def search_vacancies(
        self,
        text: str,
        area: Optional[str] = None,
        schedule: Optional[str] = None,
        page_limit: int = 1,
    ) -> dict:
        if self.access_token:
            return self._search_api(text, area, schedule, page_limit)
        return self._search_scrape(text, area, schedule, page_limit)

    def _search_api(
        self,
        text: str,
        area: Optional[str] = None,
        schedule: Optional[str] = None,
        page_limit: int = 1,
    ) -> dict:
        params = {
            "text": text,
            "order_by": "publication_time",
            "per_page": 100,
            "page": 0,
        }

        if area:
            params["area"] = area
        if schedule:
            params["schedule"] = schedule

        all_results = []
        for page in range(page_limit):
            params["page"] = page
            response = self.session.get(f"{HH_API_BASE}/vacancies", params=params)
            response.raise_for_status()
            data = response.json()
            all_results.extend(data.get("items", []))
            if page >= data.get("pages", 1) - 1:
                break

        all_results = self._enrich_with_api_details(all_results)
        return self._format_results(all_results)

    def _fetch_vacancy_detail(self, vacancy_id) -> dict:
        try:
            _rate_limit(1.0)
            resp = self.session.get(f"{HH_API_BASE}/vacancies/{vacancy_id}", timeout=10)
            if resp.status_code == 200:
                return resp.json()
        except Exception:
            pass
        return {}

    def _enrich_with_api_details(self, vacancies: list) -> list:
        ids = []
        for v in vacancies:
            vid = v.get("id")
            if vid:
                ids.append(vid)

        details_map = {}
        with ThreadPoolExecutor(max_workers=3) as executor:
            future_to_id = {executor.submit(self._fetch_vacancy_detail, vid): vid for vid in ids}
            for future in as_completed(future_to_id):
                vid = future_to_id[future]
                try:
                    details_map[vid] = future.result()
                except Exception:
                    details_map[vid] = {}

        for v in vacancies:
            vid = v.get("id")
            detail = details_map.get(vid, {})
            if detail:
                v["description"] = detail.get("description", "")
                v["key_skills"] = [s.get("name", "") for s in detail.get("key_skills", []) if isinstance(s, dict)]

        return vacancies

    def _fetch_vacancy_skills(self, vacancy_id) -> list:
        with _cache_lock:
            if vacancy_id in _skills_cache:
                return _skills_cache[vacancy_id]

        for attempt in range(2):
            time.sleep(0.5)
            try:
                _rate_limit(3.0)
                resp = self.scrape_session.get(f"https://hh.ru/vacancy/{vacancy_id}", timeout=15)
                html = resp.text
                if self._is_captcha_page(html):
                    if attempt == 0:
                        time.sleep(5)
                        continue
                    return []
                m = re.search(r'"keySkills":\{"keySkill":\[([^\]]*)\]', html)
                if m:
                    skills = json.loads("[" + m.group(1) + "]")
                    with _cache_lock:
                        _skills_cache[vacancy_id] = skills
                    return skills
                return []
            except Exception:
                if attempt == 0:
                    time.sleep(5)
                    continue
                return []
        return []

    def _enrich_with_skills(self, vacancies: list) -> list:
        ids = []
        for v in vacancies:
            vid = v.get("vacancyId") or v.get("id")
            if vid:
                ids.append((len(ids), vid))

        skills_map = {}
        with ThreadPoolExecutor(max_workers=1) as executor:
            future_to_idx = {executor.submit(self._fetch_vacancy_skills, vid): idx for idx, vid in ids}
            for future in as_completed(future_to_idx):
                idx = future_to_idx[future]
                try:
                    skills_map[idx] = future.result()
                except Exception:
                    skills_map[idx] = []

        for i, v in enumerate(vacancies):
            v["key_skills"] = skills_map.get(i, [])

        return vacancies

    def _fetch_vacancy_description(self, vacancy_id) -> str:
        for attempt in range(2):
            try:
                _rate_limit(3.0)
                resp = self.scrape_session.get(f"https://hh.ru/vacancy/{vacancy_id}", timeout=15)
                html = resp.text
                if self._is_captcha_page(html):
                    if attempt == 0:
                        time.sleep(5)
                        continue
                    return ""
                m = re.search(r'"description":\s*"((?:[^"\\]|\\.)*)"', html)
                if m:
                    desc = m.group(1)
                    desc = json.loads('"' + desc + '"')
                    return desc
                return ""
            except Exception:
                if attempt == 0:
                    time.sleep(5)
                    continue
                return ""
        return ""

    def _enrich_with_scrape_details(self, vacancies: list) -> list:
        ids = []
        for v in vacancies:
            vid = v.get("vacancyId") or v.get("id")
            if vid:
                ids.append((len(ids), vid))

        desc_map = {}
        with ThreadPoolExecutor(max_workers=1) as executor:
            future_to_idx = {executor.submit(self._fetch_vacancy_description, vid): idx for idx, vid in ids}
            for future in as_completed(future_to_idx):
                idx = future_to_idx[future]
                try:
                    desc_map[idx] = future.result()
                except Exception:
                    desc_map[idx] = ""

        for i, v in enumerate(vacancies):
            if not v.get("description"):
                v["description"] = desc_map.get(i, "")

        return vacancies

    def _curl_get(self, url: str) -> str:
        _rate_limit(1.0)
        try:
            resp = self.scrape_session.get(url, timeout=15)
            return resp.text
        except Exception:
            return ""

    def _search_scrape(
        self,
        text: str,
        area: Optional[str] = None,
        schedule: Optional[str] = None,
        page_limit: int = 1,
    ) -> dict:
        import urllib.parse
        params = {
            "text": text,
            "order_by": "publication_time",
            "per_page": 50,
        }
        if area:
            params["area"] = area
        if schedule:
            params["schedule"] = schedule

        all_results = []
        for page in range(page_limit):
            params["page"] = page + 1
            qs = urllib.parse.urlencode(params)
            html = self._curl_get(f"https://hh.ru/search/vacancy?{qs}")
            if self._is_captcha_page(html):
                if page == 0:
                    raise Exception("HH.ru запросил проверку (captcha). Подождите 30-60 секунд и попробуйте снова.")
                break
            vacancies = self._parse_scraped_page(html)
            all_results.extend(vacancies)
            if len(vacancies) < 5:
                break

        all_results = all_results[:250]
        first_10 = all_results[:10]
        first_10 = self._enrich_with_skills(first_10)
        first_10 = self._enrich_with_scrape_details(first_10)
        all_results = first_10 + all_results[10:]
        return self._format_results(all_results)

    def _is_captcha_page(self, html: str) -> bool:
        if len(html) < 5000:
            return True
        if re.search(r'class="[^"]*captcha[^"]*"', html.lower()):
            return True
        if re.search(r'<title>[^<]*captcha[^<]*</title>', html.lower()):
            return True
        return False

    def _parse_scraped_page(self, html: str) -> list:
        match = re.search(r'"vacancies":\s*\[', html)
        if not match:
            return []

        start = match.start() + len('"vacancies":')
        bracket_count = 0
        in_string = False
        escape_next = False

        for i in range(start, len(html)):
            c = html[i]
            if escape_next:
                escape_next = False
                continue
            if c == '\\' and not escape_next:
                escape_next = True
                continue
            if c == '"' and not escape_next:
                in_string = not in_string
                continue
            if in_string:
                continue
            if c == '[':
                bracket_count += 1
            elif c == ']':
                bracket_count -= 1
                if bracket_count == 0:
                    json_str = html[start:i + 1]
                    try:
                        return json.loads(json_str)
                    except json.JSONDecodeError:
                        return []
        return []

    def _format_results(self, items: list) -> dict:
        results = []
        for v in items:
            salary = v.get("salary") or v.get("compensation")
            salary_info = None
            if salary and isinstance(salary, dict):
                if "noCompensation" not in salary:
                    salary_from = salary.get("from", {}).get("value") if isinstance(salary.get("from"), dict) else salary.get("from")
                    salary_to = salary.get("to", {}).get("value") if isinstance(salary.get("to"), dict) else salary.get("to")
                    currency = salary.get("currencyCode") or salary.get("currency")
                    if isinstance(currency, dict):
                        currency = currency.get("code", "RUR")
                    salary_info = {
                        "from": salary_from,
                        "to": salary_to,
                        "currency": currency or "RUR",
                        "gross": salary.get("gross"),
                    }

            company = v.get("company") or v.get("employer")
            employer_name = company.get("name", "Не указана") if isinstance(company, dict) else str(company) if company else "Не указана"

            area = v.get("area")
            area_name = area.get("name", "Не указан") if isinstance(area, dict) else str(area) if area else "Не указан"

            work_experience = v.get("workExperience")
            experience = v.get("experience")
            if isinstance(work_experience, str):
                experience_name = EXPERIENCE_MAP.get(work_experience, work_experience)
            elif isinstance(experience, dict):
                experience_name = experience.get("name", "Не указан")
            elif isinstance(experience, str):
                experience_name = experience
            else:
                experience_name = "Не указан"

            schedule = v.get("schedule") or v.get("@workSchedule")
            if isinstance(schedule, dict):
                schedule_name = schedule.get("name", "Не указан")
            elif isinstance(schedule, str):
                schedule_name = SCHEDULE_MAP.get(schedule, schedule)
            else:
                schedule_name = "Не указан"

            key_skills = v.get("key_skills", [])
            if key_skills and isinstance(key_skills[0], dict):
                key_skills = [s.get("name", "") for s in key_skills]

            if not key_skills:
                desc_skills = extract_skills_from_text(v.get("description", ""))
                title_skills = extract_keywords_from_title(v.get("name", ""))
                key_skills = list(set(desc_skills + [s.lower() for s in title_skills]))

            vacancy_id = v.get("id") or v.get("vacancyId")
            url = v.get("alternate_url") or f"https://hh.ru/vacancy/{vacancy_id}"
            responses_count = v.get("responsesCount") or v.get("totalResponsesCount")

            results.append({
                "id": vacancy_id,
                "name": v.get("name"),
                "employer": employer_name,
                "area": area_name,
                "experience": experience_name,
                "schedule": schedule_name,
                "salary": salary_info,
                "url": url,
                "description": v.get("description", ""),
                "key_skills": key_skills,
                "responses_count": responses_count,
                "published_at": v.get("published_at") or (v.get("publicationTime", {}).get("$") if isinstance(v.get("publicationTime"), dict) else None),
            })

        return {
            "items": results,
            "found": len(results),
        }
