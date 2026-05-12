"""Authenticated crawler for the TDTU syllabus portal.

Flow: (1) POST credentials to the SSO backend, (2) follow the
``Authenticate.aspx?Token=…`` redirect so the cross-domain auth cookie
lands on ``decuongmonhoc.tdtu.edu.vn``, (3) fetch each syllabus page, and
(4) parse out title + course code + Brief course content. Output is a
JSON array consumed by :mod:`ml.data.curriculum.sync`.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

import httpx
from bs4 import BeautifulSoup, NavigableString

from .catalog import COURSES, CurriculumCourse

logger = logging.getLogger(__name__)


SYLLABUS_URL_TEMPLATE = (
    "https://decuongmonhoc.tdtu.edu.vn/sinhvien/xemdecuong"
    "?mamon={code}&ngonngu={lang}&mahedaotao={program}"
)
LOGIN_REFERER = (
    "https://old-stdportal.tdtu.edu.vn/Login/Index"
    "?ReturnUrl=https%3A%2F%2Fold-stdportal.tdtu.edu.vn%2F"
)
LOGIN_URL = (
    "https://old-stdportal.tdtu.edu.vn/Login/SignIn"
    "?ReturnURL=https%3A%2F%2Fold-stdportal.tdtu.edu.vn%2F"
)
LOGIN_ORIGIN = "https://old-stdportal.tdtu.edu.vn"
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)


@dataclass(slots=True)
class SyllabusRecord:
    """One scraped row, ready for the sync step."""

    code: str
    title: str | None
    description: str | None
    source_url: str
    fetched_at: str
    error: str | None = None
    raw_html_path: str | None = None


@dataclass(slots=True)
class CrawlerOptions:
    user: str
    password: str
    program: str = "K"
    language: str = "en"
    output: Path = field(default_factory=lambda: Path("ml/data/syllabi.json"))
    debug_html: Path | None = None
    keep_html_dir: Path | None = None
    sleep_between_courses: float = 0.4
    timeout_sec: float = 30.0
    only_codes: tuple[str, ...] | None = None


class TdtuLoginError(RuntimeError):
    """SSO call did not return ``result: success``."""


def login(client: httpx.Client, *, user: str, password: str) -> None:
    """Drive the SSO chain so subsequent GETs come back authenticated."""
    logger.info("logging in as user=%s …", user)
    resp = client.post(
        LOGIN_URL,
        data={"user": user, "pass": password},
        headers={
            "X-Requested-With": "XMLHttpRequest",
            "Referer": LOGIN_REFERER,
            "Origin": LOGIN_ORIGIN,
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        },
    )
    resp.raise_for_status()
    try:
        payload = resp.json()
    except ValueError as exc:
        raise TdtuLoginError(f"login response was not JSON: {resp.text[:200]}") from exc

    result = (payload.get("result") or "").lower()
    if result != "success":
        raise TdtuLoginError(f"login failed: result={payload.get('result')!r}")

    sso_url = payload.get("url")
    if not sso_url:
        raise TdtuLoginError("login succeeded but no SSO redirect URL was provided")
    logger.info("login ok — exchanging token at SSO …")

    resp2 = client.get(sso_url, follow_redirects=True)
    if resp2.status_code >= 400:
        raise TdtuLoginError(
            f"SSO token exchange failed (status={resp2.status_code} url={sso_url})"
        )


# Regexes are written against the ASP.NET MVC view markup. If the portal
# template changes, this is the only place that needs editing.
_TITLE_RE = re.compile(
    r'font-weight:bold;text-transform:\s*uppercase[^"]*">\s*([^<]+?)\s*</span>',
    re.IGNORECASE,
)
_CODE_RE = re.compile(r"COURSE\s*ID:\s*([A-Z0-9]+)\s*</span>", re.IGNORECASE)
_BRIEF_RE = re.compile(
    r"Brief\s+course\s+content[^<]*</span>(.*?)<div\s+class=\"row\"",
    re.IGNORECASE | re.DOTALL,
)


def _clean_text(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip()


def parse_syllabus(html: str) -> tuple[str | None, str | None, str | None]:
    """Pull ``(title, course_code, description)`` from one syllabus HTML."""
    title_m = _TITLE_RE.search(html)
    code_m = _CODE_RE.search(html)

    description: str | None = None
    brief_m = _BRIEF_RE.search(html)
    if brief_m:
        inner = BeautifulSoup(brief_m.group(1), "lxml")
        chunks: list[str] = []
        for node in inner.descendants:
            if isinstance(node, NavigableString):
                txt = _clean_text(str(node))
                if txt:
                    chunks.append(txt)
        if chunks:
            description = _clean_text(" ".join(chunks))

    title = _clean_text(title_m.group(1)) if title_m else None
    code = code_m.group(1).strip() if code_m else None
    return title, code, description


def fetch_one(
    client: httpx.Client,
    *,
    code: str,
    language: str,
    program: str,
) -> tuple[str, str]:
    """Fetch the syllabus HTML for one course code."""
    url = SYLLABUS_URL_TEMPLATE.format(code=code, lang=language, program=program)
    resp = client.get(url, follow_redirects=True)
    if "txtUser" in resp.text and "txtPass" in resp.text:
        # Bounced to the SSO form — likely a race against the cookie
        # bridge. Retry once before giving up.
        logger.warning("course=%s landed on the login page — retrying once", code)
        time.sleep(0.5)
        resp = client.get(url, follow_redirects=True)
    resp.raise_for_status()
    return str(resp.url), resp.text


def crawl_all(opts: CrawlerOptions) -> list[SyllabusRecord]:
    """Log in once, then iterate the catalog and write ``opts.output``."""
    targets: Iterable[CurriculumCourse] = COURSES
    if opts.only_codes:
        only = set(opts.only_codes)
        targets = [c for c in COURSES if c.code in only]
        missing = only - {c.code for c in COURSES}
        if missing:
            logger.warning("ignoring unknown course codes: %s", sorted(missing))

    records: list[SyllabusRecord] = []
    # http2 off — some redirect hosts negotiate it weirdly with httpx.
    with httpx.Client(
        timeout=opts.timeout_sec,
        headers={"User-Agent": DEFAULT_USER_AGENT},
        follow_redirects=True,
    ) as client:
        login(client, user=opts.user, password=opts.password)

        for idx, course in enumerate(targets, start=1):
            now = datetime.now(timezone.utc).isoformat()
            url = SYLLABUS_URL_TEMPLATE.format(
                code=course.code, lang=opts.language, program=opts.program,
            )
            logger.info(
                "[%d/%d] fetching code=%s expected_title=%r",
                idx,
                len(list(targets)) if isinstance(targets, list) else "?",
                course.code,
                course.title_en,
            )
            try:
                source_url, html = fetch_one(
                    client, code=course.code, language=opts.language, program=opts.program,
                )
            except httpx.HTTPError as exc:
                logger.error("code=%s transport error: %s", course.code, exc)
                records.append(
                    SyllabusRecord(
                        code=course.code,
                        title=course.title_en,
                        description=None,
                        source_url=url,
                        fetched_at=now,
                        error=f"http_error: {exc}",
                    )
                )
                continue

            title, parsed_code, description = parse_syllabus(html)
            final_title = title or course.title_en
            if parsed_code and parsed_code != course.code:
                logger.warning(
                    "code mismatch — catalog=%s portal=%s; keeping portal value",
                    course.code, parsed_code,
                )
                final_code = parsed_code
            else:
                final_code = course.code

            raw_html_path = None
            if opts.keep_html_dir is not None:
                opts.keep_html_dir.mkdir(parents=True, exist_ok=True)
                p = opts.keep_html_dir / f"{final_code}.html"
                p.write_text(html, encoding="utf-8")
                raw_html_path = str(p)

            err = None if description else "missing_brief_content"
            if err:
                logger.warning("code=%s parsed but no Brief course content found", final_code)

            records.append(
                SyllabusRecord(
                    code=final_code,
                    title=final_title,
                    description=description,
                    source_url=source_url,
                    fetched_at=now,
                    error=err,
                    raw_html_path=raw_html_path,
                )
            )

            if opts.debug_html is not None and idx == 1:
                opts.debug_html.parent.mkdir(parents=True, exist_ok=True)
                opts.debug_html.write_text(html, encoding="utf-8")
                logger.info("saved debug HTML to %s", opts.debug_html)

            if opts.sleep_between_courses > 0 and idx < len(COURSES):
                time.sleep(opts.sleep_between_courses)

    opts.output.parent.mkdir(parents=True, exist_ok=True)
    opts.output.write_text(
        json.dumps([asdict(r) for r in records], indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    ok = sum(1 for r in records if r.description)
    logger.info(
        "wrote %d records (%d with descriptions) to %s", len(records), ok, opts.output,
    )
    return records


def _parse_args(argv: list[str]) -> CrawlerOptions:
    p = argparse.ArgumentParser(
        description=(
            "Authenticated crawl of TDTU's syllabus portal. Writes a JSON file "
            "that ml.data.curriculum.sync then upserts into Supabase."
        )
    )
    p.add_argument("--user", default=os.getenv("TDTU_USER"), help="Student ID (or TDTU_USER env).")
    p.add_argument("--password", "--pass", dest="password", default=os.getenv("TDTU_PASS"),
                   help="SSO password (or TDTU_PASS env).")
    p.add_argument("--language", default="en")
    p.add_argument("--program", default="K")
    p.add_argument("--out", type=Path, default=Path("ml/data/syllabi.json"))
    p.add_argument("--debug-html", type=Path, default=None,
                   help="If set, dump the first fetched HTML page here.")
    p.add_argument("--keep-html-dir", type=Path, default=None,
                   help="If set, save every fetched page as <code>.html here.")
    p.add_argument("--only", type=str, default=None,
                   help="Comma-separated list of codes (e.g. '501031,502045').")
    p.add_argument("--sleep", type=float, default=0.4,
                   help="Politeness delay between course fetches (seconds).")
    args = p.parse_args(argv)
    if not args.user or not args.password:
        p.error("--user and --password are required (or set TDTU_USER / TDTU_PASS)")
    only_codes = (
        tuple(c.strip() for c in args.only.split(",") if c.strip()) if args.only else None
    )
    return CrawlerOptions(
        user=args.user,
        password=args.password,
        program=args.program,
        language=args.language,
        output=args.out,
        debug_html=args.debug_html,
        keep_html_dir=args.keep_html_dir,
        sleep_between_courses=args.sleep,
        only_codes=only_codes,
    )


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=os.getenv("CRAWLER_LOG_LEVEL", "INFO"),
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )
    opts = _parse_args(argv if argv is not None else sys.argv[1:])
    records = crawl_all(opts)
    ok = sum(1 for r in records if r.description)
    failed = len(records) - ok
    print(f"crawled: {len(records)} | with_description: {ok} | failed: {failed}")
    return 0 if failed == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
