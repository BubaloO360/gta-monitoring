import io
import json
import math
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

try:
    from PIL import Image, ImageDraw, ImageFont
except ImportError:
    Image = ImageDraw = ImageFont = None

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
MILESTONE_IDX = {key: idx for idx, (key, _, _) in enumerate(MILESTONES)}
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

STABILITY_BAR_PT = {
    "unknown": "Estável",
    "constant": "Instável",
    "often": "Trava muito",
    "sometimes": "Às vezes",
    "rarely": "Quase estável",
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

    manual_score = effort.get("manualScore")
    if (
        isinstance(manual_score, (int, float))
        and not isinstance(manual_score, bool)
        and math.isfinite(manual_score)
    ):
        score = min(100.0, max(0.0, float(manual_score)))

    return {
        "score": js_round_1(score),
        "groundwork": gw,
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
    furthest_idx = max((MILESTONE_IDX[item["reached"]] for item in scored), default=0)
    return {
        "leader": leader,
        "score": leader["score"] if leader else 0.0,
        "furthest_idx": furthest_idx,
        "ranked": scored,
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


# ---------- Geração do GIF animado das duas barras ----------

def _load_font(size: int, bold: bool = False):
    candidates = (
        [
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
            "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
        ]
        if bold
        else [
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
            "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
        ]
    )
    for path in candidates:
        if os.path.exists(path):
            return ImageFont.truetype(path, size)
    return ImageFont.load_default()


def _lerp_color(c1: tuple[int, int, int], c2: tuple[int, int, int], t: float) -> tuple[int, int, int]:
    return (
        int(c1[0] + (c2[0] - c1[0]) * t),
        int(c1[1] + (c2[1] - c1[1]) * t),
        int(c1[2] + (c2[2] - c1[2]) * t),
    )


def _build_gradient_strip(width: int, height: int):
    stops = [
        (0.00, (122, 92, 255)),   # #7a5cff roxo
        (0.25, (186, 82, 224)),   # lilás
        (0.55, (255, 46, 126)),   # #ff2e7e rosa
        (0.80, (255, 98, 102)),   # coral
        (1.00, (255, 138, 61)),   # #ff8a3d laranja
    ]
    strip = Image.new("RGB", (width, height))
    draw = ImageDraw.Draw(strip)
    for x in range(width):
        pos = x / max(1, width - 1)
        color = stops[-1][1]
        for i in range(len(stops) - 1):
            p0, c0 = stops[i]
            p1, c1 = stops[i + 1]
            if p0 <= pos <= p1:
                t = (pos - p0) / (p1 - p0)
                color = _lerp_color(c0, c1, t)
                break
        draw.line([(x, 0), (x, height)], fill=color)
    return strip


def _draw_progress_bar(
    img,
    draw,
    grad_strip,
    x0: int,
    y0: int,
    width: int,
    height: int,
    pct_value: float,
    bg_color: tuple[int, int, int],
) -> None:
    radius = height // 2
    track_color = (29, 32, 45)
    draw.rounded_rectangle([x0, y0, x0 + width, y0 + height], radius=radius, fill=track_color)

    fill_w = int(round((max(0.0, min(100.0, pct_value)) / 100.0) * width))
    if fill_w > 0:
        mask = Image.new("L", (width, height), 0)
        mdraw = ImageDraw.Draw(mask)
        # Trilha externa arredondada + preenchimento arredondado na ponta direita
        mdraw.rounded_rectangle([0, 0, fill_w, height], radius=min(radius, max(1, fill_w // 2)), fill=255)
        track_mask = Image.new("L", (width, height), 0)
        ImageDraw.Draw(track_mask).rounded_rectangle([0, 0, width, height], radius=radius, fill=255)
        combined = Image.composite(mask, Image.new("L", (width, height), 0), track_mask)
        bar_grad = grad_strip.resize((width, height)) if grad_strip.size != (width, height) else grad_strip
        img.paste(bar_grad, (x0, y0), combined)

    # Divisórias verticais em 25%, 40%, 55%, 70%, 85%
    for _, _, at in MILESTONES[1:-1]:
        dx = x0 + int(round((at / 100.0) * width))
        draw.rectangle([dx - 1, y0, dx + 1, y0 + height], fill=bg_color)


def _draw_status_dot(draw, cx: int, cy: int, state: str) -> None:
    r = 4
    pink = (255, 46, 126)
    dark = (45, 51, 68)
    if state == "reached":
        draw.ellipse([cx - r, cy - r, cx + r, cy + r], fill=pink)
    elif state == "partial":
        draw.ellipse([cx - r, cy - r, cx + r, cy + r], fill=dark)
        draw.pieslice([cx - r, cy - r, cx + r, cy + r], start=90, end=270, fill=pink)
    elif state == "next":
        draw.ellipse([cx - r, cy - r, cx + r, cy + r], outline=pink, width=2)
    else:
        draw.ellipse([cx - r, cy - r, cx + r, cy + r], fill=dark)


def _draw_milestone_label(draw, x: int, y: int, label: str, state: str, font, align: str) -> None:
    bbox = draw.textbbox((0, 0), label, font=font)
    tw = bbox[2] - bbox[0]
    th = bbox[3] - bbox[1]
    dot_gap = 12
    total_w = dot_gap + tw

    if align == "left":
        start_x = x
    elif align == "right":
        start_x = x - total_w
    else:
        start_x = int(x - total_w / 2)

    text_color = (
        (242, 244, 248)
        if state == "reached"
        else (198, 204, 218)
        if state in ("partial", "next")
        else (98, 105, 124)
    )
    _draw_status_dot(draw, start_x + 4, y + th // 2 + 2, state)
    draw.text((start_x + dot_gap, y), label, font=font, fill=text_color)


def _project_milestone_items(leader: dict | None) -> list[tuple[int, str, str]]:
    if not leader:
        return [(at, label, "unreached") for _, label, at in MILESTONES]

    effort = leader["effort"]
    gta = effort.get("gta") or {}
    gw = leader.get("groundwork", 0.0)
    past_gw = ladder_value(gta.get("loads")) >= 1.0

    def st(val: float) -> str:
        if val >= 1.0:
            return "reached"
        if val > 0.0:
            return "partial"
        return "unreached"

    fps_raw = gta.get("fps")
    try:
        fps_num = float(fps_raw) if fps_raw else 0.0
    except (TypeError, ValueError):
        fps_num = 0.0
    fps_label = f"{round(fps_num)} FPS" if fps_num > 0 else "30 FPS"
    fps_state = st(min(1.0, fps_num / 30.0))

    stab_key = gta.get("stability") or "unknown"
    stab_val = STABILITY.get(stab_key, 0.0)
    stab_label = STABILITY_BAR_PT.get(stab_key, "Estável")
    stab_state = st(stab_val)

    return [
        (0, "Preparação", "reached" if past_gw else st(gw)),
        (25, "Exe rodando", st(ladder_value(gta.get("loads")))),
        (40, "Primeiro frame", st(ladder_value(gta.get("boots")))),
        (55, "Menu principal", st(ladder_value(gta.get("menu")))),
        (70, "Dentro do jogo", st(ladder_value(gta.get("ingame")))),
        (85, fps_label, fps_state),
        (100, stab_label, stab_state),
    ]


def generate_progress_gif(ov: dict) -> bytes | None:
    if Image is None:
        return None

    W, H = 1000, 355
    BG = (15, 16, 22)
    BAR_X, BAR_W = 55, 890
    BAR1_Y, BAR1_H = 112, 24
    BAR2_Y, BAR2_H = 266, 20

    font_header = _load_font(13, bold=True)
    font_score = _load_font(18, bold=True)
    font_pct = _load_font(12, bold=True)
    font_label = _load_font(12, bold=True)
    font_pill = _load_font(12, bold=True)
    font_init = _load_font(12, bold=True)

    grad1 = _build_gradient_strip(BAR_W, BAR1_H)
    grad2 = _build_gradient_strip(BAR_W, BAR2_H)

    target_score = ov["score"]
    leader = ov["leader"]
    leader_name = leader["effort"].get("name", "Líder") if leader else "Nenhum"
    leader_label = leader["reached_label"] if leader else "-"
    furthest_idx = ov.get("furthest_idx", 0)
    proj_items = _project_milestone_items(leader)

    # Posicionamento sem sobreposição dos marcadores do Top 6 na barra geral
    top_ranked = ov.get("ranked", [])[:6]
    markers = []
    for r in top_ranked:
        tx = BAR_X + (r["score"] / 100.0) * BAR_W
        markers.append({"r": r, "tx": tx, "ax": tx})
    markers.sort(key=lambda m: m["tx"])

    gap = 40.0
    for i in range(1, len(markers)):
        markers[i]["ax"] = max(markers[i]["tx"], markers[i - 1]["ax"] + gap)
    if markers:
        shift = sum(m["tx"] - m["ax"] for m in markers) / len(markers)
        for i, m in enumerate(markers):
            m["ax"] = max(BAR_X + 18.0, m["ax"] + shift)
            if i > 0:
                m["ax"] = max(m["ax"], markers[i - 1]["ax"] + gap)
        overflow = markers[-1]["ax"] - (BAR_X + BAR_W - 18.0)
        if overflow > 0:
            for m in markers:
                m["ax"] -= overflow

    # Base estática (títulos, marcadores do top 6, rótulos de ambas as barras)
    base = Image.new("RGB", (W, H), BG)
    bdraw = ImageDraw.Draw(base)

    # Cabeçalho Seção 1
    bdraw.text((BAR_X, 18), "PROGRESSO GERAL", font=font_header, fill=(142, 148, 168))
    bdraw.line([(BAR_X, 208), (BAR_X + BAR_W, 208)], fill=(32, 36, 50), width=1)

    # Marcadores do Top 6 acima da barra geral
    circle_y = 74
    circle_r = 14
    for m in markers:
        eff = m["r"]["effort"]
        is_leader = leader is not None and eff is leader["effort"]
        ax, tx = int(round(m["ax"])), int(round(m["tx"]))
        line_col = (255, 46, 126) if is_leader else (66, 72, 92)
        bdraw.line([(ax, circle_y + circle_r), (tx, BAR1_Y - 1)], fill=line_col, width=2)
        bdraw.ellipse([tx - 3, BAR1_Y - 4, tx + 3, BAR1_Y + 2], fill=line_col)

        fill_c = (28, 30, 42)
        outline_c = (255, 46, 126) if is_leader else (85, 92, 114)
        bdraw.ellipse(
            [ax - circle_r, circle_y - circle_r, ax + circle_r, circle_y + circle_r],
            fill=fill_c,
            outline=outline_c,
            width=2,
        )
        initials = (eff.get("name") or "??")[:2]
        ib = bdraw.textbbox((0, 0), initials, font=font_init)
        bdraw.text(
            (ax - (ib[2] - ib[0]) / 2, circle_y - (ib[3] - ib[1]) / 2 - 1),
            initials,
            font=font_init,
            fill=(245, 247, 252) if is_leader else (180, 186, 204),
        )
        if is_leader:
            pill_text = f"{leader_name} · {target_score:g}%"
            pb = bdraw.textbbox((0, 0), pill_text, font=font_pill)
            pw, ph = pb[2] - pb[0] + 18, pb[3] - pb[1] + 10
            px = int(max(BAR_X, min(BAR_X + BAR_W - pw, ax - pw / 2)))
            py = 36
            bdraw.rounded_rectangle([px, py, px + pw, py + ph], radius=10, fill=(26, 29, 42), outline=(66, 72, 92), width=1)
            bdraw.text((px + 9, py + 4), pill_text, font=font_pill, fill=(242, 244, 248))

    # Porcentagens e rótulos da Barra Geral
    for idx, (_, label, at) in enumerate(MILESTONES):
        mx = BAR_X + int(round((at / 100.0) * BAR_W))
        align = "left" if at == 0 else ("right" if at == 100 else "center")
        st = "reached" if idx <= furthest_idx else ("next" if idx == furthest_idx + 1 else "unreached")
        pct_text = f"{at}%"
        pb = bdraw.textbbox((0, 0), pct_text, font=font_pct)
        pw = pb[2] - pb[0]
        px = mx if align == "left" else (mx - pw if align == "right" else int(mx - pw / 2))
        pct_col = (255, 46, 126) if st == "reached" else (112, 118, 138)
        bdraw.text((px, BAR1_Y + BAR1_H + 12), pct_text, font=font_pct, fill=pct_col)
        _draw_milestone_label(bdraw, mx, BAR1_Y + BAR1_H + 30, label, st, font_label, align)

    # Cabeçalho Seção 2 (Projeto Líder)
    bdraw.text((BAR_X, 228), f"PROJETO LÍDER · {leader_name.upper()}", font=font_header, fill=(142, 148, 168))
    sub_right = f"{leader_label}"
    sb = bdraw.textbbox((0, 0), sub_right, font=font_header)
    bdraw.text((BAR_X + BAR_W - (sb[2] - sb[0]), 228), sub_right, font=font_header, fill=(198, 204, 218))

    # Rótulos da Barra do Projeto
    for at, label, st in proj_items:
        mx = BAR_X + int(round((at / 100.0) * BAR_W))
        align = "left" if at == 0 else ("right" if at == 100 else "center")
        _draw_milestone_label(bdraw, mx, BAR2_Y + BAR2_H + 16, label, st, font_label, align)

    # Gera quadros da animação (ease-out cúbico de 0% até target_score)
    total_steps = 18
    frames = []
    durations = []

    for step in range(total_steps + 1):
        p = step / total_steps
        eased = 1.0 - (1.0 - p) ** 3
        val = target_score * eased

        frame = base.copy()
        fdraw = ImageDraw.Draw(frame)

        # Porcentagem animada no canto superior direito
        score_txt = f"{val:.1f}%" if (target_score % 1 and step == total_steps) else f"{round(val)}%"
        if step == total_steps:
            score_txt = f"{target_score:g}%"
        stb = fdraw.textbbox((0, 0), score_txt, font=font_score)
        fdraw.text((BAR_X + BAR_W - (stb[2] - stb[0]), 14), score_txt, font=font_score, fill=(255, 46, 126))

        _draw_progress_bar(frame, fdraw, grad1, BAR_X, BAR1_Y, BAR_W, BAR1_H, val, BG)
        _draw_progress_bar(frame, fdraw, grad2, BAR_X, BAR2_Y, BAR_W, BAR2_H, val, BG)

        frames.append(frame.convert("P", palette=Image.ADAPTIVE, colors=128))
        durations.append(6000 if step == total_steps else 55)

    buf = io.BytesIO()
    frames[0].save(
        buf,
        format="GIF",
        save_all=True,
        append_images=frames[1:],
        duration=durations,
        loop=0,
        optimize=True,
    )
    return buf.getvalue()


# ---------- Envio para o Discord (JSON ou Multipart com GIF) ----------

def _build_multipart(payload: dict, file_bytes: bytes, filename: str) -> tuple[bytes, str]:
    boundary = f"----PortwatchBoundary{uuid.uuid4().hex}"
    parts = [
        f"--{boundary}\r\n".encode("utf-8"),
        b'Content-Disposition: form-data; name="payload_json"\r\n',
        b"Content-Type: application/json; charset=utf-8\r\n\r\n",
        json.dumps(payload).encode("utf-8"),
        b"\r\n",
        f"--{boundary}\r\n".encode("utf-8"),
        f'Content-Disposition: form-data; name="files[0]"; filename="{filename}"\r\n'.encode("utf-8"),
        b"Content-Type: image/gif\r\n\r\n",
        file_bytes,
        b"\r\n",
        f"--{boundary}--\r\n".encode("utf-8"),
    ]
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


def send_discord_alert(
    webhook_url: str,
    title: str,
    ov: dict,
    previous_value: float | None,
    color: int,
) -> None:
    current_value = ov["score"]
    leader = ov["leader"]
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

    gif_bytes = None
    try:
        gif_bytes = generate_progress_gif(ov)
    except Exception as exc:
        print(f"AVISO ao gerar GIF de progresso: {type(exc).__name__}", file=sys.stderr)

    embed = {
        "title": title,
        "description": description[:4000],
        "url": DISPLAY_URL,
        "color": color,
        "fields": fields,
    }
    if gif_bytes:
        embed["image"] = {"url": "attachment://progresso.gif"}
    if effort.get("assessedAt"):
        embed["footer"] = {"text": f"Revisado em {effort['assessedAt']}"}

    payload = {
        "username": "Portwatch VI Monitor",
        "embeds": [embed],
    }

    if gif_bytes:
        body, content_type = _build_multipart(payload, gif_bytes, "progresso.gif")
    else:
        body = json.dumps(payload).encode("utf-8")
        content_type = "application/json"

    req = urllib.request.Request(
        webhook_url,
        data=body,
        headers={
            "Content-Type": content_type,
            "User-Agent": USER_AGENT,
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
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
                ov,
                state["last_score"],
                0x3498DB,
            )
            print("Notificação de teste enviada no Discord (state.json não alterado).")
            return

        last_score = state["last_score"]

        if last_score is None:
            state["last_score"] = current
            state["last_leader"] = leader_id
            state["notified_90"] = current >= 90.0
            state["notified_100"] = current >= 100.0
            save_state(state)
            print(f"Estado inicial salvo em {current:g}% ({leader_name}).")
            return

        if current <= last_score:
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

        send_discord_alert(webhook_url, title, ov, last_score, color)
        state["last_score"] = current
        state["last_leader"] = leader_id
        save_state(state)
        print(f"Alerta de avanço enviado ({last_score:g}% -> {current:g}%).")
    except RuntimeError as exc:
        print(f"ERRO: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
