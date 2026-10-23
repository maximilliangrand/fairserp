"""Knowledge graph from Wikidata (CC0) with a Wikipedia summary (CC BY-SA), keyless.

Entity matching is fuzzy: ``wbsearchentities`` ranks label and alias matches, and the top
candidate is accepted when every significant query term appears in its label, alias or
matched text. That lets "einstein" resolve to Albert Einstein (Q937) while long or vague
queries get no entity rather than a wrong one.
"""

from typing import Any
from urllib.parse import quote

from fairserp.engines.base import Engine, EngineContext, Role, require
from fairserp.engines.wikipedia import wiki_lang
from fairserp.model import EngineOutput, Entity, SearchQuery
from fairserp.results import query_terms, truncate
from fairserp.rights import CommercialUse, Provenance, Rights

API = "https://www.wikidata.org/w/api.php"
MAX_QUERY_TERMS = 4
# Items that describe a name or a list rather than the thing a searcher means.
NAME_CLASSES = ("family name", "given name", "surname", "disambiguation", "wikimedia")
PROPERTIES = {
    "P31": "instance_of", "P106": "occupation", "P569": "born", "P19": "place_of_birth",
    "P570": "died", "P20": "place_of_death", "P27": "citizenship", "P571": "founded",
    "P112": "founded_by", "P169": "ceo", "P159": "headquarters", "P17": "country",
    "P36": "capital", "P1082": "population", "P856": "official_website", "P18": "image",
}


def pick_candidate(search: dict[str, Any], q: str) -> str | None:
    terms = query_terms(q)
    if not terms or len(terms) > MAX_QUERY_TERMS:
        return None
    for candidate in search.get("search", [])[:5]:
        description = (candidate.get("description") or "").lower()
        if any(marker in description for marker in NAME_CLASSES):
            continue
        text = " ".join([candidate.get("label", ""), (candidate.get("match") or {}).get("text", ""),
                         *candidate.get("aliases", [])]).lower()
        words = set(text.replace("-", " ").split())
        if all(t in words for t in terms):
            return candidate["id"]
    return None


class WikidataEngine(Engine):
    name = "wikidata"
    role = Role.ENRICHMENT
    upstream = API
    supports = frozenset({"language"})
    fixture_verified = True
    rights = Rights(
        provenance=Provenance.OPEN_DATA,
        commercial_use=CommercialUse.ALLOWED,
        max_storage_seconds=None,
        attribution="Facts from Wikidata (CC0); description from Wikipedia (CC BY-SA 4.0)",
        terms_url="https://foundation.wikimedia.org/wiki/Policy:Terms_of_Use",
        terms_reviewed="2026-10-23",
    )
    notes = "No key. Knowledge graph with Wikidata IDs."

    async def fetch(self, query: SearchQuery, ctx: EngineContext) -> Any:
        lang = wiki_lang(query, ctx.settings.wikipedia_lang)
        search = await self.get_json(ctx, "GET", API, params={
            "action": "wbsearchentities", "search": query.q, "language": lang,
            "uselang": lang, "type": "item", "limit": 5, "format": "json"})
        chosen = pick_candidate(search, query.q)
        raw: dict[str, Any] = {"lang": lang, "search": search, "chosen": chosen}
        if chosen is None:
            return raw
        entities = await self.get_json(ctx, "GET", API, params={
            "action": "wbgetentities", "ids": chosen, "format": "json", "languages": lang,
            "props": "labels|descriptions|claims|sitelinks", "sitefilter": f"{lang}wiki"})
        entity = entities["entities"][chosen]
        refs = _referenced_items(entity)
        if refs:
            labels = await self.get_json(ctx, "GET", API, params={
                "action": "wbgetentities", "ids": "|".join(refs[:50]), "format": "json",
                "languages": lang, "props": "labels"})
            raw["labels"] = {k: v.get("labels", {}).get(lang, {}).get("value")
                             for k, v in labels.get("entities", {}).items()}
        raw["entity"] = entity
        title = (entity.get("sitelinks") or {}).get(f"{lang}wiki", {}).get("title")
        if title:
            summary_url = (f"https://{lang}.wikipedia.org/api/rest_v1/page/summary/"
                           f"{quote(title.replace(' ', '_'), safe='')}")
            raw["summary"] = await self.get_json(ctx, "GET", summary_url)
        return raw

    def parse(self, raw: dict[str, Any], query: SearchQuery) -> EngineOutput:
        require("search" in raw, "missing search response")
        if not raw.get("chosen") or "entity" not in raw:
            return EngineOutput()
        lang, entity = raw["lang"], raw["entity"]
        labels = raw.get("labels") or {}
        summary = raw.get("summary") or {}
        attributes: dict[str, str] = {}
        image = None
        for pid, name in PROPERTIES.items():
            values = [_value(c, labels) for c in entity.get("claims", {}).get(pid, [])[:3]]
            values = [v for v in values if v]
            if not values:
                continue
            if pid == "P18":
                image = values[0]
            else:
                attributes[name] = ", ".join(values)
        description = entity.get("descriptions", {}).get(lang, {}).get("value", "")
        page = summary.get("content_urls", {}).get("desktop", {}).get("page", "")
        return EngineOutput(entity=Entity(
            title=entity.get("labels", {}).get(lang, {}).get("value", raw["chosen"]),
            type=description.split(" (")[0],
            description=truncate(summary.get("extract", "") or description, 600),
            url=page or f"https://www.wikidata.org/wiki/{raw['chosen']}",
            image=(summary.get("thumbnail") or {}).get("source") or image,
            website=attributes.pop("official_website", None),
            wikidata_id=raw["chosen"],
            attributes=attributes,
        ))


def _referenced_items(entity: dict[str, Any]) -> list[str]:
    refs = []
    for pid in PROPERTIES:
        for claim in entity.get("claims", {}).get(pid, [])[:3]:
            value = claim.get("mainsnak", {}).get("datavalue", {}).get("value")
            if isinstance(value, dict) and value.get("entity-type") == "item":
                refs.append(value["id"])
    return list(dict.fromkeys(refs))


def _value(claim: dict[str, Any], labels: dict[str, str | None]) -> str | None:
    snak = claim.get("mainsnak", {})
    data = snak.get("datavalue", {})
    value, kind = data.get("value"), data.get("type")
    if kind == "wikibase-entityid":
        return labels.get(value.get("id"))
    if kind == "time":
        stamp = value["time"].lstrip("+")
        return stamp[:4] if value.get("precision", 11) <= 9 else stamp[:10]
    if kind == "quantity":
        return value["amount"].lstrip("+")
    if snak.get("datatype") == "commonsMedia":
        return f"https://commons.wikimedia.org/wiki/Special:FilePath/{quote(value)}"
    if isinstance(value, str):
        return value
    return None
