import requests
import re
from bs4 import BeautifulSoup

# =========================================================
# COOKIES
# =========================================================
cookies = {
    ".AspNetCore.Cookies": "CfDJ8Io4ABHDnZtJgifzvFZjjiey5u51V5YZjKhPD7_mmAaZ5BAyisgqjqkVOC6_2UjOzc8rPggITzT4FQBCk4c1wCNZskreelRkuqLBaYffWht2iPN-c-EfUCrvKvp4vXpfAn1c8yLMu5jcjBcFzxYR5gXQen5uh-xkjDW4pu0OZGeYM-26XcxA0qAHXIf4Lx6PuVAE6S6Yf0M5xXLQ2k7m6YODFDD2eozzOuA5DEggow90GqM_qPlv-3gcb5fhdcLmreLYADD1Iat84kD2b7jYyhXimoR_NkvbR4RkC0Fs7xD_wuiQuAidKhxL6eGN7RJdxON7aRq0xosYShA8BxPWTFZ2J2UduyUk7JuztjL3d2OWPgPwPhaksDlbPoJumiaYP3RZRwArRmU2h4kPeejMeCJvJpmHH6j_c3uKZhcMxfE_Np8bO8Ok3qs9HPJawRJpzlfvGVlVDWMrstQyIhOp3EYiiX9St79RUs7hXOSlE1FYLVsivhSQaxyeo08BvNnwta6Hwz_2wn1ADcfRx4f9TU1RqLqhAicKC_CWDEKBp1SGU9wZ2alOpCI3Gr-tClJX2tI_zqAdR0qgv9Kio_mu9jZJRqdh0kIDOfqeAH9AktjJzuSovbFF26R4a9N1ylJ179J_DhNbg-T_iEEnJDr3AHWVlq2jTaS64LQX59E7ebuo9_KslnYUfYjSWKSiIgsyXxwzsUiWLBkuYFJTf4QHbOmYNkqt6weUQkI2uSN3bk4GbqMlnbiygW6-L2mQD0JQ3gkxoiCrTBtyn5F0tdUDP2tMJ_A_AEQhp7gk9c6QKOwfsxyFJ7PE5q27RTgM2Q9UGlEpB-h-wkfQ3thi9-ZpcPjnEtWysffOSbdVIqDHpRa0nOAYDIqSIXH0CCVy1tYzVwHdJO_-o_Iav-lvuzKNR9Z2JvQBQAVbmE36Zhaids8RLue8dLLkdPBbJv3SOCvI-nEwF5K9yqBxy26NOqWAtJY83MMMq4IsIsuFeqy13TDQcsc19XWi3f3PY4iV-1L8yNNGMzwZvt9ecfUWDq7yZXBKHUfsICHqr9bS6ltmAqEV7bUnNQEBNOtEEbkodYuNzy_oo0G7uTKQblZwVWHgHFcDimCEBxOhlTUcg-U1IlKgOdM8h3mg1YCu7BWETdxkVSCP8DCI7zqH7xkJNCENh-CBN4k_29PDor1UEnwNBu9jH_rIA75nqX-eVAksvte429U4VlBQ1K45nV9AnZCEFuKqogk1EkNIf3DjiM0gsrBh1lvG7eCf-vpXO6itZOUHbjcT2nFnvaOCG3PiCqWXiDY49nJXHlw901OvF_1-On5ileqZf0mJB8DdPyh9V4oYkoF-6P1OvXvY2IgXue9tiI_NZg0JrPVwOYtJ-0K2N2EPIdHWfhH2Th3Ir9GEofrpiFMbKFFIp3S-cXxpIEVIvIPW3wKMsJ1rTUQ_2tkKYrf_5Ji3D-K0gpSJ_8RE57EmvOyKvbvLicE20Foq7G5kJiN81078D8k8ezI7cofGn0LYwjA5jNUzjSfoaHMdu3VIMnGEMHQietBMEFTx3XQcBok1F_1q1M7OSBIhDBTthg57elkFNZQdu1QM4MAqKiQIyhz5WqCCCRPqzAm6IabeT6IsvvawaRiGr81IIFVyXk6zp8DV3caXyA0aAR45YlYwR8hRt1ZH_W1Az9erAjpGL_e3lek2T52CnlARsdnUO_0dxepMnJ-aqzxZa_DQDBHF5yJvgvXammsn_3ePp5lnflvWvxTraoUIAHKKRwQLXDpBAy5NOOl8YuBZFh6g4W90Tq7LllWvhxHW4hWBN8Y903ewErnXbrr8omIdDUSA4UrXo295WsOr61rEIa02ErQJ9NEP9lSHnvG_w9jjAhAVMafSR9Tuq2ziCPPnpliNYt56s48iNXkW3N1RvEpRTG84FIXjkv4i-jW6xZ9Xkp4xfqotRL3ueg_En3_oR4lEIQJyoXxfBPgf5tgoGwTWNmF0iwgPeA0U8Vc20qGiQj22nhi1AliW5HAayuQsuneqfYuJyDwfj6LNXTy6TwAtxrHBwPTJ8qOFRBjzU7D2n6y6VUrZcL4Y7XMAKtDWxPJ2B6zYIKdQEj-dG8iulmp-6diQ5o118hloi_LvTcSYl6uJE0AaMrxM3eevbWFnO_0-4mOYD16ORZoITSUIfVKT8Jf6y35Z33Lp6dgvcGTlF7dSZMp1ClkfHUI8_UjbIxJcjoOuKEYEAkexLqQ2JECxVEwNYNjN5xPvsJ6SB93XsKOaAPbszi_zCNJb7Yp-LOu3hhlDPKgvO6CIZvAxqbfUJiOrDus5reL5Ocjb55vvG2eh1YsWpWolktoaOIN-dfVPLwP31FIhATWeZQxcIjz-7tP9Vkam-m9q8NZBJRPeamA8qixFiDm1QxbeaMP0M6Y8NeTQSK-Hr5z5w2QofAZ8SLid5D4Alnr2HxN5DBpUsbjE_C6KGeyCnfGMgmr7cTw69CGW6WXoqg7K8EfIydC4gggpTETuPcAY8hgNY_j9lIBjaliqzl_wkxKihzptGRqRkbxJQ-dwsGA3kZKnHPzdmpbqbg5vU91cYTc7UkJ4qUlYr6SuW0tTJI5xm99O688EgW9mG8uVlp-osNhMgXjWRbe7ILvWxre-EHrAO0WsfR8yzZauXYONE0Y3e84dIXbvSU4kdFZfWcJ-pVV0KbpnZN1Pesmuq01MNy5ZWvlvyV2b90w",
    ".AspNetCore.Session": "CfDJ8Io4ABHDnZtJgifzvFZjjicjoTh8biqReotTd3JWmNxoqFOT7%2F3W1A6yxL7eretjsx5SPR6dVAy10Mu%2FWIeDKXSiVjRXMfNdjuP0wj4FbgLkLSi4wZudocoPIAIVNsMEK%2Bi7os%2BtP7fV2TnONg1mfnFCtnUZ%2Fp0TYNAtV%2Bu7%2FTts"
}

# =========================================================
# REQUEST CONFIG
# =========================================================
url = "https://decuongmonhoc.tdtu.edu.vn/sinhvien/xemdecuong"

params = {
    "mamon": "504091",
    "ngonngu": "vi",
    "mahedaotao": "K"
}

headers = {
    "User-Agent": "Mozilla/5.0",
    "Referer": "https://decuongmonhoc.tdtu.edu.vn/"
}

# =========================================================
# REQUEST
# =========================================================
r = requests.get(
    url,
    params=params,
    cookies=cookies,
    headers=headers,
    timeout=30
)

print("=" * 80)
print("STATUS:", r.status_code)
print("=" * 80)

if r.status_code != 200:
    print("REQUEST FAILED")
    exit()

print("REQUEST OK")

# =========================================================
# PARSE HTML
# =========================================================
soup = BeautifulSoup(r.text, "html.parser")

print("\nTITLE:")
print(soup.title.text if soup.title else "NO TITLE")

# =========================================================
# RAW TEXT PREVIEW
# =========================================================
text = soup.get_text("\n", strip=True)

print("\nTEXT PREVIEW:")
print(text[:5000])

# =========================================================
# FIND TABLES
# =========================================================
tables = soup.find_all("table")

print("\nTOTAL TABLES:", len(tables))

for i, table in enumerate(tables):
    print("\n" + "=" * 80)
    print("TABLE", i)
    print("=" * 80)

    table_text = table.get_text(" ", strip=True)
    print(table_text[:2000])

# =========================================================
# EXTRACT DESCRIPTION
# =========================================================
description = None

match = re.search(
    r"4\.\s*Brief course content:(.*?)(5\.\s*Student’s tasks:)",
    text,
    re.DOTALL
)

if match:
    description = match.group(1).strip()

# =========================================================
# EXTRACT CLOS
# =========================================================
clos = []

for table in tables:
    table_text = table.get_text(" ", strip=True)

    # detect CLO table
    if "CLOs" in table_text and "PLO" in table_text:

        rows = table.find_all("tr")

        for row in rows:
            cols = row.find_all("td")

            if len(cols) >= 2:

                code = cols[0].get_text(" ", strip=True)
                desc = cols[1].get_text(" ", strip=True)

                # only actual CLO rows
                if code.isdigit():

                    clos.append({
                        "code": f"CLO{code}",
                        "description": desc
                    })

# =========================================================
# EXTRACT TOPICS
# =========================================================
topics = []

for table in tables:

    table_text = table.get_text(" ", strip=True)

    # detect schedule table
    if "Session" in table_text and "Content" in table_text:

        rows = table.find_all("tr")

        for row in rows:

            cols = row.find_all("td")

            if len(cols) >= 2:

                session = cols[0].get_text(" ", strip=True)

                # skip invalid rows
                if not re.search(r"\d", session):
                    continue

                content = cols[1].get_text(" ", strip=True)

                content = re.sub(r"\s+", " ", content)

                # remove duplicated spaces
                content = content.strip()

                # optional short title
                title = content[:120]

                topics.append({
                    "session": session,
                    "title": title,
                    "content": content
                })

# =========================================================
# PRINT RESULTS
# =========================================================
print("\n" + "=" * 80)
print("CLOs")
print("=" * 80)

for clo in clos:
    print(clo)

print("\n" + "=" * 80)
print("TOPICS")
print("=" * 80)

for topic in topics:
    print(topic)

print("\n" + "=" * 80)
print("DESCRIPTION")
print("=" * 80)

print(description)

# =========================================================
# FINAL JSON
# =========================================================
final_json = {
    "course_code": params["mamon"],
    "description": description,
    "clos": clos,
    "topics": topics
}

print("\n" + "=" * 80)
print("FINAL JSON")
print("=" * 80)

print(final_json)