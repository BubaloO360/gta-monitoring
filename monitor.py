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
    ("groundwork", "Preparação", 0),
    ("loads", "Exe rodando", 25),
    ("boots", "Primeiro frame", 40),
    ("menu", "Menu principal", 55),
    ("ingame", "Dentro do jogo", 70),
    ("fps30", "30 FPS", 85),
    ("stable", "Estável", 100),
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

STABILITY_LABEL_PT = {
    "unknown": "Desconhecida",
    "constant": "Trava constantemente",
    "often": "Trava com frequência",
    "sometimes": "Trava às vezes",
    "rarely": "Raramente trava",
    "stable": "Estável",
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
    default = {
        "last_score": None,
        "last_leader": None,
        "notified_90": False,
        "notified_100": False,
    }
    if not STATE_FILE.exists():
        return default
    raw = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    return {**default, **raw}


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


def _translate_mymemory(text: str) -> str | None:
    params = urllib.parse.urlencode({"q": text[:500], "langpair": "en|pt-BR"})
    req = urllib.request.Request(
        f"https://api.mymemory.translated.net/get?{params}",
        headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
        method="GET",
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    if data.get("responseStatus") != 200:
        return None
    translated = (data.get("responseData") or {}).get("translatedText", "").strip()
    if not translated or "MYMEMORY WARNING" in translated.upper():
        return None
    return translated


def _translate_google_clients5(text: str) -> str | None:
    params = urllib.parse.urlencode(
        {"client": "dict-chrome-ex", "sl": "en", "tl": "pt-BR", "q": text}
    )
    req = urllib.request.Request(
        f"https://clients5.google.com/translate_a/t?{params}",
        headers={"User-Agent": USER_AGENT},
        method="GET",
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    if isinstance(data, list) and data:
        first = data[0]
        if isinstance(first, str):
            return first.strip() or None
        if isinstance(first, list) and first and isinstance(first[0], str):
            return first[0].strip() or None
    return None


def _translate_google_gtx(text: str) -> str | None:
    params = urllib.parse.urlencode(
        {"client": "gtx", "sl": "en", "tl": "pt-BR", "dt": "t", "q": text}
    )
    req = urllib.request.Request(
        f"https://translate.googleapis.com/translate_a/single?{params}",
        headers={"User-Agent": USER_AGENT},
        method="GET",
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    translated = "".join(part[0] for part in data[0] if part and part[0]).strip()
    return translated or None


def translate_ptbr(text: str) -> str:
    clean = (text or "").strip()
    if not clean:
        return ""

    providers = (
        ("MyMemory", _translate_mymemory),
        ("Google-clients5", _translate_google_clients5),
        ("Google-gtx", _translate_google_gtx),
    )

    for name, fn in providers:
        try:
            result = fn(clean)
            if result:
                return result
        except Exception as exc:
            print(f"AVISO tradução ({name}): {type(exc).__name__}", file=sys.stderr)

    return clean


def format_bullet_list(items: list[str], prefix: str, max_len: int = 950) -> str:
    if not items:
        return ""
    lines = [f"{prefix} {translate_ptbr(item)}" for item in items[:5] if str(item).strip()]
    joined = "\n".join(lines)
    if len(joined) > max_len:
        return joined[: max_len - 3].rstrip() + "..."
    return joined


def send_discord_alert(
    webhook_url: str,
    title: str,
    current_value: float,
    previous_value: float | None,
    leader: dict | None,
    color: int,
) -> None:
    effort = leader["effort"] if leader else {}
    leader_name = effort.get("name", "Desconhecido")
    reached_label = leader["reached_label"] if leader else "-"

    formatted_now = f"{current_value:g}%"
    if previous_value is not None and current_value > previous_value:
        progress_str = f"{previous_value:g}% → **{formatted_now}** (+{current_value - previous_value:g}%)"
    else:
        progress_str = f"**{formatted_now}**"

    summary_pt = translate_ptbr(effort.get("summary", ""))
    description = f"O progresso geral chegou a {progress_str}."
    if summary_pt:
        description += f"\n\n{summary_pt}"

    fields = [
        {"name": "Projeto líder", "value": leader_name, "inline": True},
        {"name": "Marco atual", "value": reached_label, "inline": True},
    ]

    gta = effort.get("gta") or {}
    if gta.get("fps"):
        fields.append({"name": "Desempenho", "value": f"{round(float(gta['fps']))} FPS", "inline": True})
    elif gta.get("stability") and gta["stability"] != "unknown":
        stab_pt = STABILITY_LABEL_PT.get(gta["stability"], gta["stability"])
        fields.append({"name": "Estabilidade", "value": stab_pt, "inline": True})

    works_text = format_bullet_list(effort.get("highlights") or [], "✅")
    if works_text:
        fields.append({"name": "O que funciona", "value": works_text, "inline": False})

    blockers_text = format_bullet_list(effort.get("blockers") or [], "⛔")
    if blockers_text:
        fields.append({"name": "O que está travando", "value": blockers_text, "inline": False})

    embed = {
        "title": title,
        "description": description[:4000],
        "url": DISPLAY_URL,
        "color": color,
        "fields": fields,
    }
    if effort.get("assessedAt"):
        embed["footer"] = {"text": f"Revisado em {effort['assessedAt']}"}

    payload = {
        "username": "Portwatch VI Monitor",
        "embeds": [embed],
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

    try:
        data = fetch_tracker()
        ov = compute_overall(data.get("efforts", []))
        current = ov["score"]
        leader = ov["leader"]
        leader_id = leader["effort"].get("id") if leader else None
        leader_name = leader["effort"].get("name") if leader else "Nenhum"

        print(f"Score atual (#ov-score): {current:g}% | Líder: {leader_name}")

        if test_mode:
            send_discord_alert(
                webhook_url,
                f"Teste Portwatch VI — {current:g}% ({leader_name})",
                current,
                state["last_score"],
                leader,
                0x3498DB,
            )
            print("Notificação de teste enviada no Discord (state.json não alterado).")
            return

        last_score = state["last_score"]

        # Primeira execução após atualizar o script: inicializa o estado sem disparar alerta falso
        if last_score is None:
            state["last_score"] = current
            state["last_leader"] = leader_id
            state["notified_90"] = current >= 90.0
            state["notified_100"] = current >= 100.0
            save_state(state)
            print(f"Estado inicial salvo em {current:g}% ({leader_name}).")
            return

        advanced = current > last_score
        if not advanced:
            print(f"Sem avanço (atual: {current:g}% | último registrado: {last_score:g}%).")
            return

        if current >= 100.0 and not state["notified_100"]:
            title = f"🎉 MARCO 100%! {leader_name} atingiu {current:g}%"
            color = 0x2ECC71
            state["notified_100"] = True
            state["notified_90"] = True
        elif current >= 90.0 and not state["notified_90"]:
            title = f"🔥 MARCO 90%! {leader_name} atingiu {current:g}%"
            color = 0xF1C40F
            state["notified_90"] = True
        else:
            title = f"📈 Novo avanço no Portwatch VI: {last_score:g}% → {current:g}%"
            color = 0xFF2E7E

        send_discord_alert(webhook_url, title, current, last_score, leader, color)
        state["last_score"] = current
        state["last_leader"] = leader_id
        save_state(state)
        print(f"Alerta de avanço enviado ({last_score:g}% -> {current:g}%).")
    except RuntimeError as exc:
        print(f"ERRO: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
