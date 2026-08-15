from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from playwright.sync_api import Browser, Page, sync_playwright

try:
    from deep_translator import GoogleTranslator
except ImportError:  # Le bot reste utilisable si la traduction est indisponible.
    GoogleTranslator = None


ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
LOG_DIR = ROOT / "logs"
STATE_FILE = DATA_DIR / "state.json"
CONFIG_FILE = ROOT / "config.json"


def env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)).replace(",", "."))
    except ValueError:
        return default


def env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


@dataclass
class Settings:
    webhook: str
    interval_minutes: int
    min_cpm: float
    min_budget: float
    max_used_percent: float
    preferred_used_percent: float
    max_fast_deadline_hours: float
    show_browser: bool
    direct_url: str
    source_url: str
    accepted_platforms: list[str]
    primary_platform: str
    send_existing: bool
    notify_changes: bool


@dataclass
class Campaign:
    title: str
    creator: str
    category: str
    description: str
    cpm: float
    spent: float
    total_budget: float
    used_percent: float
    verified: bool
    approval_rate: int | None = None
    platforms: list[str] = field(default_factory=list)
    requirements: str = ""
    resources: list[dict[str, str]] = field(default_factory=list)
    whop_link: str = ""
    fast_deadline: str = ""
    rename_profile: bool = False
    rating: str = ""
    reasons: list[str] = field(default_factory=list)

    @property
    def remaining(self) -> float:
        return max(0.0, self.total_budget - self.spent)

    def fingerprint(self) -> str:
        relevant = {
            "embed_version": 2,
            "title": self.title,
            "cpm": self.cpm,
            "spent": self.spent,
            "total_budget": self.total_budget,
            "approval_rate": self.approval_rate,
            "platforms": self.platforms,
            "requirements": self.requirements,
            "resources": self.resources,
            "fast_deadline": self.fast_deadline,
            "rename_profile": self.rename_profile,
            "rating": self.rating,
        }
        raw = json.dumps(relevant, ensure_ascii=False, sort_keys=True)
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def load_settings() -> Settings:
    load_dotenv(ROOT / ".env")
    with CONFIG_FILE.open("r", encoding="utf-8") as handle:
        config = json.load(handle)

    webhook = os.getenv("DISCORD_WEBHOOK_URL", "").strip()
    return Settings(
        webhook=webhook,
        interval_minutes=max(1, env_int("CHECK_INTERVAL_MINUTES", 30)),
        min_cpm=env_float("MIN_CPM", 0.80),
        min_budget=env_float("MIN_TOTAL_BUDGET", 3000),
        max_used_percent=env_float("MAX_USED_PERCENT", 35),
        preferred_used_percent=env_float("PREFERRED_USED_PERCENT", 25),
        max_fast_deadline_hours=env_float("MAX_FAST_DEADLINE_HOURS", 3),
        show_browser=os.getenv("SHOW_BROWSER", "false").lower() == "true",
        direct_url=config["direct_campaign_page"],
        source_url=config["whop_page"],
        accepted_platforms=[x.lower() for x in config["platforms"]],
        primary_platform=config["primary_platform"].lower(),
        send_existing=bool(config.get("send_existing_on_first_run", True)),
        notify_changes=bool(config.get("notify_on_changes", True)),
    )


def ensure_dirs() -> None:
    DATA_DIR.mkdir(exist_ok=True)
    LOG_DIR.mkdir(exist_ok=True)


def log(message: str) -> None:
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{stamp}] {message}"
    print(line, flush=True)
    with (LOG_DIR / "bot.log").open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")


def load_state() -> dict[str, Any]:
    if not STATE_FILE.exists():
        return {"initialized": False, "campaigns": {}, "translations": {}}
    try:
        with STATE_FILE.open("r", encoding="utf-8") as handle:
            state = json.load(handle)
        state.setdefault("initialized", False)
        state.setdefault("campaigns", {})
        state.setdefault("translations", {})
        state.setdefault("next_color", 0)
        return state
    except (OSError, json.JSONDecodeError):
        return {"initialized": False, "campaigns": {}, "translations": {}}


def save_state(state: dict[str, Any]) -> None:
    temp = STATE_FILE.with_suffix(".tmp")
    with temp.open("w", encoding="utf-8") as handle:
        json.dump(state, handle, ensure_ascii=False, indent=2)
    temp.replace(STATE_FILE)


def money(value: float) -> str:
    return f"{value:,.0f} $".replace(",", " ")


def parse_money(raw: str) -> float:
    cleaned = raw.replace("\u202f", "").replace("\xa0", "").replace(" ", "")
    match = re.search(r"[\d,.]+", cleaned)
    if not match:
        return 0.0
    number = match.group(0)
    # Whop affiche les milliers au format américain : $250,000.
    # Une virgule suivie exactement de trois chiffres est donc un séparateur
    # de milliers, pas une décimale.
    if "," in number and "." not in number:
        parts = number.split(",")
        if len(parts) > 1 and all(len(part) == 3 for part in parts[1:]):
            number = "".join(parts)
        else:
            number = number.replace(",", ".")
    elif "," in number and "." in number:
        number = number.replace(",", "")
    try:
        return float(number)
    except ValueError:
        return 0.0


def extract_card(raw: dict[str, Any]) -> Campaign | None:
    text = raw.get("text", "")
    title = raw.get("title", "").strip()
    if not title:
        return None

    budget_match = re.search(
        r"\$([\d\s\u202f,]+(?:\.\d+)?)\s*/\s*\$([\d\s\u202f,]+(?:\.\d+)?)",
        text,
    )
    cpm_match = re.search(r"\$([\d.,]+)\s*/\s*1K", text, re.I)
    if not budget_match or not cpm_match:
        return None

    spent = parse_money(budget_match.group(1))
    total = parse_money(budget_match.group(2))
    cpm = parse_money(cpm_match.group(1))
    used = (spent / total * 100) if total else 100.0

    return Campaign(
        title=title,
        creator=raw.get("creator", "Non indiqué").strip() or "Non indiqué",
        category=raw.get("category", "Non indiquée").strip() or "Non indiquée",
        description=raw.get("description", "").strip(),
        cpm=cpm,
        spent=spent,
        total_budget=total,
        used_percent=used,
        verified=bool(raw.get("verified")),
    )


def extract_fast_deadline(text: str, max_hours: float) -> str:
    patterns = [
        r"(?:within|in|sous|dans)\s+(\d+(?:[.,]\d+)?)\s*(minutes?|mins?|heures?|hours?|hrs?)",
        r"(\d+(?:[.,]\d+)?)\s*(minutes?|mins?|heures?|hours?|hrs?)\s+(?:to|get|pour|afin)",
    ]
    for pattern in patterns:
        for match in re.finditer(pattern, text, re.I):
            value = float(match.group(1).replace(",", "."))
            unit = match.group(2).lower()
            hours = value / 60 if unit.startswith(("min", "minute")) else value
            if hours < max_hours:
                return match.group(0)
    return ""


def detected_platforms(text: str) -> list[str]:
    low = text.lower()
    found: list[str] = []
    if "tiktok" in low:
        found.append("TikTok")
    if "youtube shorts" in low or "yt shorts" in low or "youtube" in low:
        found.append("YouTube Shorts")
    if "instagram" in low or "reels" in low:
        found.append("Instagram")
    if re.search(r"(?:^|\W)x(?:\W|$)|twitter", low):
        found.append("X")
    return found


def classify(campaign: Campaign, settings: Settings) -> None:
    text = f"{campaign.description}\n{campaign.requirements}".lower()
    campaign.fast_deadline = extract_fast_deadline(
        campaign.requirements, settings.max_fast_deadline_hours
    )
    campaign.rename_profile = bool(
        re.search(
            r"(rename|change|modify|set up).{0,35}(profile|username|handle|bio|page)"
            r"|(?:profile|username|handle|bio|page).{0,35}(must|required|rename|change)",
            text,
            re.I,
        )
    )

    has_tiktok = "tiktok" in [p.lower() for p in campaign.platforms]
    has_youtube = "youtube shorts" in [p.lower() for p in campaign.platforms]
    reasons: list[str] = []

    if campaign.fast_deadline:
        campaign.rating = "À ÉVITER"
        reasons.append(f"délai trop court détecté : {campaign.fast_deadline}")
    elif not (has_tiktok or has_youtube):
        campaign.rating = "À ÉVITER"
        reasons.append("TikTok et YouTube Shorts non détectés")
    elif campaign.used_percent <= settings.preferred_used_percent and has_tiktok:
        campaign.rating = "EXCELLENT"
        reasons.append("TikTok accepté et cagnotte très peu utilisée")
    elif campaign.used_percent <= settings.max_used_percent:
        campaign.rating = "RECOMMANDÉ"
        reasons.append("respecte les critères de paiement et de cagnotte")
    else:
        campaign.rating = "MOYEN"

    if campaign.approval_rate is not None:
        if campaign.approval_rate >= 70:
            reasons.append(f"bon taux d’approbation ({campaign.approval_rate} %)")
        elif campaign.approval_rate < 40:
            reasons.append(f"taux d’approbation faible ({campaign.approval_rate} %)")
    else:
        reasons.append("taux d’approbation non indiqué")

    if campaign.verified:
        reasons.append("agence vérifiée")
    if campaign.rename_profile:
        reasons.append("attention : modification du profil probablement demandée")
    campaign.reasons = reasons


def basic_match(campaign: Campaign, settings: Settings) -> bool:
    return (
        campaign.cpm >= settings.min_cpm
        and campaign.total_budget >= settings.min_budget
        and campaign.used_percent <= settings.max_used_percent
    )


def scan_campaigns(page: Page, settings: Settings) -> list[Campaign]:
    log("Ouverture de Content Rewards...")
    page.goto(settings.direct_url, wait_until="domcontentloaded", timeout=60_000)
    page.wait_for_selector("section button h3", timeout=60_000)

    raw_cards = page.locator("section button:has(h3)").evaluate_all(
        """buttons => buttons.map(button => {
          const heading = button.querySelector('h3');
          const paragraph = button.querySelector('p');
          const images = Array.from(button.querySelectorAll('img'));
          const texts = Array.from(button.querySelectorAll('*'))
            .map(el => (el.childElementCount === 0 ? (el.textContent || '').trim() : ''))
            .filter(Boolean);
          const headingIndex = texts.indexOf((heading?.textContent || '').trim());
          let creator = '';
          let category = '';
          if (headingIndex > 0) {
            const before = texts.slice(0, headingIndex);
            creator = before.find(t => !/^·$/.test(t) && !/^\\d+(m|h|d|mo)$/.test(t)) || '';
            category = [...before].reverse().find(t =>
              !/^·$/.test(t) && !/^\\d+(m|h|d|mo)$/.test(t) && t !== creator
            ) || '';
          }
          return {
            title: (heading?.textContent || '').trim(),
            description: (paragraph?.textContent || '').trim(),
            creator,
            category,
            verified: images.some(img => /verified/i.test(img.alt || '')),
            text: button.innerText || ''
          };
        })"""
    )

    cards = [c for raw in raw_cards if (c := extract_card(raw))]
    candidates = [c for c in cards if basic_match(c, settings)]
    log(f"{len(cards)} campagnes trouvées, {len(candidates)} passent le premier filtre.")

    results: list[Campaign] = []
    for index, campaign in enumerate(candidates, start=1):
        try:
            locator = page.locator("section button:has(h3)").filter(
                has=page.get_by_role("heading", name=campaign.title, exact=True)
            )
            if locator.count() != 1:
                log(f"Fiche ambiguë ignorée : {campaign.title}")
                continue

            locator.click()
            dialog = page.locator('[role="dialog"]')
            dialog.wait_for(state="visible", timeout=15_000)
            detail = dialog.inner_text(timeout=10_000)
            campaign.whop_link = page.url

            approval = re.search(r"(\d+)\s*%\s*approval rate", detail, re.I)
            campaign.approval_rate = int(approval.group(1)) if approval else None
            campaign.platforms = detected_platforms(detail)

            req_match = re.search(
                r"Requirements\s+(.*?)(?:Earnings|Analytics|Resources|$)",
                detail,
                re.I | re.S,
            )
            campaign.requirements = (
                req_match.group(1).strip() if req_match else "Non indiquées"
            )

            links = dialog.locator("a").evaluate_all(
                """links => links.map(a => ({
                  text: (a.innerText || a.textContent || 'Ressource').trim(),
                  url: a.href
                })).filter(x => x.url && !x.url.startsWith('javascript:'))"""
            )
            campaign.resources = [
                {"name": item["text"][:100] or "Ressource", "url": item["url"]}
                for item in links[:8]
            ]

            classify(campaign, settings)
            results.append(campaign)
            log(f"[{index}/{len(candidates)}] {campaign.rating} — {campaign.title}")
        except Exception as exc:
            log(f"Impossible de lire la fiche « {campaign.title} » : {exc}")
        finally:
            try:
                page.keyboard.press("Escape")
                page.locator('[role="dialog"]').wait_for(state="hidden", timeout=5_000)
            except Exception:
                page.goto(settings.direct_url, wait_until="domcontentloaded", timeout=60_000)
                page.wait_for_selector("section button h3", timeout=60_000)

    return results


EMBED_COLORS = [
    0x5865F2,  # bleu Discord
    0x2ECC71,  # vert
    0x9B59B6,  # violet
    0xF39C12,  # orange
    0xE91E63,  # rose
    0x00B8D9,  # turquoise
]


def translate_text_fr(text: str, state: dict[str, Any]) -> str:
    """Traduit une seule fois puis conserve le résultat dans l'historique."""
    cleaned = text.strip()
    if not cleaned or cleaned in {"Non indiquées", "Non indiqué"}:
        return cleaned

    key = hashlib.sha256(cleaned.encode("utf-8")).hexdigest()
    cache = state.setdefault("translations", {})
    if key in cache:
        return cache[key]
    if GoogleTranslator is None:
        return cleaned

    try:
        translator = GoogleTranslator(source="auto", target="fr")
        # La limite du traducteur est évitée en découpant les textes longs.
        chunks = [cleaned[i : i + 3500] for i in range(0, len(cleaned), 3500)]
        translated = "\n".join(translator.translate(chunk) for chunk in chunks)
        cache[key] = translated
        return translated
    except Exception as exc:
        log(f"Traduction indisponible, texte original conservé : {exc}")
        return cleaned


def translate_campaign(campaign: Campaign, state: dict[str, Any]) -> None:
    campaign.description = translate_text_fr(campaign.description, state)
    campaign.requirements = translate_text_fr(campaign.requirements, state)


def discord_payload(
    campaign: Campaign, changed: bool = False, color: int = 0x5865F2
) -> dict[str, Any]:
    resources = "\n".join(
        f"[{r['name'][:60]}]({r['url']})" for r in campaign.resources
    ) or "Aucun lien public détecté"
    requirements = campaign.requirements.strip()
    if len(requirements) > 900:
        requirements = requirements[:897] + "..."

    fields = [
        {
            "name": "👤 Créateur / agence",
            "value": campaign.creator,
            "inline": True,
        },
        {
            "name": "🏷️ Catégorie",
            "value": campaign.category,
            "inline": True,
        },
        {
            "name": "💰 Rémunération",
            "value": f"**{campaign.cpm:.2f} $ / 1 000 vues**",
            "inline": True,
        },
        {
            "name": "🏦 Cagnotte restante",
            "value": f"**{money(campaign.remaining)}** / {money(campaign.total_budget)}",
            "inline": True,
        },
        {
            "name": "📊 Cagnotte utilisée",
            "value": f"**{campaign.used_percent:.1f} %**",
            "inline": True,
        },
        {
            "name": "🎯 Plateformes",
            "value": ", ".join(campaign.platforms) or "Non indiquées",
            "inline": True,
        },
        {
            "name": "✅ Taux d’approbation",
            "value": (
                f"{campaign.approval_rate} %"
                if campaign.approval_rate is not None
                else "Non indiqué"
            ),
            "inline": True,
        },
        {
            "name": "🪪 Profil à renommer",
            "value": "⚠️ Oui/probable" if campaign.rename_profile else "Non détecté",
            "inline": True,
        },
        {
            "name": "📋 Conditions pour être accepté",
            "value": requirements or "Non indiquées",
            "inline": False,
        },
        {
            "name": "⭐ Analyse du bot",
            "value": "\n".join(f"• {r}" for r in campaign.reasons) or "—",
            "inline": False,
        },
        {"name": "🔗 Ressources", "value": resources[:1024], "inline": False},
    ]

    rating_style = {
        "EXCELLENT": ("🟢", "EXCELLENT"),
        "RECOMMANDÉ": ("🟡", "RECOMMANDÉ"),
        "À ÉVITER": ("🔴", "À ÉVITER"),
    }
    rating_emoji, rating_label = rating_style.get(
        campaign.rating, ("⚪", campaign.rating or "NON CLASSÉ")
    )

    return {
        "username": "Alertes Whop",
        "embeds": [
            {
                "author": {
                    "name": f"{rating_emoji} {rating_label} • Whop Content Rewards"
                },
                "title": f"{'🔄 Offre modifiée • ' if changed else ''}{campaign.title}",
                "description": campaign.description[:1500] or "Aucune description.",
                "url": campaign.whop_link,
                "color": color,
                "fields": fields,
                "footer": {
                    "text": f"Classement : {campaign.rating} • Clique sur le titre pour ouvrir l’offre"
                },
                "timestamp": datetime.now().astimezone().isoformat(),
            }
        ],
    }


def webhook_base(webhook: str) -> str:
    parts = urllib.parse.urlsplit(webhook)
    return urllib.parse.urlunsplit((parts.scheme, parts.netloc, parts.path.rstrip("/"), "", ""))


def discord_request(
    url: str, method: str, payload: dict[str, Any] | None = None
) -> dict[str, Any] | None:
    body = (
        json.dumps(payload, ensure_ascii=False).encode("utf-8")
        if payload is not None
        else None
    )
    headers = {"User-Agent": "WhopAlertBot/1.0"}
    if body is not None:
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            raw = response.read()
            if response.status not in (200, 204):
                raise RuntimeError(f"Discord a répondu {response.status}")
            return json.loads(raw.decode("utf-8")) if raw else None
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Erreur Discord {exc.code}: {detail}") from exc


def send_discord(webhook: str, payload: dict[str, Any]) -> str:
    base = webhook_base(webhook)
    response = discord_request(base + "?wait=true", "POST", payload)
    if not response or not response.get("id"):
        raise RuntimeError("Discord n’a pas renvoyé l’identifiant du message.")
    return str(response["id"])


def edit_discord(
    webhook: str, message_id: str, payload: dict[str, Any]
) -> None:
    url = f"{webhook_base(webhook)}/messages/{message_id}"
    discord_request(url, "PATCH", payload)


def delete_discord(webhook: str, message_id: str) -> None:
    url = f"{webhook_base(webhook)}/messages/{message_id}"
    discord_request(url, "DELETE")


def status_payload(
    title: str,
    description: str,
    color: int,
    fields: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "username": "Alertes Whop",
        "embeds": [
            {
                "author": {"name": "Surveillance automatique Whop"},
                "title": title,
                "description": description,
                "color": color,
                "fields": fields or [],
                "footer": {"text": "Actualisation automatique toutes les 30 minutes"},
                "timestamp": datetime.now().astimezone().isoformat(),
            }
        ],
    }


def update_status_message(
    settings: Settings, state: dict[str, Any], payload: dict[str, Any]
) -> None:
    message_id = state.get("status_message_id")
    if message_id:
        try:
            edit_discord(settings.webhook, message_id, payload)
            return
        except Exception:
            log("Ancien message d'état introuvable, création d'un nouveau.")
    state["status_message_id"] = send_discord(settings.webhook, payload)
    save_state(state)


def run_scan(browser: Browser, settings: Settings, state: dict[str, Any]) -> None:
    page = browser.new_page()
    try:
        started_at = datetime.now().astimezone()
        update_status_message(
            settings,
            state,
            status_payload(
                "🔄 Actualisation en cours…",
                "Le bot examine les campagnes Whop et applique tes critères.",
                0xF1C40F,
                [
                    {
                        "name": "🕐 Démarrage",
                        "value": started_at.strftime("%d/%m/%Y à %H:%M"),
                        "inline": True,
                    },
                    {
                        "name": "🔎 État",
                        "value": "Analyse des offres…",
                        "inline": True,
                    },
                ],
            ),
        )
        campaigns = scan_campaigns(page, settings)
        previous = state["campaigns"]
        first_run = not state.get("initialized", False)
        sent = 0
        seen_keys: set[str] = set()

        for campaign in campaigns:
            key = campaign.whop_link or campaign.title
            seen_keys.add(key)
            fingerprint = campaign.fingerprint()
            old = previous.get(key)
            changed = bool(old and old.get("fingerprint") != fingerprint)
            discord_message_id = old.get("discord_message_id") if old else None
            should_send = (
                (first_run and settings.send_existing)
                or old is None
                or (changed and settings.notify_changes)
            )

            if campaign.rating == "À ÉVITER":
                should_send = False

            if should_send:
                translate_campaign(campaign, state)
                discord_color = (
                    old.get("discord_color") if old else None
                )
                if discord_color is None:
                    color_index = int(state.get("next_color", 0))
                    discord_color = EMBED_COLORS[color_index % len(EMBED_COLORS)]
                    state["next_color"] = color_index + 1
                payload = discord_payload(campaign, changed, int(discord_color))
                if changed and discord_message_id:
                    edit_discord(settings.webhook, discord_message_id, payload)
                else:
                    discord_message_id = send_discord(settings.webhook, payload)
                sent += 1
                time.sleep(1)

            previous[key] = {
                "fingerprint": fingerprint,
                "last_seen": datetime.now().astimezone().isoformat(),
                "discord_message_id": discord_message_id,
                "discord_color": (
                    discord_color if should_send else (old or {}).get("discord_color")
                ),
                "campaign": asdict(campaign),
            }

        # Une campagne qui ne fait plus partie des résultats admissibles est
        # retirée de l'historique après un scan terminé correctement.
        removed_keys = [key for key in list(previous) if key not in seen_keys]
        for key in removed_keys:
            message_id = previous[key].get("discord_message_id")
            try:
                if message_id:
                    delete_discord(settings.webhook, message_id)
                del previous[key]
            except Exception as exc:
                # On garde l'entrée afin de retenter la suppression au scan suivant.
                log(f"Suppression Discord à retenter pour {key} : {exc}")

        state["initialized"] = True
        state["last_scan"] = datetime.now().astimezone().isoformat()
        save_state(state)
        finished_at = datetime.now().astimezone()
        duration = max(1, int((finished_at - started_at).total_seconds()))
        next_run = finished_at + timedelta(minutes=settings.interval_minutes)
        update_status_message(
            settings,
            state,
            status_payload(
                "✅ Mise à jour terminée",
                "La recherche Whop est terminée et les messages Discord sont à jour.",
                0x2ECC71,
                [
                    {
                        "name": "🎯 Offres retenues",
                        "value": str(len(campaigns)),
                        "inline": True,
                    },
                    {
                        "name": "📨 Alertes ajoutées/modifiées",
                        "value": str(sent),
                        "inline": True,
                    },
                    {
                        "name": "🗑️ Offres supprimées",
                        "value": str(len(removed_keys)),
                        "inline": True,
                    },
                    {
                        "name": "⏱️ Durée",
                        "value": f"{duration} secondes",
                        "inline": True,
                    },
                    {
                        "name": "🕐 Prochaine vérification estimée",
                        "value": next_run.strftime("%d/%m/%Y à %H:%M"),
                        "inline": True,
                    },
                ],
            ),
        )
        log(
            f"Vérification terminée : {len(campaigns)} offres retenues, "
            f"{sent} alertes envoyées, {len(removed_keys)} anciennes offres supprimées."
        )
    except Exception as exc:
        try:
            update_status_message(
                settings,
                state,
                status_payload(
                    "⚠️ Actualisation interrompue",
                    "Une erreur est survenue. GitHub réessaiera lors de la prochaine exécution.",
                    0xE74C3C,
                    [{"name": "Erreur", "value": str(exc)[:900], "inline": False}],
                ),
            )
        except Exception:
            pass
        raise
    finally:
        page.close()


def test_discord(settings: Settings) -> None:
    payload = {
        "username": "Alertes Whop",
        "embeds": [
            {
                "title": "✅ Connexion réussie",
                "description": "Le bot Whop peut envoyer des messages dans ce salon.",
                "color": 0x2ECC71,
            }
        ],
    }
    send_discord(settings.webhook, payload)
    print("Message de test envoyé sur Discord.")


def validate_webhook(settings: Settings) -> None:
    if (
        not settings.webhook.startswith("https://discord.com/api/webhooks/")
        or "REMPLACE_MOI" in settings.webhook
    ):
        raise RuntimeError(
            "Webhook Discord absent. Ouvre .env et colle l'adresse après "
            "DISCORD_WEBHOOK_URL="
        )


def installed_browser_path() -> str | None:
    candidates = [
        Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe"),
        Path(r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"),
        Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"),
        Path(r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"),
        Path("/usr/bin/google-chrome"),
        Path("/usr/bin/google-chrome-stable"),
        Path("/usr/bin/chromium"),
        Path("/usr/bin/chromium-browser"),
    ]
    for candidate in candidates:
        if candidate.exists():
            return str(candidate)
    return None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--test-discord", action="store_true")
    parser.add_argument(
        "--once",
        action="store_true",
        help="Effectue une vérification puis s'arrête (GitHub Actions).",
    )
    args = parser.parse_args()

    ensure_dirs()
    settings = load_settings()
    try:
        validate_webhook(settings)
        if args.test_discord:
            test_discord(settings)
            return 0

        state = load_state()
        with sync_playwright() as playwright:
            executable = installed_browser_path()
            browser = playwright.chromium.launch(
                headless=not settings.show_browser,
                executable_path=executable,
            )
            try:
                while True:
                    try:
                        run_scan(browser, settings, state)
                    except KeyboardInterrupt:
                        raise
                    except Exception:
                        log("Erreur pendant la vérification :")
                        log(traceback.format_exc())
                        if args.once:
                            return 1
                    if args.once:
                        log("Vérification unique terminée.")
                        return 0
                    log(f"Prochaine vérification dans {settings.interval_minutes} minutes.")
                    time.sleep(settings.interval_minutes * 60)
            finally:
                browser.close()
    except KeyboardInterrupt:
        log("Bot arrêté par l’utilisateur.")
        return 0
    except Exception as exc:
        log(f"ERREUR : {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
