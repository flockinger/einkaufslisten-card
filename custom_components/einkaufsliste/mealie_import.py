"""🍲 Rezepte aus Mealie holen (REST-API von Mealie, Anmeldung mit einem API-Token als „Bearer“).

Adresse und Token stellt man in der Karte ein (⚙️ → Import & Sicherung → Mealie). Sie liegen nur auf dem Server
in den Einstellungen der Einkaufsliste (manager.mealie), werden nie an die Karte zurückgeschickt und nicht in
Sicherungen exportiert. Gelesen wird nur (GET) – in Mealie wird nichts verändert.

Übernommen werden Name, Zutaten (Menge, Einheit, Notiz), Zubereitung (je Schritt eine Zeile, samt der Zuordnung
„welche Zutaten gehören zu welchem Schritt“), Portionen, Dauer, Link zur Quelle und das Rezeptfoto.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import re
from typing import TYPE_CHECKING, Any
from urllib.parse import quote, urlsplit

import aiohttp

from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .netutil import read_limited
from .quantity import norm_qty
from .recipe_import import _iso_minutes, _nice, parse_line

if TYPE_CHECKING:
    from .manager import EinkaufslisteManager

_LOGGER = logging.getLogger(__name__)

MAX_BYTES = 8 * 1024 * 1024
MAX_IMAGE = 3 * 1024 * 1024
MAX_RECIPES = 3000
PER_PAGE = 100
MAX_IMPORT = 200  # so viele Rezepte auf einmal übernehmen (jedes braucht eine eigene Abfrage)


def clean_base(url: Any) -> str:
    """Aus dem, was jemand tippt, die Grundadresse machen („mealie.lan:9000/api/“ -> „http://mealie.lan:9000“)."""
    raw = str(url or "").strip()
    if not raw:
        raise ValueError("Bitte die Mealie-Adresse eintragen, z. B. http://192.168.1.20:9000")
    if "://" not in raw:
        raw = "http://" + raw
    try:
        parts = urlsplit(raw)
        host = parts.hostname
    except ValueError as err:
        raise ValueError("Die Mealie-Adresse sieht nicht richtig aus.") from err
    if parts.scheme not in ("http", "https") or not host or parts.username or parts.password:
        raise ValueError("Die Mealie-Adresse sieht nicht richtig aus – z. B. http://192.168.1.20:9000")
    path = parts.path.rstrip("/")
    if path.endswith("/api"):
        path = path[:-4]
    return f"{parts.scheme}://{parts.netloc}{path}"


def _session(hass, verify_ssl: bool) -> aiohttp.ClientSession:
    # 🔐 Mealie im Heimnetz hat oft ein eigenes (selbst signiertes) Zertifikat – dafür gibt es den Schalter
    return async_get_clientsession(hass, verify_ssl=bool(verify_ssl))


async def _request(session: aiohttp.ClientSession, base: str, token: str, endpoint: str,
                   params: dict | None = None, binary: bool = False, limit: int = MAX_BYTES) -> Any:
    """Eine GET-Anfrage an die Mealie-API. JSON zurück (bei binary=True die Bytes). Fehler als verständliche ValueError."""
    try:
        async with asyncio.timeout(25):
            resp = await session.get(
                f"{base}/api/{endpoint}", params=params,
                headers={"Authorization": f"Bearer {token}", "accept": "*/*" if binary else "application/json"},
                allow_redirects=False,
            )
            if resp.status in (401, 403):
                raise ValueError("Mealie lehnt das Token ab – bitte prüfen (in Mealie im Profil unter „API-Token“ ein neues erzeugen).")
            if resp.status == 404 and not binary:
                raise ValueError("Unter dieser Adresse habe ich kein Mealie gefunden – stimmen Adresse und Port?")
            if resp.status >= 300:
                if binary:
                    return None
                raise ValueError(f"Mealie antwortet mit Fehler {resp.status}.")
            raw = await read_limited(resp.content, limit)
    except (TimeoutError, aiohttp.ClientError) as err:
        _LOGGER.debug("Mealie nicht erreichbar: %s", err)
        text = str(err).lower()
        if "certificate" in text or "ssl" in text:
            raise ValueError("Das Zertifikat von Mealie wird nicht akzeptiert – dann bitte „Zertifikat nicht prüfen“ ankreuzen.") from err
        raise ValueError("Mealie ist von Home Assistant aus nicht erreichbar – Adresse und Netzwerk prüfen.") from err
    if raw is None:
        if binary:
            return None
        raise ValueError("Die Antwort von Mealie ist ungewöhnlich groß – abgebrochen.")
    if binary:
        return raw
    try:
        return json.loads(raw)
    except ValueError as err:
        raise ValueError("Mealie hat keine lesbare Antwort geschickt – ist das die richtige Adresse?") from err


def _need_token(token: Any) -> str:
    token = str(token or "").strip()
    if token.lower().startswith("bearer "):
        token = token[7:].strip()
    if not token:
        raise ValueError("Bitte das API-Token eintragen (in Mealie im Profil unter „API-Token“ erzeugen).")
    return token


# ---------------------------------------------------------------- Liste
async def async_list(hass, url: Any, token: Any, verify_ssl: bool = True, search: str | None = None) -> dict[str, Any]:
    """Alle Rezepte aus Mealie als Zeilen für die Vorschau: {slug, name, minutes, servings, image, categories}."""
    token = _need_token(token)
    base = clean_base(url)
    session = _session(hass, verify_ssl)
    rows: list[dict[str, Any]] = []
    total = 0
    page = 1
    while page <= MAX_RECIPES // PER_PAGE + 1:
        params = {"page": page, "perPage": PER_PAGE, "orderBy": "name", "orderDirection": "asc"}
        if search:
            params["search"] = search
        data = await _request(session, base, token, "recipes", params=params)
        if not isinstance(data, dict) or not isinstance(data.get("items"), list):
            raise ValueError("Mealie hat die Rezepte in einem unbekannten Format geschickt – ist die Adresse richtig?")
        total = int(data.get("total") or 0)
        for r in data["items"]:
            row = summary_row(r)
            if row:
                rows.append(row)
        if page >= int(data.get("total_pages") or 1) or not data["items"]:
            break
        page += 1
    more = max(0, len(rows) - MAX_RECIPES, total - len(rows) if len(rows) >= MAX_RECIPES else 0)
    return {"rows": rows[:MAX_RECIPES], "more": more, "total": total}


def summary_row(r: Any) -> dict[str, Any] | None:
    if not isinstance(r, dict):
        return None
    slug = str(r.get("slug") or "").strip()
    name = " ".join(str(r.get("name") or "").split())
    if not slug or not name:
        return None
    cats = [str(c.get("name")) for c in (r.get("recipeCategory") or []) if isinstance(c, dict) and c.get("name")]
    return {
        "slug": slug,
        "id": str(r.get("id") or ""),
        "name": name,
        "minutes": minutes_of(r),
        "servings": servings_of(r),
        "image": bool(r.get("image")),
        "categories": cats[:3],
    }


# ---------------------------------------------------------------- Umwandeln
_TIME = re.compile(
    r"(?:(?P<d>\d+(?:[.,]\d+)?)\s*(?:days?|tage?n?|d)\.?(?![a-z]))?\s*"
    r"(?:(?P<h>\d+(?:[.,]\d+)?)\s*(?:hours?|hrs?|stunden?|std\.?|h)\.?(?![a-z]))?\s*"
    r"(?:(?P<m>\d+(?:[.,]\d+)?)\s*(?:minutes?|mins?|minuten?|m)\.?(?![a-z]))?",
    re.IGNORECASE,
)


def _minutes_text(value: Any) -> int | None:
    """Mealie schreibt die Zeit frei: „45 Minutes“, „1 Hour 30 Minutes“, „1 Std. 30 Min.“, „PT1H30M“, „90“."""
    text = str(value or "").strip()
    if not text:
        return None
    iso = _iso_minutes(text)
    if iso:
        return iso
    if re.fullmatch(r"\d+", text):
        n = int(text)
    else:
        m = _TIME.fullmatch(text)
        if not m or not any(m.group("d", "h", "m")):
            return None
        num = lambda s: float((s or "0").replace(",", "."))  # noqa: E731
        n = round(num(m.group("d")) * 1440 + num(m.group("h")) * 60 + num(m.group("m")))
    return n if 1 <= n <= 1440 else None


def minutes_of(r: dict[str, Any]) -> int | None:
    """⏱️ Gesamtzeit, sonst Vorbereitung + Kochen/Arbeitszeit."""
    total = _minutes_text(r.get("totalTime"))
    if total:
        return total
    n = sum(_minutes_text(r.get(k)) or 0 for k in ("prepTime", "cookTime", "performTime"))
    return n if 1 <= n <= 1440 else None


def servings_of(r: dict[str, Any]) -> int | None:
    """👥 Portionen (Mealie: recipeServings, sonst recipeYieldQuantity)."""
    for key in ("recipeServings", "recipeYieldQuantity"):
        try:
            n = round(float(r.get(key) or 0))
        except (TypeError, ValueError):
            continue
        if 1 <= n <= 99:
            return n
    return None


def _number(value: Any) -> str | None:
    try:
        v = round(float(value), 2)
    except (TypeError, ValueError):
        return None
    if v <= 0:
        return None
    return str(int(v)) if v == int(v) else str(v)


def _ingredient(ing: Any) -> dict[str, Any] | None:
    """Eine Mealie-Zutat in {name, quantity, note} umwandeln. Ohne „Zutat“ (nur Freitext) wird der Text zerlegt."""
    if not isinstance(ing, dict):
        return None
    food = ing.get("food") if isinstance(ing.get("food"), dict) else None
    fname = _nice((food or {}).get("name") or "")
    note = _nice(str(ing.get("note") or ""))
    if not fname:  # Freitext-Zeile: „200 g Mehl“ steht dann in note / originalText / display
        text = str(ing.get("originalText") or ing.get("display") or ing.get("note") or "")
        return parse_line(text)
    qty = None
    if not ing.get("disableAmount"):
        num = _number(ing.get("quantity"))
        unit = ing.get("unit") if isinstance(ing.get("unit"), dict) else None
        uname = ""
        if unit:
            uname = str(unit.get("abbreviation") or unit.get("name") or "").strip()
        if num:
            qty = norm_qty(f"{num} {uname}".strip())
    return {"name": fname[:60], "quantity": qty, "note": note[:60] or None}


_MD_LINK = re.compile(r"!?\[([^\]]*)\]\([^)]*\)")
_MD_MARK = re.compile(r"(\*\*|__|~~|`)")
_MD_HEAD = re.compile(r"^\s{0,3}#{1,6}\s*")
_MD_LIST = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+")


def _plain(text: Any) -> str:
    """Mealie-Text (Markdown) für die Kochansicht glätten: Links und Fettdruck weg, alles in eine Zeile."""
    t = str(text or "")
    t = _MD_LINK.sub(r"\1", t)
    t = _MD_MARK.sub("", t)
    parts = [_MD_LIST.sub("", _MD_HEAD.sub("", ln)).strip() for ln in t.splitlines()]
    return " ".join(p for p in parts if p)


def map_recipe(detail: dict[str, Any], base: str) -> dict[str, Any]:
    """Ein Mealie-Rezept (GET /api/recipes/{slug}) in das umwandeln, was add_recipe braucht."""
    items: list[dict[str, Any]] = []
    ref_names: dict[str, str] = {}
    seen: set[tuple[str, str]] = set()
    for ing in detail.get("recipeIngredient") or []:
        entry = _ingredient(ing)
        if not entry:
            continue
        key = (entry["name"].lower(), (entry["note"] or "").lower())
        n = 1
        while key in seen:  # gleiche Zutat zweimal (z. B. Öl fürs Braten und für den Teig): nicht verlieren
            n += 1
            note = f"{entry['note']}, {n}" if entry["note"] else f"Nr. {n}"
            entry["note"] = note[:60]
            key = (entry["name"].lower(), entry["note"].lower())
        seen.add(key)
        items.append(entry)
        if isinstance(ing, dict) and ing.get("referenceId"):
            ref_names[str(ing["referenceId"])] = entry["name"].lower()
    steps: list[str] = []
    step_items: list[list[str] | None] = []
    for st in detail.get("recipeInstructions") or []:
        if not isinstance(st, dict):
            continue
        text = _plain(st.get("text"))
        title = _plain(st.get("title"))
        line = f"{title}: {text}" if title and text else (text or title)
        if not line:
            continue
        steps.append(line)
        names: list[str] = []
        for ref in st.get("ingredientReferences") or []:
            name = ref_names.get(str((ref or {}).get("referenceId")))
            if name and name not in names:
                names.append(name)
        step_items.append(names or None)
    slug = str(detail.get("slug") or "")
    org = str(detail.get("orgURL") or "").strip()
    source = org if re.match(r"^https?://\S+$", org) and len(org) <= 500 else None
    return {
        "name": _nice(str(detail.get("name") or ""))[:60],
        "items": items,
        "steps": "\n".join(steps)[:8000] or None,
        "step_items": step_items if any(step_items) else None,
        "servings": servings_of(detail),
        "minutes": minutes_of(detail),
        "source": source,
        "slug": slug,
        "recipe_id": str(detail.get("id") or ""),
        "has_image": bool(detail.get("image")),
        "base": base,
    }


async def _fetch_image(session, base: str, token: str, recipe_id: str) -> str | None:
    """📷 Rezeptfoto von Mealie holen (kleine Fassung) und als Data-URL zurückgeben, wenn es ein Bild ist."""
    if not recipe_id:
        return None
    for fname in ("min-original.webp", "original.webp"):
        try:
            raw = await _request(session, base, token, f"media/recipes/{quote(recipe_id, safe='')}/images/{fname}",
                                 binary=True, limit=MAX_IMAGE)
        except ValueError:
            continue
        if raw and (raw[:3] == b"\xff\xd8\xff" or raw[:8] == b"\x89PNG\r\n\x1a\n" or raw[8:12] == b"WEBP"):
            mime = "image/png" if raw[:4] == b"\x89PNG" else "image/webp" if raw[8:12] == b"WEBP" else "image/jpeg"
            return f"data:{mime};base64,{base64.b64encode(raw).decode()}"
    return None


# ---------------------------------------------------------------- Übernehmen
_FIELDS = ("name", "items", "steps", "servings", "minutes", "source", "step_items")


def _sig(values: dict[str, Any]) -> str:
    """Fingerabdruck der Inhalte eines Rezepts (ohne Symbol, Gruppe, Plan, Notizen)."""
    data = {k: values.get(k) for k in _FIELDS}
    return hashlib.sha1(json.dumps(data, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()[:16]


def find_linked(recipes: list[dict[str, Any]], recipe_id: str, name: str) -> dict[str, Any] | None:
    """Das Rezept hier zu einem Mealie-Rezept: erst über die gemerkte Mealie-ID, sonst über den Namen."""
    if recipe_id:
        for r in recipes:
            if (r.get("mealie") or {}).get("id") == recipe_id:
                return r
    low = name.lower()
    return next((r for r in recipes if r["name"].lower() == low), None)


async def async_import(hass, manager: EinkaufslisteManager, cfg: dict[str, Any], slugs: list[str],
                       with_photos: bool = True, mode: str = "skip") -> dict[str, Any]:
    """Die gewählten Mealie-Rezepte anlegen.

    mode "skip": Gibt es das Rezept schon, wird es übersprungen.
    mode "update": Vorhandene werden aktualisiert, wenn sie sich in Mealie geändert haben – außer du hast sie hier
        selbst geändert (die bleiben unangetastet).
    mode "overwrite": wie "update", aber auch selbst geänderte werden überschrieben.
    """
    token = _need_token(cfg.get("token"))
    base = clean_base(cfg.get("url"))
    session = _session(hass, cfg.get("verify_ssl", True))
    added: list[str] = []
    updated: list[str] = []
    exists: list[str] = []
    unchanged: list[str] = []
    kept: list[str] = []
    failed: list[str] = []
    photos = 0
    for slug in [str(s) for s in slugs][:MAX_IMPORT]:
        try:
            detail = await _request(session, base, token, f"recipes/{quote(slug, safe='')}")
            if not isinstance(detail, dict):
                raise ValueError("unbekanntes Format")
            rec = map_recipe(detail, base)
            if not rec["name"]:
                raise ValueError("ohne Namen")
            src_sig = _sig(rec)
            have = find_linked(manager.recipes, rec["recipe_id"], rec["name"])
            fresh = False
            if have is None:
                recipe = manager.add_recipe(
                    rec["name"], rec["items"], steps=rec["steps"], servings=rec["servings"], minutes=rec["minutes"],
                    source=rec["source"], step_items=rec["step_items"],
                )
                fresh = True
            elif mode == "skip":
                exists.append(have["name"])
                continue
            else:
                link = have.get("mealie") or {}
                if link.get("src_sig") == src_sig and link.get("id") == rec["recipe_id"]:
                    unchanged.append(have["name"])
                    continue
                edited = link.get("local_sig") != _sig(have)  # ohne gemerkten Stand (älterer Import) gilt es als geändert
                if edited and mode != "overwrite":
                    kept.append(have["name"])
                    continue
                recipe = manager.update_recipe(
                    have["id"], name=rec["name"], items=rec["items"], steps=rec["steps"], servings=rec["servings"],
                    minutes=rec["minutes"], source=rec["source"], step_items=rec["step_items"],
                )
            recipe["mealie"] = {"id": rec["recipe_id"], "slug": rec["slug"], "src_sig": src_sig, "local_sig": _sig(recipe)}
            manager._changed()  # noqa: SLF001
        except ValueError as err:
            _LOGGER.info("Mealie-Rezept %s übersprungen: %s", slug, err)
            failed.append(slug)
            continue
        (added if fresh else updated).append(recipe["name"])
        if with_photos and rec["has_image"]:
            try:
                data = await _fetch_image(session, base, token, rec["recipe_id"])
                if data:
                    from .manager import recipe_photo_key  # noqa: PLC0415

                    await manager.async_set_photo(recipe_photo_key(recipe["id"]), data)
                    photos += 1
            except ValueError as err:
                _LOGGER.debug("Foto zu %s nicht übernommen: %s", slug, err)
    return {"added": len(added), "names": added, "updated": len(updated), "updated_names": updated,
            "exists": len(exists), "exists_names": exists, "unchanged": len(unchanged),
            "kept": len(kept), "kept_names": kept, "failed": len(failed), "photos": photos}


async def async_test(hass, url: Any, token: Any, verify_ssl: bool = True) -> dict[str, Any]:
    """Verbindung prüfen: Gibt es das Mealie, und passt das Token? Antwort: {version, total}."""
    token = _need_token(token)
    base = clean_base(url)
    session = _session(hass, verify_ssl)
    about = await _request(session, base, token, "app/about")
    version = str(about.get("version") or "") if isinstance(about, dict) else ""
    data = await _request(session, base, token, "recipes", params={"page": 1, "perPage": 1})
    if not isinstance(data, dict) or "items" not in data:
        raise ValueError("Das sieht nicht nach Mealie aus – ist die Adresse richtig?")
    return {"version": version or None, "total": int(data.get("total") or 0)}
