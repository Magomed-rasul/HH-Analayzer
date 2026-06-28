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
            import os
            cached_token = os.environ.get("HH_ACCESS_TOKEN")
            if cached_token:
                self.access_token = cached_token
                self.session.headers["Authorization"] = f"Bearer {cached_token}"
            else:
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
            import os
            env_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), ".env")
            if os.path.exists(env_path):
                lines = []
                with open(env_path, "r") as f:
                    lines = [l for l in f.readlines() if not l.startswith("HH_ACCESS_TOKEN")]
                lines.append(f"HH_ACCESS_TOKEN={self.access_token}")
                with open(env_path, "w") as f:
                    f.write("".join(lines))

    def search_vacancies(
        self,
        text: str,
        area: Optional[str] = None,
        schedule: Optional[str] = None,
        page_limit: int = 1,
        title_only: bool = False,
    ) -> dict:
        if self.access_token:
            return self._search_api(text, area, schedule, page_limit, title_only)
        return self._search_scrape(text, area, schedule, page_limit, title_only)

    def _search_api(
        self,
        text: str,
        area: Optional[str] = None,
        schedule: Optional[str] = None,
        page_limit: int = 1,
        title_only: bool = False,
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
        if title_only:
            params["search_field"] = "name"

        all_results = []
        for page in range(page_limit):
            params["page"] = page
            response = self.session.get(f"{HH_API_BASE}/vacancies", params=params)
            response.raise_for_status()
            data = response.json()
            all_results.extend(data.get("items", []))
            if page >= data.get("pages", 1) - 1:
                break

        try:
            scraped_map = self._scrape_search_page(text, area, schedule, title_only, page_limit)
            scraped_by_name = {}
            for sv in scraped_map.values():
                name = (sv.get("name") or "").lower().strip()
                if name:
                    scraped_by_name[name] = sv
            for v in all_results:
                name = (v.get("name") or "").lower().strip()
                if name and name in scraped_by_name:
                    sv = scraped_by_name[name]
                    rc = sv.get("totalResponsesCount")
                    if rc is None:
                        rc = sv.get("responsesCount")
                    if rc is not None:
                        v["responsesCount"] = rc
        except Exception:
            pass

        need_enrich = [v for v in all_results[:50] if not v.get("description") and v.get("id")]
        if need_enrich:
            with ThreadPoolExecutor(max_workers=8) as executor:
                future_to_v = {executor.submit(self._fetch_vacancy_detail, v["id"]): v for v in need_enrich}
                for future in as_completed(future_to_v):
                    v = future_to_v[future]
                    try:
                        detail = future.result()
                        if detail:
                            if detail.get("description"):
                                v["description"] = detail["description"]
                            if detail.get("key_skills"):
                                v["key_skills"] = [s.get("name", "") for s in detail.get("key_skills", []) if isinstance(s, dict)]
                    except Exception:
                        pass

        need_meta = [v for v in all_results[:50] if not v.get("schedule")]
        if need_meta:
            with ThreadPoolExecutor(max_workers=4) as executor:
                future_to_v = {executor.submit(self._fetch_vacancy_meta, v["id"]): v for v in need_meta}
                for future in as_completed(future_to_v):
                    v = future_to_v[future]
                    try:
                        meta = future.result()
                        if meta:
                            if meta.get("schedule"):
                                v["schedule"] = meta["schedule"]
                    except Exception:
                        pass

        return self._format_results(all_results)

    def _scrape_search_page(self, text, area=None, schedule=None, title_only=False, page_limit=5):
        import urllib.parse
        scraped_map = {}
        try:
            params = {"text": text, "order_by": "publication_time", "per_page": 50}
            if area:
                params["area"] = area
            if schedule:
                params["schedule"] = schedule
            if title_only:
                params["search_field"] = "name"
            for pg in range(1, page_limit + 1):
                params["page"] = pg
                qs = urllib.parse.urlencode(params)
                html = self.scrape_session.get(f"https://hh.ru/search/vacancy?{qs}", timeout=15).text
                if self._is_captcha_page(html):
                    break
                for sv in self._parse_scraped_page(html):
                    vid = sv.get("vacancyId")
                    if vid:
                        scraped_map[vid] = sv
                if pg < page_limit:
                    time.sleep(2)
        except Exception:
            pass
        return scraped_map

    def _scrape_search_page_sorted(self, text, area=None, schedule=None, title_only=False, sort="relevance", page_limit=5):
        import urllib.parse
        scraped_map = {}
        try:
            params = {"text": text, "order_by": sort, "per_page": 50}
            if area:
                params["area"] = area
            if schedule:
                params["schedule"] = schedule
            if title_only:
                params["search_field"] = "name"
            for pg in range(1, page_limit + 1):
                params["page"] = pg
                qs = urllib.parse.urlencode(params)
                html = self.scrape_session.get(f"https://hh.ru/search/vacancy?{qs}", timeout=15).text
                if self._is_captcha_page(html):
                    break
                for sv in self._parse_scraped_page(html):
                    vid = sv.get("vacancyId")
                    if vid:
                        scraped_map[vid] = sv
        except Exception:
            pass
        return scraped_map

    def _fetch_vacancy_detail(self, vacancy_id) -> dict:
        try:
            _rate_limit(0.05)
            resp = self.session.get(f"{HH_API_BASE}/vacancies/{vacancy_id}", timeout=3)
            if resp.status_code == 200:
                return resp.json()
        except Exception:
            pass
        return {}

    def _enrich_with_api_details(self, vacancies: list) -> list:
        ids = []
        for v in vacancies:
            vid = v.get("id")
            if vid and not v.get("description"):
                ids.append(vid)

        details_map = {}
        with ThreadPoolExecutor(max_workers=8) as executor:
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
                _rate_limit(0.5)
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

    def _fetch_description_fast(self, vacancy_id):
        for attempt in range(2):
            try:
                _rate_limit(0.5)
                resp = self.scrape_session.get(f"https://hh.ru/vacancy/{vacancy_id}", timeout=15)
                html = resp.text
                if self._is_captcha_page(html):
                    if attempt == 0:
                        time.sleep(5)
                        continue
                    return None
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
                return None
        return None

    def _fetch_vacancy_meta(self, vacancy_id):
        for attempt in range(2):
            try:
                _rate_limit(0.5)
                resp = self.scrape_session.get(f"https://hh.ru/vacancy/{vacancy_id}", timeout=15)
                html = resp.text
                if self._is_captcha_page(html):
                    if attempt == 0:
                        time.sleep(5)
                        continue
                    return None
                result = {}
                m = re.search(r'"description":\s*"((?:[^"\\]|\\.)*)"', html)
                if m:
                    result["description"] = json.loads('"' + m.group(1) + '"')
                sm = re.search(r'"schedule":\{"id":"([^"]+)","name":"([^"]+)"', html)
                if sm:
                    result["schedule"] = {"id": sm.group(1), "name": sm.group(2)}
                else:
                    for sid, sname in SCHEDULE_MAP.items():
                        if re.search(rf'{re.escape(sname)}', html):
                            result["schedule"] = {"id": sid, "name": sname}
                            break
                return result
            except Exception:
                if attempt == 0:
                    time.sleep(5)
                    continue
                return None
        return None

    def fetch_descriptions_batch(self, vacancies: list) -> list:
        need_scrape = []
        for i, v in enumerate(vacancies):
            vid = v.get("id")
            if not vid:
                continue
            missing_desc = not v.get("description")
            missing_schedule = not v.get("schedule")
            missing_responses = v.get("responsesCount") is None
            if missing_desc or (missing_schedule and missing_responses):
                need_scrape.append((i, vid, missing_desc, missing_schedule, missing_responses))

        if not need_scrape:
            return vacancies

        meta_map = {}
        consecutive_none = 0
        with ThreadPoolExecutor(max_workers=4) as executor:
            future_to_idx = {executor.submit(self._fetch_vacancy_meta, vid): idx for idx, vid, _, _, _ in need_scrape}
            for future in as_completed(future_to_idx):
                idx = future_to_idx[future]
                try:
                    result = future.result()
                except Exception:
                    result = None
                if result is None:
                    consecutive_none += 1
                    meta_map[idx] = {}
                    if consecutive_none >= 3:
                        for f in future_to_idx:
                            f.cancel()
                        break
                else:
                    consecutive_none = 0
                    meta_map[idx] = result

        for i, v in enumerate(vacancies):
            meta = meta_map.get(i, {})
            if meta:
                if not v.get("description") and meta.get("description"):
                    v["description"] = meta["description"]
                if not v.get("schedule") and meta.get("schedule"):
                    v["schedule"] = meta["schedule"]
                if v.get("responsesCount") is None and meta.get("responsesCount") is not None:
                    v["responsesCount"] = meta["responsesCount"]

        return vacancies

    def _enrich_with_scrape_details(self, vacancies: list) -> list:
        ids = []
        for v in vacancies:
            vid = v.get("vacancyId") or v.get("id")
            if vid:
                ids.append((len(ids), vid))

        meta_map = {}
        with ThreadPoolExecutor(max_workers=1) as executor:
            future_to_idx = {executor.submit(self._fetch_vacancy_meta, vid): idx for idx, vid in ids}
            for future in as_completed(future_to_idx):
                idx = future_to_idx[future]
                try:
                    meta_map[idx] = future.result() or {}
                except Exception:
                    meta_map[idx] = {}

        for i, v in enumerate(vacancies):
            meta = meta_map.get(i, {})
            if meta:
                if not v.get("description") and meta.get("description"):
                    v["description"] = meta["description"]
                if meta.get("schedule"):
                    v["schedule"] = meta["schedule"]
                if meta.get("responsesCount") is not None:
                    v["responsesCount"] = meta["responsesCount"]

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
        title_only: bool = False,
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
            if len(vacancies) == 0:
                break
            if page + 1 < page_limit:
                time.sleep(2)

        all_results = all_results[:250]
        if title_only:
            query_words = text.lower().split()
            all_results = [v for v in all_results if all(w in (v.get("name") or "").lower() for w in query_words)]
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

            desc_text = ((v.get("description") or "") + " " + (v.get("name") or "")).lower()
            if any(w in desc_text for w in ["удалён", "удален", "remote"]):
                schedule_name = "Удалённо"
            elif any(w in desc_text for w in ["гибрид", "hybrid"]):
                schedule_name = "Гибрид"
            elif schedule_name == "Полный день" or schedule_name == "Не указан":
                schedule_name = "Офис"

            key_skills = v.get("key_skills", [])
            if key_skills and isinstance(key_skills[0], dict):
                key_skills = [s.get("name", "") for s in key_skills]

            if not key_skills:
                desc_skills = extract_skills_from_text(v.get("description", ""))
                title_skills = extract_keywords_from_title(v.get("name", ""))
                key_skills = list(set(desc_skills + [s.lower() for s in title_skills]))

            vacancy_id = v.get("id") or v.get("vacancyId")
            url = v.get("alternate_url") or f"https://hh.ru/vacancy/{vacancy_id}"
            responses_count = v.get("totalResponsesCount")
            if responses_count is None:
                responses_count = v.get("responsesCount")
            if responses_count is None:
                responses_count = v.get("responses_count")
            if responses_count == 0:
                responses_count = None

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
