from __future__ import annotations

import hashlib
import math
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from calendar import month_name
from dataclasses import dataclass
from datetime import UTC, date, datetime
from datetime import time as datetime_time
from email.message import Message
from html.parser import HTMLParser
from zoneinfo import ZoneInfo

SOURCE_RSS = "rss"
SOURCE_SUMMARY = "summary"
SOURCE_TABLE_B1 = "table_b1"
SOURCE_URLS = {
    SOURCE_RSS: "https://www.bls.gov/feed/empsit.rss",
    SOURCE_SUMMARY: "https://www.bls.gov/news.release/empsit.nr0.htm",
    SOURCE_TABLE_B1: "https://www.bls.gov/news.release/empsit.t17.htm",
}
BLS_FEED_URL = SOURCE_URLS[SOURCE_RSS]
SOURCE_NAMES = tuple(SOURCE_URLS)
EASTERN = ZoneInfo("America/New_York")
_MONTHS = {month_name[month]: month for month in range(1, 13)}
_NS = {"atom": "http://www.w3.org/2005/Atom"}
_MAX_PAYLOAD = 4_000_000


class BLSReleaseError(ValueError):
    """An official BLS payload cannot be mapped to one required release value."""


class BLSStaleReleaseError(BLSReleaseError):
    """Official content is valid BLS content, but not the required release."""


@dataclass(frozen=True)
class ParsedRelease:
    published_at_utc: str
    entry_id: str
    release_url: str
    reference_year: int
    reference_month: int
    period_name: str
    change_jobs: int
    provenance_text: str


@dataclass(frozen=True)
class SourceAttempt:
    source_name: str
    source_url: str
    phase: str
    request_number: int
    request_started_wall_ns: int
    request_started_monotonic_ns: int
    first_byte_wall_ns: int | None
    first_byte_monotonic_ns: int | None
    body_complete_wall_ns: int
    body_complete_monotonic_ns: int
    parse_complete_wall_ns: int
    parse_complete_monotonic_ns: int
    http_status: int | None
    http_date: str | None
    age: str | None
    etag: str | None
    last_modified: str | None
    cache_control: str | None
    payload: bytes
    payload_sha256: str
    parsed_value: int | None
    reference_year: int | None
    reference_month: int | None
    published_at_utc: str | None
    entry_id: str | None
    release_url: str | None
    provenance_text: str | None
    validation_result: str
    rejection_reason: str | None

    @property
    def valid(self) -> bool:
        return self.validation_result == "VALID" and self.parsed_value is not None

    @property
    def parser_runtime_us(self) -> float:
        return (
            self.parse_complete_monotonic_ns - self.body_complete_monotonic_ns
        ) / 1_000.0


@dataclass(frozen=True)
class ReleaseEvidence:
    source_name: str
    source_url: str
    payload: bytes
    payload_sha256: str
    receipt_wall_time_ns: int
    receipt_monotonic_ns: int
    receipt_iso_utc: str
    valid_wall_time_ns: int
    valid_monotonic_ns: int
    published_at_utc: str
    entry_id: str
    release_url: str
    reference_year: int
    reference_month: int
    period_name: str
    change_jobs: int
    provenance_text: str


def evidence_from_attempt(attempt: SourceAttempt) -> ReleaseEvidence:
    if not attempt.valid:
        raise BLSReleaseError("cannot promote an invalid source attempt")
    assert attempt.parsed_value is not None
    assert attempt.reference_year is not None
    assert attempt.reference_month is not None
    assert attempt.published_at_utc is not None
    return ReleaseEvidence(
        source_name=attempt.source_name,
        source_url=attempt.source_url,
        payload=attempt.payload,
        payload_sha256=attempt.payload_sha256,
        receipt_wall_time_ns=attempt.body_complete_wall_ns,
        receipt_monotonic_ns=attempt.body_complete_monotonic_ns,
        receipt_iso_utc=_iso_utc(attempt.body_complete_wall_ns),
        valid_wall_time_ns=attempt.parse_complete_wall_ns,
        valid_monotonic_ns=attempt.parse_complete_monotonic_ns,
        published_at_utc=attempt.published_at_utc,
        entry_id=attempt.entry_id or f"{attempt.source_name}-release",
        release_url=attempt.release_url or attempt.source_url,
        reference_year=attempt.reference_year,
        reference_month=attempt.reference_month,
        period_name=month_name[attempt.reference_month],
        change_jobs=attempt.parsed_value,
        provenance_text=attempt.provenance_text or "",
    )


def _iso_utc(wall_ns: int) -> str:
    return datetime.fromtimestamp(wall_ns / 1e9, UTC).isoformat().replace("+00:00", "Z")


def _text(entry: ET.Element, name: str) -> str:
    node = entry.find(f"atom:{name}", _NS)
    return "" if node is None or node.text is None else " ".join(node.text.split())


def _validate_expected(
    *,
    year: int,
    month: int,
    release_date: date,
    expected_year: int | None,
    expected_month: int | None,
    expected_release_date: str | None,
) -> None:
    if expected_year is not None and year != expected_year:
        raise BLSStaleReleaseError(f"wrong BLS reference year: {year}")
    if expected_month is not None and month != expected_month:
        raise BLSStaleReleaseError(f"wrong BLS reference month: {month}")
    if (
        expected_release_date is not None
        and release_date.isoformat() != expected_release_date
    ):
        raise BLSStaleReleaseError(
            f"wrong BLS publication date: {release_date.isoformat()}"
        )


def _parse_rss_change(content: str) -> int:
    signed = re.findall(
        r"nonfarm payroll employment[^.]{0,120}?\(([+-][\d,]+)\)",
        content,
        flags=re.IGNORECASE,
    )
    if len(signed) == 1:
        return int(signed[0].replace(",", ""))
    directional = re.findall(
        r"nonfarm payroll employment\s+(increased|rose|rises|edges up|edged up|decreased|declined|fell|edges down|edged down)\s+(?:in\s+\w+\s+)?(?:by\s+)?([\d,]+)",
        content,
        flags=re.IGNORECASE,
    )
    if len(directional) != 1:
        raise BLSReleaseError(
            "RSS entry does not contain one deterministic payroll change"
        )
    direction, raw_value = directional[0]
    value = int(raw_value.replace(",", ""))
    negative = direction.lower() in {
        "decreased",
        "declined",
        "fell",
        "edges down",
        "edged down",
    }
    return -value if negative else value


def parse_rss_payload(
    payload: bytes,
    *,
    expected_year: int | None = None,
    expected_month: int | None = None,
    expected_release_date: str | None = None,
) -> ParsedRelease:
    if not payload or len(payload) > 2_000_000:
        raise BLSReleaseError("RSS payload is empty or unreasonably large")
    try:
        root = ET.fromstring(payload)
    except ET.ParseError as exc:
        raise BLSReleaseError("malformed BLS Atom payload") from exc
    if (
        root.tag != "{http://www.w3.org/2005/Atom}feed"
        or _text(root, "id") != "bls.gov:feed:empsit"
    ):
        raise BLSReleaseError("payload is not the official Employment Situation feed")
    candidates: list[tuple[ET.Element, datetime, int, int, str]] = []
    for entry in root.findall("atom:entry", _NS):
        published_text = _text(entry, "published")
        content = _text(entry, "content")
        try:
            published = datetime.fromisoformat(published_text)
        except ValueError:
            continue
        period_matches = re.findall(r"\b(" + "|".join(_MONTHS) + r")\b", content)
        if len(set(period_matches)) != 1:
            continue
        period_name = period_matches[0]
        month = _MONTHS[period_name]
        year = published.year if month <= published.month else published.year - 1
        if expected_year is not None and year != expected_year:
            continue
        if expected_month is not None and month != expected_month:
            continue
        if (
            expected_release_date is not None
            and published.date().isoformat() != expected_release_date
        ):
            continue
        candidates.append((entry, published, year, month, period_name))
    if (
        expected_year is None
        and expected_month is None
        and expected_release_date is None
        and candidates
    ):
        newest = max(item[1] for item in candidates)
        candidates = [item for item in candidates if item[1] == newest]
    if len(candidates) != 1:
        if (
            expected_year is not None
            or expected_month is not None
            or expected_release_date is not None
        ):
            raise BLSStaleReleaseError(
                f"expected exactly one matching RSS release; found {len(candidates)}"
            )
        raise BLSReleaseError(
            f"expected exactly one latest RSS release; found {len(candidates)}"
        )
    entry, published, year, month, period_name = candidates[0]
    content = _text(entry, "content")
    change_jobs = _parse_rss_change(content)
    link = entry.find("atom:link", _NS)
    release_url = "" if link is None else str(link.attrib.get("href") or "")
    parsed_url = urllib.parse.urlparse(release_url)
    if (
        parsed_url.scheme != "https"
        or parsed_url.hostname != "www.bls.gov"
        or "/news.release/archives/empsit_" not in parsed_url.path
    ):
        raise BLSReleaseError("RSS entry does not link to an official archived release")
    entry_id = _text(entry, "id")
    if not entry_id.startswith("empsit-"):
        raise BLSReleaseError("RSS entry ID is malformed")
    return ParsedRelease(
        published.astimezone(UTC).isoformat().replace("+00:00", "Z"),
        entry_id,
        release_url,
        year,
        month,
        period_name,
        change_jobs,
        content,
    )


class _HTMLDocument(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.text_parts: list[str] = []
        self.rows: list[list[str]] = []
        self._ignored = 0
        self._row: list[str] | None = None
        self._cell: list[str] | None = None

    def handle_starttag(self, tag: str, _attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"script", "style"}:
            self._ignored += 1
        elif not self._ignored and tag == "tr":
            self._row = []
        elif not self._ignored and tag in {"td", "th"} and self._row is not None:
            self._cell = []

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style"} and self._ignored:
            self._ignored -= 1
        elif (
            not self._ignored
            and tag in {"td", "th"}
            and self._cell is not None
            and self._row is not None
        ):
            self._row.append(" ".join(" ".join(self._cell).split()))
            self._cell = None
        elif not self._ignored and tag == "tr" and self._row is not None:
            if self._row:
                self.rows.append(self._row)
            self._row = None

    def handle_data(self, data: str) -> None:
        if self._ignored:
            return
        value = " ".join(data.split())
        if value:
            self.text_parts.append(value)
            if self._cell is not None:
                self._cell.append(value)

    @property
    def text(self) -> str:
        return " ".join(self.text_parts)


def _html_document(payload: bytes) -> _HTMLDocument:
    if not payload or len(payload) > _MAX_PAYLOAD:
        raise BLSReleaseError("HTML payload is empty or unreasonably large")
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise BLSReleaseError("BLS HTML payload is not UTF-8") from exc
    parser = _HTMLDocument()
    parser.feed(text)
    parser.close()
    return parser


def _publication_datetime(release_date: date) -> str:
    released = datetime.combine(release_date, datetime_time(8, 30), tzinfo=EASTERN)
    return released.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _summary_sentence_change(sentence: str, period_name: str) -> int:
    signed = re.findall(
        rf"\bin\s+{period_name}\s*\(([+-][\d,]+)\)",
        sentence,
        flags=re.IGNORECASE,
    )
    if len(signed) == 1:
        return int(signed[0].replace(",", ""))
    directional = re.findall(
        rf"Total nonfarm payroll employment\s+(increased|rose|rises|edges up|edged up|decreased|declined|fell|edges down|edged down)\s+(?:by\s+)?([\d,]+)\s+in\s+{period_name}",
        sentence,
        flags=re.IGNORECASE,
    )
    if len(directional) != 1:
        raise BLSReleaseError(
            "summary does not contain one accepted employment-change expression"
        )
    direction, raw_value = directional[0]
    value = int(raw_value.replace(",", ""))
    negative = direction.lower() in {
        "decreased",
        "declined",
        "fell",
        "edges down",
        "edged down",
    }
    return -value if negative else value


def parse_summary_payload(
    payload: bytes,
    *,
    expected_year: int | None = None,
    expected_month: int | None = None,
    expected_release_date: str | None = None,
) -> ParsedRelease:
    document = _html_document(payload)
    text = document.text
    if (
        "Employment Situation Summary" not in text
        or "Establishment Survey Data" not in text
    ):
        raise BLSReleaseError("page is not the Employment Situation Summary")
    headings = re.findall(
        r"THE EMPLOYMENT SITUATION\s*[-–—]\s*([A-Z][a-z]+)\s+(\d{4})",
        text,
        flags=re.IGNORECASE,
    )
    if len(headings) != 1 or headings[0][0].title() not in _MONTHS:
        raise BLSReleaseError(
            "summary has no unique Employment Situation period heading"
        )
    period_name, raw_year = headings[0][0].title(), headings[0][1]
    year, month = int(raw_year), _MONTHS[period_name]
    release_matches = re.findall(
        r"8:30\s+a\.m\.\s*\(ET\)\s+\w+,\s+([A-Z][a-z]+)\s+(\d{1,2}),\s+(\d{4})",
        text,
        flags=re.IGNORECASE,
    )
    if len(release_matches) != 1:
        raise BLSReleaseError("summary has no unique 8:30 ET publication date")
    release_month, release_day, release_year = release_matches[0]
    try:
        release_date = date(
            int(release_year), _MONTHS[release_month.title()], int(release_day)
        )
    except (KeyError, ValueError) as exc:
        raise BLSReleaseError("summary publication date is malformed") from exc
    _validate_expected(
        year=year,
        month=month,
        release_date=release_date,
        expected_year=expected_year,
        expected_month=expected_month,
        expected_release_date=expected_release_date,
    )
    establishment = text.split("Establishment Survey Data", 1)[1]
    sentences = re.findall(
        r"Total nonfarm payroll employment[^.]{0,280}\.",
        establishment,
        flags=re.IGNORECASE,
    )
    candidates = [
        sentence
        for sentence in sentences
        if re.search(rf"\bin\s+{period_name}\b", sentence, flags=re.IGNORECASE)
    ]
    if len(candidates) != 1:
        raise BLSReleaseError(
            f"summary has {len(candidates)} target-period total-nonfarm statements"
        )
    change_jobs = _summary_sentence_change(candidates[0], period_name)
    return ParsedRelease(
        _publication_datetime(release_date),
        f"summary-{release_date.isoformat()}",
        SOURCE_URLS[SOURCE_SUMMARY],
        year,
        month,
        period_name,
        change_jobs,
        candidates[0],
    )


def _number(value: str) -> float:
    cleaned = value.replace(",", "").replace("−", "-").strip()
    if not re.fullmatch(r"[+-]?\d+(?:\.\d+)?", cleaned):
        raise BLSReleaseError(
            f"Table B-1 contains a nonnumeric Total nonfarm cell: {value!r}"
        )
    return float(cleaned)


def parse_table_b1_payload(
    payload: bytes,
    *,
    expected_year: int | None = None,
    expected_month: int | None = None,
    expected_release_date: str | None = None,
) -> ParsedRelease:
    document = _html_document(payload)
    text = document.text
    required = (
        "Table B-1. Employees on nonfarm payrolls by industry sector and selected industry detail",
        "ESTABLISHMENT DATA",
        "In thousands",
        "Not seasonally adjusted",
        "Seasonally adjusted",
        "Change from:",
    )
    if any(item.lower() not in text.lower() for item in required):
        raise BLSReleaseError("page does not expose the required Table B-1 semantics")
    change_periods = re.findall(
        r"Change from:\s*([A-Z][a-z]+)\s*(\d{4})\s*-\s*([A-Z][a-z]+)\s*(\d{4})",
        text,
        flags=re.IGNORECASE,
    )
    if len(change_periods) != 1:
        raise BLSReleaseError("Table B-1 has no unique month-over-month change header")
    prior_name, prior_year_raw, period_name, year_raw = change_periods[0]
    prior_name, period_name = prior_name.title(), period_name.title()
    if prior_name not in _MONTHS or period_name not in _MONTHS:
        raise BLSReleaseError("Table B-1 change header contains an unknown month")
    year, month = int(year_raw), _MONTHS[period_name]
    prior_year, prior_month = int(prior_year_raw), _MONTHS[prior_name]
    expected_prior = date(year - 1, 12, 1) if month == 1 else date(year, month - 1, 1)
    if (prior_year, prior_month) != (expected_prior.year, expected_prior.month):
        raise BLSReleaseError("Table B-1 change header is not month-over-month")
    modified = re.findall(
        r"Last Modified Date:\s*([A-Z][a-z]+)\s+(\d{1,2}),\s+(\d{4})",
        text,
        flags=re.IGNORECASE,
    )
    if len(modified) != 1:
        raise BLSReleaseError("Table B-1 has no unique publication date")
    release_month, release_day, release_year = modified[0]
    try:
        release_date = date(
            int(release_year), _MONTHS[release_month.title()], int(release_day)
        )
    except (KeyError, ValueError) as exc:
        raise BLSReleaseError("Table B-1 publication date is malformed") from exc
    _validate_expected(
        year=year,
        month=month,
        release_date=release_date,
        expected_year=expected_year,
        expected_month=expected_month,
        expected_release_date=expected_release_date,
    )
    rows = [
        row
        for row in document.rows
        if row and " ".join(row[0].split()).lower() == "total nonfarm"
    ]
    if len(rows) != 1:
        raise BLSReleaseError(
            f"Table B-1 contains {len(rows)} exact Total nonfarm rows"
        )
    row = rows[0]
    if len(row) != 10:
        raise BLSReleaseError(
            f"Table B-1 Total nonfarm row has {len(row) - 1} values; expected 9"
        )
    values = [_number(value) for value in row[1:]]
    sa_prior_level, sa_target_level, reported_change = values[6], values[7], values[8]
    if not math.isclose(
        sa_target_level - sa_prior_level, reported_change, abs_tol=0.0001
    ):
        raise BLSReleaseError(
            "Table B-1 final change is inconsistent with seasonally adjusted levels"
        )
    change_jobs_float = reported_change * 1_000.0
    if not math.isclose(change_jobs_float, round(change_jobs_float), abs_tol=1e-9):
        raise BLSReleaseError(
            "Table B-1 total nonfarm change is not an integral job count"
        )
    return ParsedRelease(
        _publication_datetime(release_date),
        f"table-b1-{release_date.isoformat()}",
        SOURCE_URLS[SOURCE_TABLE_B1],
        year,
        month,
        period_name,
        round(change_jobs_float),
        " | ".join(row),
    )


def parse_source_payload(
    source_name: str,
    payload: bytes,
    *,
    expected_year: int | None = None,
    expected_month: int | None = None,
    expected_release_date: str | None = None,
) -> ParsedRelease:
    arguments = {
        "expected_year": expected_year,
        "expected_month": expected_month,
        "expected_release_date": expected_release_date,
    }
    if source_name == SOURCE_RSS:
        return parse_rss_payload(payload, **arguments)
    if source_name == SOURCE_SUMMARY:
        return parse_summary_payload(payload, **arguments)
    if source_name == SOURCE_TABLE_B1:
        return parse_table_b1_payload(payload, **arguments)
    raise ValueError(f"unknown bounded BLS source: {source_name}")


def _header(headers: Message | None, name: str) -> str | None:
    if headers is None:
        return None
    value = headers.get(name)
    return value.strip() if value else None


class BLSHttpClient:
    """One bounded official endpoint with preflight validators and a shared opener."""

    def __init__(
        self,
        source_name: str,
        *,
        contact: str | None,
        timeout_seconds: float = 5.0,
    ) -> None:
        if source_name not in SOURCE_URLS:
            raise ValueError(f"unknown bounded BLS source: {source_name}")
        self.source_name = source_name
        self.url = SOURCE_URLS[source_name]
        self.timeout_seconds = timeout_seconds
        clean_contact = (contact or "").strip().replace("\r", "").replace("\n", "")
        contact_token = clean_contact if clean_contact else "contact-not-configured"
        self.user_agent = (
            "Mozilla/5.0 (compatible; ParallaxFlash/1.1; "
            f"source={source_name}; contact={contact_token})"
        )
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        self.etag: str | None = None
        self.last_modified: str | None = None

    def fetch(
        self,
        *,
        phase: str,
        request_number: int,
        expected_year: int | None,
        expected_month: int | None,
        expected_release_date: str | None,
        revalidate: bool,
    ) -> SourceAttempt:
        started_wall, started_mono = time.time_ns(), time.monotonic_ns()
        headers = {
            "Accept": (
                "application/atom+xml, application/rss+xml"
                if self.source_name == SOURCE_RSS
                else "text/html"
            ),
            "User-Agent": self.user_agent,
            "Cache-Control": "no-cache, max-age=0",
            "Pragma": "no-cache",
        }
        if revalidate and self.etag:
            headers["If-None-Match"] = self.etag
        if revalidate and self.last_modified:
            headers["If-Modified-Since"] = self.last_modified
        request = urllib.request.Request(self.url, method="GET", headers=headers)
        first_wall = first_mono = None
        status: int | None = None
        response_headers: Message | None = None
        payload = b""
        validation: str | None = None
        rejection: str | None = None
        try:
            with self.opener.open(request, timeout=self.timeout_seconds) as response:
                status = int(response.status)
                response_headers = response.headers
                first = response.read(1)
                if first:
                    first_wall, first_mono = time.time_ns(), time.monotonic_ns()
                    payload = first + response.read(_MAX_PAYLOAD)
                    if len(payload) > _MAX_PAYLOAD:
                        validation = "INVALID"
                        rejection = "response exceeds bounded payload size"
        except urllib.error.HTTPError as exc:
            status = int(exc.code)
            response_headers = exc.headers
            first = exc.read(1)
            if first:
                first_wall, first_mono = time.time_ns(), time.monotonic_ns()
                payload = first + exc.read(_MAX_PAYLOAD)
            validation = "NOT_MODIFIED" if status == 304 else "HTTP_ERROR"
            rejection = f"HTTP {status}"
        except TimeoutError as exc:
            validation, rejection = "TIMEOUT", f"{type(exc).__name__}: {exc}"
        except urllib.error.URLError as exc:
            is_timeout = isinstance(exc.reason, TimeoutError)
            validation = "TIMEOUT" if is_timeout else "HTTP_ERROR"
            rejection = f"URLError: {exc.reason}"
        body_wall, body_mono = time.time_ns(), time.monotonic_ns()
        if status == 200:
            self.etag = _header(response_headers, "ETag") or self.etag
            self.last_modified = (
                _header(response_headers, "Last-Modified") or self.last_modified
            )
        parsed: ParsedRelease | None = None
        if validation is None:
            if status != 200:
                validation, rejection = (
                    "HTTP_ERROR",
                    f"unexpected HTTP status {status}",
                )
            elif not payload:
                validation, rejection = "INVALID", "HTTP 200 response body is empty"
            else:
                try:
                    parsed = parse_source_payload(
                        self.source_name,
                        payload,
                        expected_year=expected_year,
                        expected_month=expected_month,
                        expected_release_date=expected_release_date,
                    )
                    validation = "VALID"
                except BLSStaleReleaseError as exc:
                    validation, rejection = "STALE", str(exc)
                except BLSReleaseError as exc:
                    validation, rejection = "INVALID", str(exc)
        parse_wall, parse_mono = time.time_ns(), time.monotonic_ns()
        assert validation is not None
        return SourceAttempt(
            source_name=self.source_name,
            source_url=self.url,
            phase=phase,
            request_number=request_number,
            request_started_wall_ns=started_wall,
            request_started_monotonic_ns=started_mono,
            first_byte_wall_ns=first_wall,
            first_byte_monotonic_ns=first_mono,
            body_complete_wall_ns=body_wall,
            body_complete_monotonic_ns=body_mono,
            parse_complete_wall_ns=parse_wall,
            parse_complete_monotonic_ns=parse_mono,
            http_status=status,
            http_date=_header(response_headers, "Date"),
            age=_header(response_headers, "Age"),
            etag=_header(response_headers, "ETag"),
            last_modified=_header(response_headers, "Last-Modified"),
            cache_control=_header(response_headers, "Cache-Control"),
            payload=payload,
            payload_sha256=hashlib.sha256(payload).hexdigest(),
            parsed_value=parsed.change_jobs if parsed else None,
            reference_year=parsed.reference_year if parsed else None,
            reference_month=parsed.reference_month if parsed else None,
            published_at_utc=parsed.published_at_utc if parsed else None,
            entry_id=parsed.entry_id if parsed else None,
            release_url=parsed.release_url if parsed else None,
            provenance_text=parsed.provenance_text if parsed else None,
            validation_result=validation,
            rejection_reason=rejection,
        )


class SourceArbiter:
    """First valid official value wins; later valid values confirm or conflict."""

    def __init__(self) -> None:
        self.winner: SourceAttempt | None = None
        self.confirmations: list[SourceAttempt] = []
        self.conflicts: list[SourceAttempt] = []
        self.valid_by_source: dict[str, SourceAttempt] = {}

    @property
    def official_source_conflict(self) -> bool:
        return bool(self.conflicts)

    def submit(self, attempt: SourceAttempt) -> str:
        if not attempt.valid:
            return "REJECTED"
        if attempt.source_name in self.valid_by_source:
            return "DUPLICATE_VALID"
        self.valid_by_source[attempt.source_name] = attempt
        if self.winner is None:
            self.winner = attempt
            return "WINNER"
        if attempt.parsed_value == self.winner.parsed_value:
            self.confirmations.append(attempt)
            return "CONFIRMED"
        self.conflicts.append(attempt)
        return "OFFICIAL_SOURCE_CONFLICT"

    def valid_delta_ms(self, source_name: str) -> float | None:
        if self.winner is None or source_name not in self.valid_by_source:
            return None
        return (
            self.valid_by_source[source_name].parse_complete_monotonic_ns
            - self.winner.parse_complete_monotonic_ns
        ) / 1e6


def parse_feed(
    payload: bytes,
    *,
    expected_year: int | None = None,
    expected_month: int | None = None,
    expected_release_date: str | None = None,
    receipt_wall_time_ns: int | None = None,
    receipt_monotonic_ns: int | None = None,
    source_url: str = BLS_FEED_URL,
) -> ReleaseEvidence:
    """Milestone-1-compatible RSS API backed by the narrow RSS parser."""
    parsed = parse_rss_payload(
        payload,
        expected_year=expected_year,
        expected_month=expected_month,
        expected_release_date=expected_release_date,
    )
    wall = time.time_ns() if receipt_wall_time_ns is None else receipt_wall_time_ns
    mono = time.monotonic_ns() if receipt_monotonic_ns is None else receipt_monotonic_ns
    return ReleaseEvidence(
        source_name=SOURCE_RSS,
        source_url=source_url,
        payload=payload,
        payload_sha256=hashlib.sha256(payload).hexdigest(),
        receipt_wall_time_ns=wall,
        receipt_monotonic_ns=mono,
        receipt_iso_utc=_iso_utc(wall),
        valid_wall_time_ns=wall,
        valid_monotonic_ns=mono,
        published_at_utc=parsed.published_at_utc,
        entry_id=parsed.entry_id,
        release_url=parsed.release_url,
        reference_year=parsed.reference_year,
        reference_month=parsed.reference_month,
        period_name=parsed.period_name,
        change_jobs=parsed.change_jobs,
        provenance_text=parsed.provenance_text,
    )


class BLSFeedClient:
    """Milestone-1 compatibility wrapper for one-shot RSS reads."""

    def __init__(self, *, timeout_seconds: float = 5.0) -> None:
        self.client = BLSHttpClient(
            SOURCE_RSS, contact=None, timeout_seconds=timeout_seconds
        )

    def fetch_release(self, **expected: object) -> ReleaseEvidence:
        attempt = self.client.fetch(
            phase="COMPATIBILITY",
            request_number=1,
            expected_year=expected.get("expected_year"),
            expected_month=expected.get("expected_month"),
            expected_release_date=expected.get("expected_release_date"),
            revalidate=False,
        )
        return evidence_from_attempt(attempt)
