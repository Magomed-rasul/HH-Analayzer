from collections import Counter
from typing import List, Dict, Any
import re
import requests
from itertools import combinations
import time

COMMON_SKILLS = [
    "python", "javascript", "java", "c++", "c#", "php", "ruby", "go", "rust", "typescript",
    "sql", "nosql", "postgresql", "mysql", "mongodb", "redis",
    "django", "flask", "fastapi", "spring", "express", "react", "vue", "angular",
    "docker", "kubernetes", "aws", "azure", "gcp", "linux", "git", "ci/cd",
    "html", "css", "sass", "less",
    "rest", "grpc", "graphql", "api",
    "agile", "scrum", "jira", "confluence",
    "machine learning", "ml", "ai", "tensorflow", "pytorch",
    "pandas", "numpy", "scipy",
    "oop", "solid", "tdd", "bdd",
    "node.js", "next.js", "nuxt.js", "svelte",
    "flutter", "kotlin", "swift", "dart",
    "terraform", "ansible", "jenkins", "gitlab",
    "figma", "postman", "elasticsearch", "kafka", "rabbitmq",
    "tailwind", "bootstrap", "webpack", "vite",
    "django rest", "graphql",
]

TITLE_ROLES = [
    "fullstack", "full stack", "backend", "frontend", "front-end", "back-end",
    "devops", "mobile", "ios", "android", "data", "qa", "lead",
    "senior", "middle", "junior", "стажер",
]

EXCLUDE_KEYWORDS = {r.lower() for r in TITLE_ROLES + ["ai"]}

_rates_cache = {"rates": None, "ts": 0}


def _get_exchange_rates() -> dict:
    now = time.time()
    if _rates_cache["rates"] and now - _rates_cache["ts"] < 3600:
        return _rates_cache["rates"]
    try:
        resp = requests.get("https://api.exchangerate-api.com/v4/latest/RUB", timeout=5)
        data = resp.json().get("rates", {})
        inv = {k: 1 / v for k, v in data.items() if v > 0}
        inv["RUR"] = 1
        inv["RUB"] = 1
        if "BYN" in inv:
            inv["BYR"] = inv["BYN"]
        _rates_cache["rates"] = inv
        _rates_cache["ts"] = now
        return inv
    except Exception:
        return {"RUB": 1, "RUR": 1, "USD": 90, "EUR": 95, "KZT": 0.18, "UZS": 0.006, "GEL": 33, "BYN": 28, "BYR": 28}


def extract_skills_from_text(text: str) -> List[str]:
    if not text:
        return []
    found = []
    text_lower = text.lower()
    for skill in COMMON_SKILLS:
        if skill in text_lower:
            found.append(skill)
    return list(set(found))


def extract_keywords_from_title(title: str) -> List[str]:
    if not title:
        return []
    keywords = []

    paren_match = re.findall(r'\(([^)]+)\)', title)
    for group in paren_match:
        parts = re.split(r'[,/|+]', group)
        for part in parts:
            part = part.strip()
            if part:
                keywords.append(part)

    before_paren = re.sub(r'\([^)]*\)', '', title).strip()
    for role in TITLE_ROLES:
        m = re.search(r'(?<!\w)' + re.escape(role) + r'(?!\w)', before_paren, re.IGNORECASE)
        if m:
            keywords.append(m.group(0))

    title_lower = title.lower()
    for skill in COMMON_SKILLS:
        pattern = r'(?<!\w)' + re.escape(skill) + r'(?!\w)'
        m = re.search(pattern, title_lower)
        if m:
            original = skill
            m_orig = re.search(pattern, title, re.IGNORECASE)
            if m_orig:
                original = m_orig.group(0)
            keywords.append(original)

    seen = set()
    unique = []
    for kw in keywords:
        kw_clean = kw.strip()
        if kw_clean and kw_clean.lower() not in seen:
            seen.add(kw_clean.lower())
            unique.append(kw_clean)
    return unique


def _median(values: list) -> float:
    if not values:
        return 0
    s = sorted(values)
    n = len(s)
    if n % 2 == 1:
        return s[n // 2]
    return (s[n // 2 - 1] + s[n // 2]) / 2


def _salary_to_rub(salary: dict, rates: dict) -> tuple:
    fr = salary.get("from")
    to = salary.get("to")
    cur = salary.get("currency", "RUR")
    if isinstance(cur, dict):
        cur = cur.get("code", "RUR")

    rate = rates.get(cur, 1)
    is_foreign = cur not in ("RUR", "RUB")

    vals = []
    if fr and fr > 0:
        vals.append(fr * rate)
    if to and to > 0:
        vals.append(to * rate)
    if not vals:
        return 0, cur, is_foreign
    return sum(vals) / len(vals), cur, is_foreign


def analyze_vacancies(vacancies: List[Dict[str, Any]]) -> Dict[str, Any]:
    rates = _get_exchange_rates()
    all_skills_per_vacancy = []
    all_title_keywords_per_vacancy = []
    experience_counter = Counter()
    schedule_counter = Counter()
    salary_values = []
    responses_list = []

    for v in vacancies:
        skills_from_key = [s.lower() for s in v.get("key_skills", []) if isinstance(s, str)]

        if skills_from_key:
            vacancy_skills = skills_from_key
        else:
            skills_from_desc = extract_skills_from_text(v.get("description", ""))
            skills_from_title = extract_skills_from_text(v.get("name", ""))
            vacancy_skills = list(set(skills_from_desc + skills_from_title))

        all_skills_per_vacancy.append(vacancy_skills)

        title_kw = set(k.lower() for k in extract_keywords_from_title(v.get("name", "")))
        desc_kw = set(k.lower() for k in extract_skills_from_text(v.get("description", "")))
        vacancy_kw = title_kw | desc_kw
        all_title_keywords_per_vacancy.append(vacancy_kw)

        experience_counter[v.get("experience", "Не указан")] += 1
        schedule_counter[v.get("schedule", "Не указан")] += 1

        salary = v.get("salary")
        if salary:
            mid, cur, is_foreign = _salary_to_rub(salary, rates)
            if mid > 0:
                salary_values.append(mid)
            salary["_converted_rub"] = round(mid) if mid else None
            salary["_original_currency"] = cur
            salary["_is_foreign"] = is_foreign

        responses = v.get("responses_count")
        if responses is not None:
            responses_list.append(responses)

    flat_skills = [s for sublist in all_skills_per_vacancy for s in sublist]
    skills_counter = Counter(flat_skills)
    top_skills = [(s, c) for s, c in skills_counter.most_common(50) if s.lower() not in EXCLUDE_KEYWORDS][:15]

    flat_title_kw = [kw for s in all_title_keywords_per_vacancy for kw in s]
    title_kw_counter = Counter(flat_title_kw)
    top_title_keywords = [(s, c) for s, c in title_kw_counter.most_common(50) if s.lower() not in EXCLUDE_KEYWORDS][:15]

    tandem_counter = Counter()
    for skills in all_skills_per_vacancy:
        unique = sorted(set(s for s in skills if s.lower() not in EXCLUDE_KEYWORDS))
        if len(unique) >= 2:
            for pair in combinations(unique, 2):
                tandem_counter[pair] += 1
    top_tandems = tandem_counter.most_common(10)

    salary_chart = []
    if salary_values:
        salary_chart = [
            {"label": "Мин.", "value": round(min(salary_values))},
            {"label": "Медиана", "value": round(_median(salary_values))},
            {"label": "Макс.", "value": round(max(salary_values))},
        ]

    return {
        "total_vacancies": len(vacancies),
        "top_skills": [{"skill": s, "count": c} for s, c in top_skills],
        "top_title_keywords": [{"keyword": s, "count": c} for s, c in top_title_keywords],
        "top_tandems": [{"pair": f"{a} + {b}", "count": c} for (a, b), c in top_tandems],
        "experience_distribution": dict(experience_counter),
        "schedule_distribution": dict(schedule_counter),
        "salary_chart": salary_chart,
        "responses_median": round(_median(responses_list)) if responses_list else 0,
    }
