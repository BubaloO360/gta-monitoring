import json
import os
import re
import sys
import time
from pathlib import Path

import requests
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

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


def parse_score(raw_text: str) -> float | None:
    match = re.search(r"\d+(?:[.,]\d+)?", raw_text.strip())
    if not match:
        return None
    value = float(match.group(0).replace(",", "."))
    return value if 0.0 <= value <= 100.0 else None


def fetch_rendered_score(url: str) -> float | None:
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/131.0.0.0 Safari/537.36"
            )
        )
        page.goto(url, wait_until="domcontentloaded", timeout=30000)

        locator = page.locator(CSS_SELECTOR).first
        locator.wait_for(state="attached", timeout=15000)
        locator.scroll_into_view_if_needed()

        # Aguarda o JavaScript atualizar o valor inicial (0) para o valor real
        try:
            page.wait_for_function(
                """(selector) => {
                    const el = document.querySelector(selector);
                    if (!el) return false;
                    const val = parseFloat(el.textContent.replace(',', '.').trim());
                    return !isNaN(val) && val > 0;
                }""",
                arg=CSS_SELECTOR,
                timeout=15000,
            )
        except PlaywrightTimeoutError:
            pass

        # Aguarda o número estabilizar (evita capturar meio da animação de contagem)
        last_val = None
        stable_reads = 0
        for _ in range(14):  # até 7 segundos
            raw = locator.inner_text()
            current_val = parse_score(raw)
            if current_val is not None and current_val == last_val:
                stable_reads += 1
                if stable_reads >= 3:
                    break
            else:
                stable_reads = 0
                last_val = current_val
            time.sleep(0.5)

        browser.close()
        return last_val


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

    current = fetch_rendered_score(TARGET_URL)
    if current is None:
        print(f"ERRO: não foi possível ler um valor válido em '{CSS_SELECTOR}'.", file=sys.stderr)
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
