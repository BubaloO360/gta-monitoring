import json
import os
import re
import sys
from pathlib import Path

import requests
from bs4 import BeautifulSoup

TARGET_URL = "https://portwatch-vi.xyz/"
DISPLAY_URL = "https://portwatch-vi.xyz/#top"
STATE_FILE = Path("state.json")
CSS_SELECTOR = "#ov-score"


def load_state() -> dict:
    if not STATE_FILE.exists():
        return {"notified_90": False, "notified_100": False}
    return json.loads(STATE_FILE.read_text(encoding="utf-8"))


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")


def fetch_html(url: str) -> str:
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/131.0.0.0 Safari/537.36"
        )
    }
    resp = requests.get(url, headers=headers, timeout=20)
    resp.raise_for_status()
    return resp.text


def extract_score(html: str) -> float | None:
    soup = BeautifulSoup(html, "html.parser")
    el = soup.select_one(CSS_SELECTOR)

    if not el:
        print(f"AVISO: elemento '{CSS_SELECTOR}' não encontrado no HTML estático.", file=sys.stderr)
        return None

    raw_text = el.get_text(strip=True)
    match = re.search(r"\d+(?:[.,]\d+)?", raw_text)
    if not match:
        print(
            f"AVISO: elemento '{CSS_SELECTOR}' encontrado, mas sem número válido "
            f"(conteúdo lido: {raw_text!r}). Provavelmente preenchido via JavaScript.",
            file=sys.stderr,
        )
        return None

    value = float(match.group(0).replace(",", "."))
    if not (0.0 <= value <= 100.0):
        print(f"AVISO: valor fora da faixa esperada (0-100): {value}", file=sys.stderr)
        return None

    return value


def send_discord_alert(webhook_url: str, milestone: int, current_value: float) -> None:
    is_complete = milestone == 100
    formatted = f"{current_value:g}%"
    payload = {
        "username": "Portwatch VI Monitor",
        "embeds": [
            {
                "title": f"Progresso atingiu {milestone}%!",
                "description": f"O marcador `#ov-score` do site chegou a **{formatted}**.",
                "url": DISPLAY_URL,
                "color": 0x2ECC71 if is_complete else 0xF1C40F,
                "fields": [
                    {"name": "Marco", "value": f"{milestone}%", "inline": True},
                    {"name": "Valor lido", "value": formatted, "inline": True},
                ],
            }
        ],
    }
    resp = requests.post(webhook_url, json=payload, timeout=15)
    resp.raise_for_status()


def main() -> None:
    webhook_url = os.environ.get("DISCORD_WEBHOOK_URL")
    if not webhook_url:
        print("ERRO: variável DISCORD_WEBHOOK_URL não configurada.", file=sys.stderr)
        sys.exit(1)

    state = load_state()
    if state.get("notified_90") and state.get("notified_100"):
        print("Marcos de 90% e 100% já notificados. Encerrando.")
        return

    html = fetch_html(TARGET_URL)
    current = extract_score(html)

    if current is None:
        sys.exit(1)

    print(f"Valor atual em {CSS_SELECTOR}: {current:g}%")

    changed = False
    if current >= 100.0 and not state["notified_100"]:
        send_discord_alert(webhook_url, 100, current)
        state["notified_100"] = True
        state["notified_90"] = True
        changed = True
        print("Alerta de 100% enviado.")
    elif current >= 90.0 and not state["notified_90"]:
        send_discord_alert(webhook_url, 90, current)
        state["notified_90"] = True
        changed = True
        print("Alerta de 90% enviado.")
    else:
        print("Nenhum marco novo atingido. Nenhuma notificação enviada.")

    if changed:
        save_state(state)


if __name__ == "__main__":
    main()
