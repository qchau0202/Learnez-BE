import json
import time
import requests
from bs4 import BeautifulSoup

# =========================================================
# CONFIG
# =========================================================

CURRICULUM_URL = (
    "https://learninginfo.tdtu.edu.vn/"
    "XemCTDT_GV_V2/GetChuongTrinhDaoTao"
)

SYLLABUS_URL = (
    "https://decuongmonhoc.tdtu.edu.vn/"
    "sinhvien/xemdecuong"
)

# =========================================================
# LOGIN COOKIES
# =========================================================
# COPY COOKIE TỪ BROWSER
# F12 -> Application -> Cookies
# =========================================================

cookies = {
    "ASP.NET_SessionId": "slboy5kiy5j4syhvxvofkmql",
    ".ASPXAUTH": "E2523E7F003C00AC61774AB75B4AA2DA3BE6B505BE8CAA98B082C9B77B415932F2E9EB22878B9B9BE2F3CBC69A8DD1A3CD9098A1F34D137B09DC05A543482A7BD8861E10D3663B1BCAEED4CAAD61D25D4836B88D4D4FDC59B2B83C1068C9D72F1F929EF413B428792625D34B6CDD8016B953E6EF41901F0CDC6E05BF5963ACCBF87CAD4D3640769BF73D9ECF3603B6E6"
}

headers = {
    "User-Agent": "Mozilla/5.0",
    "Referer": "https://decuongmonhoc.tdtu.edu.vn/"
}

# =========================================================
# GET CURRICULUM
# =========================================================

def get_curriculum():

    params = {
        "hdt": "K",
        "nk": "2023",
        "ng": "502",
        "macn": "",
        "xemtichluy": "tichluy",
        "mssv": "523K0021",
        "xemchuyensau": "",
        "ngonngu": "vi",
        "lop": "23K50201",
        "khoilop": "23K50201"
    }

    r = requests.get(
        CURRICULUM_URL,
        params=params,
        cookies=cookies,
        headers=headers,
        timeout=30
    )

    r.raise_for_status()

    return r.json()

# =========================================================
# EXTRACT COURSE LIST
# =========================================================

def extract_courses(curriculum_json):

    courses = []

    for item in curriculum_json:
    # TDTU API returns a dictionary. The list of courses is usually in 'chiTietKhoi'
    # or 'dtHocPhanList'. If an error occurs, it returns {'loi': 'message'}.
    if isinstance(curriculum_json, dict):
        if "loi" in curriculum_json:
            print(f"ERROR FROM PORTAL: {curriculum_json['loi']}")
            return []
        items = curriculum_json.get("chiTietKhoi") or curriculum_json.get("dtHocPhanList") or []
    else:
        items = curriculum_json

    for item in items:
        if not isinstance(item, dict):
            continue

        course = {
            "course_code": item.get("MonHocID"),
            "course_name_vi": item.get("TenMonHoc"),
            "course_name_en": item.get("TenMH_TiengAnh"),
            "credits": item.get("SoDVHocTap"),
            "semester": item.get("HocKy"),
            "academic_year": item.get("NamHoc")
        }

        courses.append(course)

    return courses

# =========================================================
# CLEAN TEXT
# =========================================================

def clean_text(text):

    if not text:
        return ""

    text = text.replace("\n", " ")
    text = text.replace("\r", " ")
    text = " ".join(text.split())

    return text.strip()

# =========================================================
# FETCH SYLLABUS HTML
# =========================================================

def fetch_syllabus_html(course_code):

    params = {
        "mamon": course_code,
        "ngonngu": "vi",
        "mahedaotao": "K"
    }

    r = requests.get(
        SYLLABUS_URL,
        params=params,
        cookies=cookies,
        headers=headers,
        timeout=30
    )

    r.raise_for_status()

    return r.text

# =========================================================
# PARSE COURSE NAME
# =========================================================

def parse_course_name(text):

    lines = text.split("\n")

    for i, line in enumerate(lines):

        line = line.strip()

        if line == "COURSE SYLLABUS":

            if i + 1 < len(lines):
                return clean_text(lines[i + 1])

    return ""

# =========================================================
# PARSE DESCRIPTION
# =========================================================

def parse_description(text):

    start = "4. Brief course content:"
    end = "5. Student’s tasks:"

    if start in text and end in text:

        description = text.split(start)[1].split(end)[0]

        return clean_text(description)

    return ""

# =========================================================
# PARSE CLOs
# =========================================================

def parse_clos(tables):

    clos = []

    if len(tables) < 3:
        return clos

    clo_table = tables[2]

    rows = clo_table.find_all("tr")

    for row in rows:

        cols = row.find_all("td")

        if len(cols) >= 2:

            code = clean_text(cols[0].get_text())
            desc = clean_text(cols[1].get_text())

            if code.isdigit():

                clos.append({
                    "code": f"CLO{code}",
                    "description": desc
                })

    return clos

# =========================================================
# PARSE TOPICS
# =========================================================

def parse_topics(tables):

    topics = []

    if len(tables) < 5:
        return topics

    topic_table = tables[4]

    rows = topic_table.find_all("tr")

    for row in rows:

        cols = row.find_all("td")

        if len(cols) < 2:
            continue

        session = clean_text(cols[0].get_text())
        content = clean_text(cols[1].get_text())

        if not session:
            continue

        if "Chapter" in session:
            continue

        if session in ["T", "E", "P", "D"]:
            continue

        if len(content) < 20:
            continue

        title = content[:120]

        topics.append({
            "session": session,
            "title": title,
            "content": content
        })

    return topics

# =========================================================
# PARSE FULL SYLLABUS
# =========================================================

def crawl_syllabus(course_code):

    print("=" * 80)
    print(f"CRAWLING COURSE: {course_code}")
    print("=" * 80)

    html = fetch_syllabus_html(course_code)

    soup = BeautifulSoup(html, "html.parser")

    text = soup.get_text("\n")

    tables = soup.find_all("table")

    course_name = parse_course_name(text)

    description = parse_description(text)

    clos = parse_clos(tables)

    topics = parse_topics(tables)

    result = {
        "course_code": course_code,
        "course_name": course_name,
        "description": description,
        "clos": clos,
        "topics": topics
    }

    return result

# =========================================================
# MAIN
# =========================================================

def main():

    print("=" * 80)
    print("GETTING CURRICULUM")
    print("=" * 80)

    curriculum_json = get_curriculum()

    print(f"TOTAL RAW ITEMS: {len(curriculum_json)}")

    courses = extract_courses(curriculum_json)

    print(f"TOTAL COURSES: {len(courses)}")

    all_courses = []

    for idx, course in enumerate(courses):

        course_code = course["course_code"]

        print("\n")
        print(f"[{idx + 1}/{len(courses)}] {course_code}")

        try:

            syllabus_data = crawl_syllabus(course_code)

            merged = {
                **course,
                **syllabus_data
            }

            all_courses.append(merged)

            print("SUCCESS")

        except Exception as e:

            print(f"ERROR: {e}")

        # RATE LIMIT
        time.sleep(1)

    print("\n")
    print("=" * 80)
    print("FINAL RESULT")
    print("=" * 80)

    print(json.dumps(all_courses[:2], indent=2, ensure_ascii=False))

    # SAVE JSON FILE
    with open("courses.json", "w", encoding="utf-8") as f:

        json.dump(
            all_courses,
            f,
            ensure_ascii=False,
            indent=2
        )

    print("\nSaved to courses.json")

# =========================================================
# RUN
# =========================================================

if __name__ == "__main__":
    main()