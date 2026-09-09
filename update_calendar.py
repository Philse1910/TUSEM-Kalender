#!/usr/bin/env python3
"""Erzeugt F2.ics aus dem Spielplan von FUSSBALL.DE."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, Optional

import requests
from bs4 import BeautifulSoup, Tag


TEAM_ID = "011MIE41AK000000VTVG0001VTR8C1K7"
TEAM_NAME = "TUSEM Essen II"
CALENDAR_NAME = "TUSEM Essen F2 – Spielplan"
TEAM_URL = (
    "https://www.fussball.de/mannschaft/"
    "tusem-essen-ii-tusem-essen-1926-niederrhein/-/saison/2627/team-id/"
    + TEAM_ID
)
MATCHPLAN_URL = (
    "https://www.fussball.de/ajax.team.matchplan/-/mode/PAGE/"
    "show-venues/true/team-id/" + TEAM_ID
)
OUTPUT_FILE = Path(__file__).with_name("F2.ics")
HOME_FALLBACK = "Fibelweg 7, 45149 Essen"


@dataclass(frozen=True)
class Match:
    date: datetime
    time_text: str
    competition: str
    home: str
    away: str
    location: Optional[str] = None

    @property
    def is_time_open(self) -> bool:
        return self.time_text == "00:01"

    @property
    def is_home_match(self) -> bool:
        return normalize_team(self.home).casefold() == TEAM_NAME.casefold()


def clean_text(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def normalize_team(value: str) -> str:
    value = clean_text(value)
    return re.sub(r"\s+-\s+Kinderfestival\s*$", "", value, flags=re.I)


def clean_location(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    value = clean_text(value)
    if "Fibelweg 7" in value:
        return HOME_FALLBACK
    return value or None


def parse_meta(text: str) -> tuple[datetime, str, str]:
    text = clean_text(text)
    date_match = re.search(r"(\d{2}\.\d{2}\.\d{2,4})", text)
    time_match = re.search(r"(?:-|\|)\s*(\d{2}:\d{2})(?:\s*Uhr)?", text)
    if not date_match or not time_match:
        raise ValueError(f"Datum oder Uhrzeit nicht erkannt: {text}")

    date_text = date_match.group(1)
    date_format = "%d.%m.%Y" if len(date_text) == 10 else "%d.%m.%y"
    match_date = datetime.strptime(date_text, date_format)

    parts = [clean_text(part) for part in text.split("|") if clean_text(part)]
    competition = parts[-1] if len(parts) >= 2 else "Kinderfußball"
    competition = re.sub(r"^\d{2}:\d{2}(?:\s*Uhr)?\s*", "", competition).strip(" -")
    if competition in {"", time_match.group(1)}:
        competition = "Kinderfußball"
    return match_date, time_match.group(1), competition


def parse_card_matches(html: str) -> list[Match]:
    """Liest Ansichten mit match-meta, team-home und team-away."""
    soup = BeautifulSoup(html, "html.parser")
    matches: list[Match] = []

    for meta in soup.select(".match-meta"):
        container: Optional[Tag] = meta
        for _ in range(6):
            if container and container.select_one(".team-home") and container.select_one(".team-away"):
                break
            container = container.parent if isinstance(container, Tag) else None
        if not container:
            continue

        home_node = container.select_one(".team-home")
        away_node = container.select_one(".team-away")
        if not home_node or not away_node:
            continue
        try:
            date, time_text, competition = parse_meta(meta.get_text(" ", strip=True))
        except ValueError:
            continue

        location_node = container.select_one(
            ".match-venue, .venue, .location, .row-venue"
        )
        matches.append(
            Match(
                date=date,
                time_text=time_text,
                competition=competition,
                home=clean_text(home_node.get_text(" ", strip=True)),
                away=clean_text(away_node.get_text(" ", strip=True)),
                location=clean_location(
                    location_node.get_text(" ", strip=True) if location_node else None
                ),
            )
        )
    return deduplicate(matches)


def parse_table_matches(html: str) -> list[Match]:
    """Liest die aktuelle FUSSBALL.DE-Spielplantabelle."""
    soup = BeautifulSoup(html, "html.parser")
    matches: list[Match] = []

    for headline in soup.select("tr.row-headline"):
        try:
            date, time_text, competition = parse_meta(headline.get_text(" ", strip=True))
        except ValueError:
            continue

        teams: list[str] = []
        location: Optional[str] = None
        sibling = headline.find_next_sibling("tr")
        while sibling and "row-headline" not in (sibling.get("class") or []):
            names = sibling.select(".club-name")
            if len(names) >= 2 and not teams:
                teams = [clean_text(node.get_text(" ", strip=True)) for node in names[:2]]
            if "row-venue" in (sibling.get("class") or []):
                location = clean_location(sibling.get_text(" ", strip=True))
            sibling = sibling.find_next_sibling("tr")

        if len(teams) == 2:
            matches.append(
                Match(date, time_text, competition, teams[0], teams[1], location)
            )
    return deduplicate(matches)


def deduplicate(matches: Iterable[Match]) -> list[Match]:
    unique: dict[tuple[str, str, str], Match] = {}
    for match in matches:
        key = (
            match.date.strftime("%Y%m%d"),
            normalize_team(match.home).casefold(),
            normalize_team(match.away).casefold(),
        )
        unique[key] = match
    return sorted(unique.values(), key=lambda item: (item.date, item.time_text))


def unfold_ical(content: str) -> list[str]:
    lines: list[str] = []
    for raw in content.replace("\r\n", "\n").split("\n"):
        if raw.startswith((" ", "\t")) and lines:
            lines[-1] += raw[1:]
        else:
            lines.append(raw)
    return lines


def unescape_ical(value: str) -> str:
    return (
        value.replace("\\n", "\n")
        .replace("\\N", "\n")
        .replace("\\,", ",")
        .replace("\\;", ";")
        .replace("\\\\", "\\")
    )


def read_existing_events(path: Path) -> dict[str, dict[str, str]]:
    """Bewahrt UID, DTSTAMP und bekannte Orte anhand des Spieltags."""
    if not path.exists():
        return {}
    events: dict[str, dict[str, str]] = {}
    current: Optional[dict[str, str]] = None
    for line in unfold_ical(path.read_text(encoding="utf-8")):
        if line == "BEGIN:VEVENT":
            current = {}
        elif line == "END:VEVENT" and current is not None:
            start = current.get("DTSTART", "")
            date_match = re.search(r"(\d{8})", start)
            if date_match:
                events[date_match.group(1)] = current
            current = None
        elif current is not None and ":" in line:
            name, value = line.split(":", 1)
            current[name.split(";", 1)[0]] = unescape_ical(value)
    return events


def escape_ical(value: str) -> str:
    return (
        value.replace("\\", "\\\\")
        .replace("\n", "\\n")
        .replace(";", "\\;")
        .replace(",", "\\,")
    )


def fold_ical_line(line: str) -> list[str]:
    """Faltet Zeilen gemäß iCalendar bei höchstens 75 UTF-8-Bytes."""
    result: list[str] = []
    current = ""
    limit = 75
    for char in line:
        candidate = current + char
        if len(candidate.encode("utf-8")) > limit and current:
            result.append(current)
            current = " " + char
            limit = 75
        else:
            current = candidate
    result.append(current)
    return result


def make_uid(match: Match) -> str:
    source = "|".join(
        [
            match.date.strftime("%Y-%m-%d"),
            normalize_team(match.home),
            normalize_team(match.away),
        ]
    )
    return hashlib.sha256(source.encode("utf-8")).hexdigest()[:24] + "@tusem-kalender.local"


def make_summary(match: Match) -> str:
    home = normalize_team(match.home)
    away = normalize_team(match.away)
    if match.is_home_match or home.casefold() == away.casefold():
        summary = f"⚽ Kinderfestival bei {TEAM_NAME}"
    else:
        summary = f"⚽ {home} – {away}"
    if match.is_time_open:
        summary += " (Uhrzeit offen)"
    return summary


def render_calendar(matches: list[Match], existing: dict[str, dict[str, str]]) -> str:
    now_stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//TUSEM Essen F2//Automatischer Spielplan//DE",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        f"X-WR-CALNAME:{escape_ical(CALENDAR_NAME)}",
        "X-WR-TIMEZONE:Europe/Berlin",
        "BEGIN:VTIMEZONE",
        "TZID:Europe/Berlin",
        "BEGIN:DAYLIGHT",
        "DTSTART:19700329T020000",
        "TZOFFSETFROM:+0100",
        "TZOFFSETTO:+0200",
        "RRULE:FREQ=YEARLY;BYMONTH=3;BYDAY=-1SU",
        "END:DAYLIGHT",
        "BEGIN:STANDARD",
        "DTSTART:19701025T030000",
        "TZOFFSETFROM:+0200",
        "TZOFFSETTO:+0100",
        "RRULE:FREQ=YEARLY;BYMONTH=10;BYDAY=-1SU",
        "END:STANDARD",
        "END:VTIMEZONE",
    ]

    for match in matches:
        date_key = match.date.strftime("%Y%m%d")
        old = existing.get(date_key, {})
        uid = old.get("UID") or make_uid(match)
        stamp = old.get("DTSTAMP") or now_stamp
        location = match.location
        if not location and match.is_home_match:
            location = HOME_FALLBACK
        if not location:
            location = old.get("LOCATION") or None

        description_parts = [f"Wettbewerb: {match.competition}"]
        if match.is_time_open:
            description_parts.append(
                "Uhrzeit noch offen; 00:01 wird nicht als echte Anstoßzeit übernommen."
            )
        if location == HOME_FALLBACK and match.is_home_match and not match.location:
            description_parts.append("Ort: Heimspiel-Fallback des TUSEM Essen.")
        description_parts.append(f"Quelle: {TEAM_URL}")

        lines.extend(["BEGIN:VEVENT", f"UID:{uid}", f"DTSTAMP:{stamp}"])
        if match.is_time_open:
            next_day = match.date + timedelta(days=1)
            lines.extend(
                [
                    f"DTSTART;VALUE=DATE:{date_key}",
                    f"DTEND;VALUE=DATE:{next_day.strftime('%Y%m%d')}",
                ]
            )
        else:
            start = datetime.strptime(
                f"{date_key} {match.time_text}", "%Y%m%d %H:%M"
            )
            end = start + timedelta(hours=2)
            lines.extend(
                [
                    f"DTSTART;TZID=Europe/Berlin:{start.strftime('%Y%m%dT%H%M%S')}",
                    f"DTEND;TZID=Europe/Berlin:{end.strftime('%Y%m%dT%H%M%S')}",
                ]
            )
        lines.extend(
            [
                f"SUMMARY:{escape_ical(make_summary(match))}",
                f"DESCRIPTION:{escape_ical(chr(10).join(description_parts))}",
                f"URL:{TEAM_URL}",
            ]
        )
        if location:
            lines.append(f"LOCATION:{escape_ical(location)}")
        lines.append("END:VEVENT")

    lines.append("END:VCALENDAR")
    folded = [piece for line in lines for piece in fold_ical_line(line)]
    return "\r\n".join(folded) + "\r\n"


def fetch_html(url: str) -> str:
    response = requests.get(
        url,
        timeout=30,
        headers={
            "User-Agent": "Mozilla/5.0 (compatible; TUSEM-Kalender/1.0)",
            "Accept-Language": "de-DE,de;q=0.9",
        },
    )
    response.raise_for_status()
    return response.text


def main() -> None:
    matchplan_html = fetch_html(MATCHPLAN_URL)
    matches = parse_table_matches(matchplan_html)

    if not matches:
        team_html = fetch_html(TEAM_URL)
        matches = parse_card_matches(team_html)
    if not matches:
        raise RuntimeError("Keine Spiele gefunden; FUSSBALL.DE-Struktur bitte prüfen.")

    existing = read_existing_events(OUTPUT_FILE)
    OUTPUT_FILE.write_text(render_calendar(matches, existing), encoding="utf-8")
    print(f"{len(matches)} Spiele in {OUTPUT_FILE.name} geschrieben.")


if __name__ == "__main__":
    main()
