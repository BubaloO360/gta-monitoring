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
from html.parser import HTMLParser
from pathlib import Path

try:
    from PIL import Image, ImageDraw, ImageFont
except ImportError:
    Image = ImageDraw = ImageFont = None

SITE_URL = "https://portwatch-vi.xyz/"
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
    ("loads", "Exe rodando", 10),
    ("boots", "Primeiro frame", 20),
    ("menu", "Menu principal", 30),
    ("world", "No mundo", 40),
    ("playable", "Jogável", 50),
    ("complete", "Completo", 100),
]
TICK = {key: at for key, _, at in MILESTONES}
MILESTONE_LABEL = {key: label for key, label, _ in MILESTONES}
MILESTONE_IDX = {key: idx for idx, (key, _, _) in enumerate(MILESTONES)}
LADDER = ["loads", "boots", "menu", "world", "playable"]

TRACKS = [
    ("story", "História", 0.35),
    ("render", "Renderização", 0.25),
    ("perf", "Desempenho", 0.20),
    ("stability", "Estabilidade", 0.20),
]

STORY_STEPS_PT = [
    (0.0, "Preso na abertura"),
    (0.1, "Prólogo concluído"),
    (0.25, "Primeiras missões"),
    (0.4, "Primeiro ato"),
    (0.6, "Metade da história"),
    (0.85, "História até os créditos"),
    (1.0, "100% concluído"),
]

RENDER_STEPS_PT = [
    (0.0, "Preto ou quebrado"),
    (0.25, "Formas sem iluminação"),
    (0.5, "Iluminado, sem efeitos"),
    (0.75, "Próximo ao console"),
    (1.0, "Igual ao console"),
]

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


def track_values(gta: dict) -> dict:
    try:
        fps_num = float(gta.get("fps") or 0)
    except (TypeError, ValueError):
        fps_num = 0.0
    return {
        "story": clamp01(gta.get("story")),
        "render": clamp01(gta.get("render")),
        "perf": clamp01(fps_num / 30.0),
        "stability": STABILITY.get(gta.get("stability"), STABILITY["unknown"]),
    }


def track_step_label(steps: list[tuple[float, str]], value: float) -> str:
    v = clamp01(value)
    for threshold, label in reversed(steps):
        if v >= threshold:
            return label
    return steps[0][1]


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

    tracks = track_values(gta)
    quality = sum(weight * tracks[key] for key, _, weight in TRACKS)
    if reached == "playable":
        score = TICK["playable"] + quality * (TICK["complete"] - TICK["playable"])
        if quality >= 1.0:
            reached = "complete"

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
        "tracks": tracks,
        "playable": reached in ("playable", "complete"),
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
        "last_pin_key": None,
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
            "Referer": SITE_URL,
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


# ---------- Leitura do quadro fixado (#pinned) ----------

class _PinnedHTMLParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.in_aside = False
        self.aside_depth = 0
        self.capture = None
        self.author_parts: list[str] = []
        self.text_parts: list[str] = []
        self.datetime_val = ""

    def handle_starttag(self, tag, attrs):
        attr_map = dict(attrs)
        classes = set((attr_map.get("class") or "").split())

        if not self.in_aside:
            if tag == "aside" and (attr_map.get("id") == "pinned" or "pinned" in classes):
                self.in_aside = True
                self.aside_depth = 1
            return

        self.aside_depth += 1
        if "pin-name" in classes:
            self.capture = "author"
        elif "pin-text" in classes:
            self.capture = "text"
        elif "pin-time" in classes and attr_map.get("datetime"):
            self.datetime_val = attr_map["datetime"].strip()
        elif tag == "br" and self.capture == "text":
            self.text_parts.append("\n")

    def handle_endtag(self, tag):
        if not self.in_aside:
            return
        if self.capture in ("author", "text") and tag in ("b", "p", "span", "div"):
            self.capture = None
        self.aside_depth -= 1
        if self.aside_depth <= 0:
            self.in_aside = False

    def handle_data(self, data):
        if self.capture == "author":
            self.author_parts.append(data)
        elif self.capture == "text":
            self.text_parts.append(data)


def extract_pinned_from_tracker(data: dict) -> dict | None:
    for key in ("pinned", "pin", "pinnedMessage", "notice", "announcement"):
        val = data.get(key)
        if not val:
            continue
        if isinstance(val, str) and val.strip():
            return {"author": "Criador", "datetime": "", "text": val.strip()}
        if isinstance(val, dict):
            text = (val.get("text") or val.get("message") or val.get("body") or "").strip()
            if text:
                author = (val.get("author") or val.get("name") or val.get("by") or "Criador").strip()
                dt = str(val.get("at") or val.get("datetime") or val.get("date") or val.get("time") or "").strip()
                return {"author": author, "datetime": dt, "text": text}
    return None


def fetch_pinned_from_html() -> dict | None:
    query = urllib.parse.urlencode({"ts": int(time.time() // 60)})
    req = urllib.request.Request(
        f"{SITE_URL}?{query}",
        headers={"User-Agent": USER_AGENT, "Accept": "text/html,*/*"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            html_body = resp.read().decode("utf-8", errors="replace")
    except Exception as exc:
        print(f"AVISO ao ler HTML do quadro fixado: {type(exc).__name__}", file=sys.stderr)
        return None

    parser = _PinnedHTMLParser()
    parser.feed(html_body)
    text = "".join(parser.text_parts).strip()
    if not text:
        return None
    author = "".join(parser.author_parts).strip() or "Criador"
    return {"author": author, "datetime": parser.datetime_val, "text": text}


def get_pinned_message(tracker_data: dict) -> dict | None:
    pinned = extract_pinned_from_tracker(tracker_data)
    if pinned:
        return pinned
    return fetch_pinned_from_html()


# ---------- Tradução PT-BR ----------

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


def _translate_single(text: str) -> str:
    providers = (
        ("MyMemory", _translate_mymemory),
        ("Google-clients5", _translate_google_clients5),
        ("Google-gtx", _translate_google_gtx),
    )
    for name, fn in providers:
        try:
            result = fn(text)
            if result:
                return result
        except Exception as exc:
            print(f"AVISO tradução ({name}): {type(exc).__name__}", file=sys.stderr)
    return text


def translate_ptbr(text: str) -> str:
    clean = (text or "").strip()
    if not clean:
        return ""
    # Preserva quebras de parágrafo (como no quadro fixado)
    paragraphs = [p.strip() for p in clean.split("\n\n") if p.strip()]
    if len(paragraphs) > 1:
        return "\n\n".join(_translate_single(p) for p in paragraphs)
    return _translate_single(clean)


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
        (0.50, (255, 46, 126)),   # #ff2e7e rosa
        (0.75, (255, 98, 102)),   # coral
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
        mdraw.rounded_rectangle([0, 0, fill_w, height], radius=min(radius, max(1, fill_w // 2)), fill=255)
        track_mask = Image.new("L", (width, height), 0)
        ImageDraw.Draw(track_mask).rounded_rectangle([0, 0, width, height], radius=radius, fill=255)
        combined = Image.composite(mask, Image.new("L", (width, height), 0), track_mask)
        bar_grad = grad_strip.resize((width, height)) if grad_strip.size != (width, height) else grad_strip
        img.paste(bar_grad, (x0, y0), combined)

    # Divisórias verticais em 10%, 20%, 30%, 40%, 50%
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
    dot_gap = 11
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

    tracks = leader.get("tracks") or {}
    quality = sum(weight * tracks.get(key, 0.0) for key, _, weight in TRACKS)
    complete_state = "reached" if leader["reached"] == "complete" else ("partial" if leader.get("playable") and quality > 0 else "unreached")

    return [
        (0, "Preparação", "reached" if past_gw else st(gw)),
        (10, "Exe rodando", st(ladder_value(gta.get("loads")))),
        (20, "Primeiro frame", st(ladder_value(gta.get("boots")))),
        (30, "Menu principal", st(ladder_value(gta.get("menu")))),
        (40, "No mundo", st(ladder_value(gta.get("world")))),
        (50, "Jogável", st(ladder_value(gta.get("playable")))),
        (100, "Completo", complete_state),
    ]


def generate_progress_gif(ov: dict) -> bytes | None:
    if Image is None:
        return None

    W, H = 1120, 415
    BG = (15, 16, 22)
    BAR_X, BAR_W = 55, 1010
    BAR1_Y, BAR1_H = 112, 24
    BAR2_Y, BAR2_H = 266, 20

    font_header = _load_font(13, bold=True)
    font_score = _load_font(18, bold=True)
    font_pct = _load_font(11, bold=True)
    font_label = _load_font(11, bold=True)
    font_pill = _load_font(12, bold=True)
    font_init = _load_font(12, bold=True)
    font_track = _load_font(11, bold=False)

    grad1 = _build_gradient_strip(BAR_W, BAR1_H)
    grad2 = _build_gradient_strip(BAR_W, BAR2_H)

    target_score = ov["score"]
    leader = ov["leader"]
    leader_name = leader["effort"].get("name", "Líder") if leader else "Nenhum"
    leader_label = leader["reached_label"] if leader else "-"
    furthest_idx = ov.get("furthest_idx", 0)
    proj_items = _project_milestone_items(leader)

    # Posicionamento sem sobreposição dos marcadores do Top 6
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

        outline_c = (255, 46, 126) if is_leader else (85, 92, 114)
        bdraw.ellipse(
            [ax - circle_r, circle_y - circle_r, ax + circle_r, circle_y + circle_r],
            fill=(28, 30, 42),
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
    sb = bdraw.textbbox((0, 0), leader_label, font=font_header)
    bdraw.text((BAR_X + BAR_W - (sb[2] - sb[0]), 228), leader_label, font=font_header, fill=(198, 204, 218))

    # Rótulos da Barra do Projeto
    for at, label, st in proj_items:
        mx = BAR_X + int(round((at / 100.0) * BAR_W))
        align = "left" if at == 0 else ("right" if at == 100 else "center")
        _draw_milestone_label(bdraw, mx, BAR2_Y + BAR2_H + 14, label, st, font_label, align)

    # 4 trilhas de qualidade (50% -> 100%) abaixo da barra do projeto
    if leader:
        gta = leader["effort"].get("gta") or {}
        tracks = leader.get("tracks") or {}
        fps_val = gta.get("fps")
        fps_str = f"{round(float(fps_val))} FPS" if fps_val else "0 FPS"
        stab_str = STABILITY_LABEL_PT.get(gta.get("stability") or "unknown", "Desconhecida")

        cards = [
            ("História (35%)", track_step_label(STORY_STEPS_PT, tracks.get("story", 0)), tracks.get("story", 0)),
            ("Render (25%)", track_step_label(RENDER_STEPS_PT, tracks.get("render", 0)), tracks.get("render", 0)),
            ("Desempenho (20%)", fps_str, tracks.get("perf", 0)),
            ("Estabilidade (20%)", stab_str, tracks.get("stability", 0)),
        ]
        card_gap = 14
        card_w = (BAR_W - card_gap * 3) // 4
        card_y = 334
        card_h = 62
        for i, (cname, cval, cfrac) in enumerate(cards):
            cx = BAR_X + i * (card_w + card_gap)
            bdraw.rounded_rectangle([cx, card_y, cx + card_w, card_y + card_h], radius=8, fill=(23, 25, 35), outline=(38, 42, 58), width=1)
            bdraw.text((cx + 12, card_y + 9), cname, font=font_pct, fill=(142, 148, 168))
            pct_s = f"{round(cfrac * 100)}%"
            pb = bdraw.textbbox((0, 0), pct_s, font=font_pct)
            bdraw.text((cx + card_w - 12 - (pb[2] - pb[0]), card_y + 9), pct_s, font=font_pct, fill=(255, 46, 126) if cfrac > 0 else (112, 118, 138))
            bdraw.text((cx + 12, card_y + 27), cval[:28], font=font_track, fill=(235, 238, 245))
            # Mini-barra dentro do card
            mx0, my0, mw, mh = cx + 12, card_y + 48, card_w - 24, 5
            bdraw.rounded_rectangle([mx0, my0, mx0 + mw, my0 + mh], radius=2, fill=(38, 42, 58))
            fw = int(round(mw * clamp01(cfrac)))
            if fw > 0:
                bdraw.rounded_rectangle([mx0, my0, mx0 + fw, my0 + mh], radius=2, fill=(255, 46, 126))

    # Quadros da animação
    total_steps = 18
    frames = []
    durations = []

    for step in range(total_steps + 1):
        p = step / total_steps
        eased = 1.0 - (1.0 - p) ** 3
        val = target_score * eased

        frame = base.copy()
        fdraw = ImageDraw.Draw(frame)

        score_txt = f"{target_score:g}%" if step == total_steps else f"{round(val)}%"
        stb = fdraw.textbbox((0, 0), score_txt, font=font_score)
        fdraw.text((BAR_X + BAR_W - (stb[2] - stb[0]), 14), score_txt, font=font_score, fill=(255, 46, 126))

        _draw_progress_bar(frame, fdraw, grad1, BAR_X, BAR1
