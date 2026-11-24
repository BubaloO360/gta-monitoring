import json
import math
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

TRACKER_URL = "https://portwatch-vi.xyz/data/tracker.json"
DISPLAY_URL = "https://portwatch-vi.xyz/#top"
STATE_FILE = Path("state.json")

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/145.0.0.0 Safari/537.36"
)

MILESTONES = [
    ("groundwork", "Groundwork", 0),
    ("loads", "Exe runs", 25),
    ("boots", "First frame", 40),
    ("menu", "Main menu", 55),
    ("ingame", "In-game", 70),
    ("fps30", "30 FPS", 85),
    ("stable", "Stable", 100),
]
TICK = {key: at for key, _, at in MILESTONES}
MILESTONE_LABEL = {key: label for key, label, _ in MILESTONES}
LADDER = ["loads", "boots", "menu", "ingame"]

GROUNDWORK = [
    ("loader", 0.2),
    ("shaders", 0.28),
    ("graphics", 0.32),
    ("proven", 0.2),
]

STABILITY = {
    "unknown": 0.0,
    "constant": 0.0,
    "often": 0.25,
    "sometimes": 0.55,
    "rarely": 0.8,
    "stable": 1.0,
}


def js_round_1(value: float) -> float:
    return math.floor(value * 10 + 0.5) / 10


def clamp01(value) -> float:
    if value is None:
        return 0.0
    try:
        num = float(value)
    except (TypeError, ValueError):
        return 0.0
    if not math.isfinite(num):
        return 0.0
    return min(1.0, max(0.0, num))


def ladder_value(value) -> float:
    if value is True:
        return 1.0
    if value is False or value is None:
        return 0.0
    return clamp01(value)


def groundwork_fraction(gw) -> float:
    if not isinstance(gw, dict):
        return 0.0
    return sum(weight * clamp01(gw.get(key)) for key, weight in GROUNDWORK)


def score_effort(effort: dict) -> dict:
    gw = groundwork_fraction(effort.get("groundwork"))
    gta = effort.get("gta") or {}
    score = TICK["loads"] * gw
    reached = "groundwork"
    prev_tick = score

    for key in LADDER:
        v = ladder_value(gta.get(key))
        if v >= 1.0:
            score = float(TICK[key])
            prev_tick = score
            reached = key
            continue
        if v > 0.0:
            score = max(score, prev_tick + v * (TICK[key] - prev_tick))
        break

    if reached == "ingame":
        try:
            fps_num = float(gta.get("fps") or 0)
        except (TypeError, ValueError):
            fps_num = 0.0
        fps = clamp01(fps_num / 30.0)
        stab = STABILITY.get(gta.get("stability"), STABILITY["unknown"])
        score = (
            TICK["ingame"]
            + fps * (TICK["fps30"] - TICK["ingame"])
            + stab * (TICK["stable"] - TICK["fps30"])
        )
        if fps >= 1.0:
            reached = "fps30"
        if fps >= 1.0 and stab >= 1.0:
            reached = "stable"

    return {
        "score": js_round_1(score),
        "reached": reached,
        "reached_label": MILESTONE_LABEL[reached],
    }


def compute_overall(efforts: list[dict]) -> dict:
    scored = []
    for effort in efforts:
        if effort.get("nuked"):
            continue
        stars = (effort.get("stats") or {}).get("stars") or 0
        scored.append({"effort": effort, "stars": stars, **score_effort(effort)})

    scored.sort(key=lambda item: (-item["score"], -item["stars"]))
    leader = scored[0] if scored else None
    return {
        "leader": leader,
        "score": leader["score"] if leader else 0.0,
    }


def load_state() -> dict:
    if not STATE_FILE.exists():
        return {"notified_90": False, "notified_100": False}
    return json.loads(STATE_FILE.read_text(encoding="utf-8"))


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")


def fetch_tracker() -> dict:
    query = urllib.parse.urlencode({"ts": int(time.time() // 60)})
    req = urllib.request.Request(
        f"{TRACKER_URL}?{query}",
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "application/json, */*",
            "Referer": "https://portwatch-vi.xyz/",
        },
        method="GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"Erro HTTP {exc.code} ao consultar tracker.json.") from None
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Erro de rede ao consultar tracker.json: {exc.reason}") from None
    except json.JSONDecodeError:
        raise RuntimeError("Resposta inválida (JSON corrompido) em tracker.json.") from None


def send_discord_alert(
    webhook_url: str,
    title: str,
    current_value: float,
    leader: dict | None,
    color: int,
) -> None:
    formatted = f"{current_value:g}%"
    leader_name = leader["effort"].get("name", "Desconhecido") if leader else "Nenhum"
    reached_label = leader["reached_label"] if leader else "-"

    payload = {
        "username": "Portwatch VI Monitor",
        "embeds": [
            {
                "title": title,
                "description": f"O progresso geral (`#ov-score`) chegou a **{formatted}**.",
                "url": DISPLAY_URL,
                "color": color,
                "fields": [
                    {"name": "Progresso", "value": formatted, "inline": True},
                    {"name": "Projeto líder", "value": leader_name, "inline": True},
                    {"name": "Marco atual", "value": reached_label, "inline": True},
                ],
            }
        ],
    }

    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        webhook_url,
        data=body,
        headers={
            "Content-Type": "application/json",
            "User-Agent": USER_AGENT,
        },
        method="POST",
    )

    # Captura qualquer erro sem propagar exceção original (que conteria a URL do webhook).
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            if not (200 <= resp.status < 300):
                raise RuntimeError(f"Discord retornou status inesperado: HTTP {resp.status}.")
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"Falha ao enviar alerta ao Discord (HTTP {exc.code}).") from None
    except urllib.error.URLError:
        raise RuntimeError("Falha de conexão ao enviar alerta ao Discord.") from None
    except OSError:
        raise RuntimeError("Erro de rede/timeout ao enviar alerta ao Discord.") from None


def main() -> None:
    webhook_url = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()
    if not webhook_url:
        print("ERRO: variável DISCORD_WEBHOOK_URL não configurada.", file=sys.stderr)
        sys.exit(1)

    test_mode = os.environ.get("TEST_NOTIFICATION", "").lower() == "true"

    state = load_state()
    if not test_mode and state.get("notified_90") and state.get("notified_100"):
        print("Marcos de 90% e 100% já notificados. Encerrando.")
        return

    try:
        data = fetch_tracker()
        ov = compute_overall(data.get("efforts", []))
        current = ov["score"]
        leader = ov["leader"]
        leader_name = leader["effort"].get("name") if leader else "Nenhum"

        print(f"Score atual (#ov-score): {current:g}% | Líder: {leader_name}")

        if test_mode:
            send_discord_alert(
                webhook_url,
                "Teste de monitoramento Portwatch VI",
                current,
                leader,
                0x3498DB,
            )
            print("Notificação de teste enviada no Discord (state.json não alterado).")
            return

        changed = False
        if current >= 100.0 and not state["notified_100"]:
            send_discord_alert(webhook_url, "Progresso atingiu 100%!", current, leader, 0x2ECC71)
            state["notified_100"] = True
            state["notified_90"] = True
            changed = True
            print("Alerta de 100% enviado.")
        elif current >= 90.0 and not state["notified_90"]:
            send_discord_alert(webhook_url, "Progresso atingiu 90%!", current, leader, 0xF1C40F)
            state["notified_90"] = True
            changed = True
            print("Alerta de 90% enviado.")
        else:
            print("Nenhum marco novo atingido. Nenhuma notificação enviada.")

        if changed:
            save_state(state)
    except RuntimeError as exc:
        print(f"ERRO: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
