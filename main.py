# Pi server for EzpeR.
import os
import json
import threading
from io import BytesIO
import ebooklib
from ebooklib import epub
from bs4 import BeautifulSoup
from PIL import Image, ImageDraw, ImageFont, ImageEnhance
from flask import Flask, jsonify, request, Response
from zeroconf import Zeroconf, ServiceInfo
import socket

# ---------------- PATHS ----------------
BOOKS_DIR = "books"  # Dir where u drop epubs into
CONFIG_PATH = "config.json" # Dir to store settings
BITMAP_CACHE_DIR = "bitmap_cache"

# ---------------- DISPLAY DIMS ----------------
PAGE_WIDTH = 296
PAGE_HEIGHT = 128
BITMAP_BYTES = 4736 

# ---------------- RENDERING CONSTS ----------------
FONT_PATH_REGULAR     = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
FONT_PATH_BOLD        = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
FONT_PATH_ITALIC      = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Oblique.ttf"
FONT_PATH_BOLD_ITALIC = "/usr/share/fonts/truetype/dejavu/DejaVuSans-BoldOblique.ttf"
DEFAULT_BODY_FONT_SIZE = 12
DEFAULT_HEADER_FONT_SIZE = 16
DEFAULT_MARGIN_PX = 4
DEFAULT_LINE_SPACING_PX = 2
DEFAULT_DITHER_THRESHOLD_OFFSET = 0  # -255..255, biases dither darker/lighter
DITHER_THRESHOLD_STEP = 16  # per encoder tick from ESP32's ENCMODE_BRIGHTNESS
PARAGRAPH_GAP_PX = 4
# ---------------- SETTINGS MENU CONSTS ----------------
SETTINGS_MENU_TREE = {
    "root": {
        "title": "Settings",
        "items": [
            {"label": "Font Size", "type": "submenu", "target": "font_size"},
            {"label": "Brightness", "type": "submenu", "target": "brightness"},
            {"label": "Margins", "type": "submenu", "target": "margins"},
            {"label": "Default Bold", "type": "toggle", "key": "default_bold"},
        ],
    },
    "font_size": {
        "title": "Font Size",
        "items": [
            {"label": "Small (10pt)", "type": "set_value", "key": "body_font_size", "value": 10},
            {"label": "Medium (12pt)", "type": "set_value", "key": "body_font_size", "value": 12},
            {"label": "Large (14pt)", "type": "set_value", "key": "body_font_size", "value": 14},
        ],
    },
    "brightness": {
        "title": "Brightness",
        "items": [
            {"label": "Darker", "type": "adjust_value", "key": "dither_threshold_offset", "delta": -DITHER_THRESHOLD_STEP},
            {"label": "Lighter", "type": "adjust_value", "key": "dither_threshold_offset", "delta": DITHER_THRESHOLD_STEP},
            {"label": "Reset", "type": "set_value", "key": "dither_threshold_offset", "value": 0},
        ],
    },
    "margins": {
        "title": "Margins",
        "items": [
            {"label": "Tight (2px)", "type": "set_value", "key": "margin_px", "value": 2},
            {"label": "Normal (4px)", "type": "set_value", "key": "margin_px", "value": 4},
            {"label": "Wide (8px)", "type": "set_value", "key": "margin_px", "value": 8},
        ],
    },
}
SETTINGS_MENU_VISIBLE_ROWS = 7  # matches ESP32 renderBookBrowser's own 7-row assumption

# ---------------- NETWORK ----------------
FLASK_HOST = "0.0.0.0"
FLASK_PORT = 80 # Requires sudo
MDNS_SERVICE_NAME = "ebookserver"  # advertised as ebookserver.local

# ---------------- DEFAULT CONFIG (written to config.json on first run) ----------------
DEFAULT_CONFIG = {
    "body_font_size": DEFAULT_BODY_FONT_SIZE,
    "header_font_size": DEFAULT_HEADER_FONT_SIZE,
    "margin_px": DEFAULT_MARGIN_PX,
    "dither_threshold_offset": DEFAULT_DITHER_THRESHOLD_OFFSET,
    "default_bold": False,
    "button_map": {
        "up": "GPIO13", "down": "GPIO12", "left": "GPIO14", "right": "GPIO27",
        "center": "GPIO26", "enc1_push": "GPIO25", "enc2_push": "GPIO22",
    },
    "bookmarks": {},       # book_id -> {block_index, char_offset, page_number}
    "last_read": None,     # {book_id, page}
}

app = Flask(__name__)
config_lock = threading.Lock()
book_cache = {}            # book_id -> {blocks: [...], pages: [...] or None until paginated}
settings_menu_state = {"path": ["root"], "selected_idx": 0}

# ============================================================
# Config persistence
# ============================================================
def load_config():
    if not os.path.exists(CONFIG_PATH):
        save_config(DEFAULT_CONFIG)
        return dict(DEFAULT_CONFIG)
    try:
        with open(CONFIG_PATH, "r") as f:
            cfg = json.load(f)
    except (json.JSONDecodeError, ValueError):
        save_config(DEFAULT_CONFIG)
        return dict(DEFAULT_CONFIG)
    for key, val in DEFAULT_CONFIG.items():
        cfg.setdefault(key, val)
    return cfg

def save_config(cfg):
    with open(CONFIG_PATH, "w") as f:
        json.dump(cfg, f, indent=2)

def get_config():
    with config_lock:
        return load_config()

def update_config(patch_fn):
    with config_lock:
        cfg = load_config()
        patch_fn(cfg)
        save_config(cfg)
        return cfg


# ============================================================
# EPUB parsing -> flat block list (heading / paragraph w/ runs / image), document order preserved
# ============================================================
def parse_epub_blocks(epub_path):
    book = epub.read_epub(epub_path)
    blocks = []
    for spine_id, _ in book.spine:
        item = book.get_item_with_id(spine_id)
        if item is None or item.get_type() != ebooklib.ITEM_DOCUMENT:
            continue
        if item.get_name() == "nav.xhtml" or (hasattr(item, "is_chapter") and not item.is_chapter()):
            continue  # skip auto-generated TOC/nav doc, not real book content
        soup = BeautifulSoup(item.get_content(), "html.parser")
        for el in soup.find_all(["h1", "h2", "h3", "h4", "p", "img"]):
            if el.name == "img":
                src = el.get("src")
                if src:
                    blocks.append({"type": "image", "src": src})
            elif el.name in ("h1", "h2", "h3", "h4"):
                text = el.get_text().strip()
                if text:
                    blocks.append({"type": "heading", "runs": [{"text": text, "bold": True, "italic": False}]})
            else:
                runs = extract_runs(el)
                if runs:
                    blocks.append({"type": "paragraph", "runs": runs})
    return blocks


def extract_runs(el):
    from bs4.element import NavigableString
    runs = []
    for node in el.descendants:
        if not isinstance(node, NavigableString):
            continue
        if node.find_parent(["script", "style"]):
            continue
        text = str(node).strip()
        if not text:
            continue
        bold = node.find_parent(["b", "strong"]) is not None
        italic = node.find_parent(["i", "em"]) is not None
        underline = node.find_parent(["u"]) is not None
        runs.append({"text": text, "bold": bold, "italic": italic, "underline": underline})
    return runs


def get_book_image_bytes(epub_path, src):
    book = epub.read_epub(epub_path)
    for item in book.get_items_of_type(ebooklib.ITEM_IMAGE):
        if item.get_name().endswith(src) or src.endswith(item.get_name()):
            return item.get_content()
    return None


# ============================================================
# Font loading
# ============================================================
def load_font(size, bold=False, italic=False):
    if bold and italic:
        path = FONT_PATH_BOLD_ITALIC
    elif bold:
        path = FONT_PATH_BOLD
    elif italic:
        path = FONT_PATH_ITALIC
    else:
        path = FONT_PATH_REGULAR
    try:
        return ImageFont.truetype(path, size)
    except OSError:
        return ImageFont.load_default()


# ============================================================
# Pagination: render-and-measure. Walks blocks, lays out into 296x128 canvases,
# records (block_index, char_offset) at each page boundary for deterministic re-fetch.
# ============================================================
def paginate_blocks(blocks, epub_path, cfg):
    pages = []
    block_idx = 0
    char_offset = 0

    while block_idx < len(blocks):
        page_blocks, block_idx, char_offset = layout_one_page(blocks, block_idx, char_offset, epub_path, cfg)
        pages.append({"start_block": page_blocks["start_block"], "start_offset": page_blocks["start_offset"],
                       "render_ops": page_blocks["render_ops"]})
        if block_idx >= len(blocks) and char_offset == 0:
            break

    return pages


def layout_one_page(blocks, start_block_idx, start_char_offset, epub_path, cfg):
    margin = cfg["margin_px"]
    body_size = cfg["body_font_size"]
    header_size = cfg["header_font_size"]
    max_width = PAGE_WIDTH - 2 * margin
    max_height = PAGE_HEIGHT - 2 * margin

    canvas = Image.new("L", (PAGE_WIDTH, PAGE_HEIGHT), 255)
    draw = ImageDraw.Draw(canvas)

    y = margin
    block_idx = start_block_idx
    char_offset = start_char_offset
    render_ops = []  # recorded so the real render pass doesn't have to redo layout math

    while block_idx < len(blocks):
        block = blocks[block_idx]

        if block["type"] == "image":
            img_bytes = get_book_image_bytes(epub_path, block["src"])
            if not img_bytes:
                block_idx += 1
                char_offset = 0
                continue
            try:
                im = Image.open(BytesIO(img_bytes))
            except Exception:
                block_idx += 1
                char_offset = 0
                continue
            scale = min(max_width / im.width, (max_height - (y - margin)) / im.height if im.height else 1)
            scale = min(scale, 1.0)
            draw_w, draw_h = max(1, int(im.width * scale)), max(1, int(im.height * scale))
            if y + draw_h > PAGE_HEIGHT - margin:
                if y == margin:
                    block_idx += 1
                    char_offset = 0
                break
            render_ops.append({"op": "image", "src": block["src"], "x": margin, "y": y, "w": draw_w, "h": draw_h})
            y += draw_h + PARAGRAPH_GAP_PX
            block_idx += 1
            char_offset = 0
            continue

        size = header_size if block["type"] == "heading" else body_size
        lines, consumed_runs_state, fits_fully = wrap_runs_to_height(
            block["runs"], char_offset, size, max_width, max_height - (y - margin), draw, cfg
        )

        if not lines:
            if y == margin:
                # Single run too tall even for an empty page — force at least one word to avoid infinite loop
                first_run = block["runs"][0]
                lines = [[{"text": first_run["text"][:1], "bold": first_run["bold"], "italic": first_run["italic"]}]]
                consumed_runs_state = 1
                fits_fully = False
            else:
                break

        for line_words in lines:
            render_ops.append({
                "op": "text_runs", "words": line_words, "x": margin, "y": y, "size": size,
                "default_bold": cfg["default_bold"],
            })
            line_h = load_font(size).getbbox("Ay")[3] + DEFAULT_LINE_SPACING_PX
            y += line_h

        if fits_fully:
            block_idx += 1
            char_offset = 0
        else:
            char_offset = consumed_runs_state
            break

        y += PARAGRAPH_GAP_PX if block["type"] == "paragraph" else 2

    return ({"start_block": start_block_idx, "start_offset": start_char_offset, "render_ops": render_ops},
            block_idx, char_offset)


def flatten_runs_to_words(runs):
    """Each run's text split into words, each word tagged with that run's bold/italic. Flat list, run boundaries lost (fine -- wrapping only needs per-word style)."""
    words = []
    for run in runs:
        for w in run["text"].split():
            words.append({"text": w, "bold": run["bold"], "italic": run["italic"]})
    return words


def wrap_runs_to_height(runs, start_word_idx, font_size, max_width, max_height, draw, cfg):
    all_words = flatten_runs_to_words(runs)
    words = all_words[start_word_idx:]
    if not words:
        return [], start_word_idx, True

    lines = []
    current_line = []
    current_line_width = 0
    used_height = 0
    word_idx = start_word_idx
    space_width = draw.textbbox((0, 0), " ", font=load_font(font_size))[2]

    for word in words:
        font = load_font(font_size, bold=word["bold"] or cfg["default_bold"], italic=word["italic"])
        line_h = font.getbbox("Ay")[3] + DEFAULT_LINE_SPACING_PX
        word_width = draw.textbbox((0, 0), word["text"], font=font)[2]
        extra = (space_width if current_line else 0) + word_width

        if current_line_width + extra <= max_width:
            current_line.append(word)
            current_line_width += extra
            word_idx += 1
        else:
            if used_height + line_h > max_height:
                return lines, word_idx, False
            lines.append(current_line)
            used_height += line_h
            current_line = [word]
            current_line_width = word_width
            word_idx += 1

    if current_line:
        line_h = load_font(font_size).getbbox("Ay")[3] + DEFAULT_LINE_SPACING_PX
        if used_height + line_h > max_height:
            return lines, word_idx - len(current_line), False
        lines.append(current_line)

    fits_fully = (word_idx >= len(all_words))
    return lines, word_idx, fits_fully


# ============================================================
# Final render pass: takes recorded render_ops, produces the actual 1-bit dithered bitmap
# ============================================================
def render_page_image(render_ops, epub_path, cfg):
    canvas = Image.new("L", (PAGE_WIDTH, PAGE_HEIGHT), 255)
    draw = ImageDraw.Draw(canvas)

    for op in render_ops:
        if op["op"] == "text_runs":
            x = op["x"]
            space_font = load_font(op["size"])
            space_width = draw.textbbox((0, 0), " ", font=space_font)[2]
            for word in op["words"]:
                font = load_font(op["size"], bold=word["bold"] or op["default_bold"], italic=word["italic"])
                draw.text((x, op["y"]), word["text"], font=font, fill=0)
                word_width = draw.textbbox((0, 0), word["text"], font=font)[2]
                x += word_width + space_width
        elif op["op"] == "image":
            img_bytes = get_book_image_bytes(epub_path, op["src"])
            if img_bytes:
                im = Image.open(BytesIO(img_bytes)).convert("L").resize((op["w"], op["h"]), Image.Resampling.LANCZOS)
                canvas.paste(im, (op["x"], op["y"]))
    canvas = apply_brightness(canvas, cfg["dither_threshold_offset"])
    bw = canvas.convert("1")  # Floyd-Steinberg dither, default in PIL
    return bw


def apply_brightness(canvas_l_mode, threshold_offset):
    if threshold_offset == 0:
        return canvas_l_mode
    factor = 1.0 + (threshold_offset / 255.0)
    factor = max(0.3, min(2.0, factor))
    return ImageEnhance.Contrast(ImageEnhance.Brightness(canvas_l_mode).enhance(factor)).enhance(1.0)


def pack_bitmap(pil_image_1bit):
    pil_image_1bit = pil_image_1bit.resize((PAGE_WIDTH, PAGE_HEIGHT)) if pil_image_1bit.size != (PAGE_WIDTH, PAGE_HEIGHT) else pil_image_1bit
    px = pil_image_1bit.load()
    out = bytearray(BITMAP_BYTES)
    bytes_per_row = (PAGE_WIDTH + 7) // 8
    for row in range(PAGE_HEIGHT):
        for col in range(PAGE_WIDTH):
            pil_val = px[col, row]  # 0=black, 255=white in PIL '1' mode
            bit = 1 if pil_val == 0 else 0  # 1=black per GxEPD_BLACK convention
            if bit:
                byte_idx = row * bytes_per_row + (col // 8)
                out[byte_idx] |= (0x80 >> (col % 8))
    return bytes(out)

def bitmap_cache_path(book_id, page_num, cfg):
    # cache key includes render-affecting settings so changing font size etc. invalidates old cache
    sig = "%d_%d_%d_%d_%d" % (
        cfg["body_font_size"], cfg["header_font_size"],
        cfg["margin_px"], int(cfg["default_bold"]),
        cfg["dither_threshold_offset"]
    )
    safe_id = book_id.replace("/", "_").replace(" ", "_")
    return os.path.join(BITMAP_CACHE_DIR, f"{safe_id}__p{page_num}__{sig}.bin")


def load_bitmap_cache(book_id, page_num, cfg):
    path = bitmap_cache_path(book_id, page_num, cfg)
    if os.path.exists(path):
        with open(path, "rb") as f:
            data = f.read()
        if len(data) == BITMAP_BYTES:
            return data
    return None


def save_bitmap_cache(book_id, page_num, cfg, bitmap_bytes):
    os.makedirs(BITMAP_CACHE_DIR, exist_ok=True)
    path = bitmap_cache_path(book_id, page_num, cfg)
    with open(path, "wb") as f:
        f.write(bitmap_bytes)

# ============================================================
# Book cache — parses + paginates on first access, re-paginates if render-affecting settings change
# ============================================================
def get_book_path(book_id):
    return os.path.join(BOOKS_DIR, f"{book_id}.epub")


def get_book_title_author(epub_path):
    try:
        book = epub.read_epub(epub_path)
        title = book.get_metadata("DC", "title")
        author = book.get_metadata("DC", "creator")
        return (title[0][0] if title else os.path.basename(epub_path)), (author[0][0] if author else "Unknown")
    except Exception:
        return os.path.basename(epub_path), "Unknown"


def ensure_book_loaded(book_id, cfg):
    if book_id in book_cache and book_cache[book_id]["cfg_signature"] == render_signature(cfg):
        return book_cache[book_id]
    epub_path = get_book_path(book_id)
    if not os.path.exists(epub_path):
        return None
    blocks = parse_epub_blocks(epub_path)
    book_cache[book_id] = {
        "blocks": blocks,
        "pages": [],
        "epub_path": epub_path,
        "cfg_signature": render_signature(cfg),
        "paginator_state": {"block_idx": 0, "char_offset": 0, "exhausted": False}
    }
    return book_cache[book_id]

def render_signature(cfg):
    return (cfg["body_font_size"], cfg["header_font_size"], cfg["margin_px"], cfg["default_bold"])


def ensure_page_exists(book_data, page_num, cfg):
    while len(book_data["pages"]) <= page_num and not book_data["paginator_state"]["exhausted"]:
        ps = book_data["paginator_state"]
        blocks = book_data["blocks"]
        if ps["block_idx"] >= len(blocks):
            ps["exhausted"] = True
            break
        page_data, new_block_idx, new_char_offset = layout_one_page(
            blocks, ps["block_idx"], ps["char_offset"], book_data["epub_path"], cfg
        )
        book_data["pages"].append({
            "start_block": page_data["start_block"],
            "start_offset": page_data["start_offset"],
            "render_ops": page_data["render_ops"]
        })
        ps["block_idx"] = new_block_idx
        ps["char_offset"] = new_char_offset
        if new_block_idx >= len(blocks):
            ps["exhausted"] = True


def render_book_page(book_id, page_num, cfg):
    # cache hit — skip all rendering entirely
    cached = load_bitmap_cache(book_id, page_num, cfg)
    if cached:
        return cached

    book_data = ensure_book_loaded(book_id, cfg)
    if not book_data:
        return None
    ensure_page_exists(book_data, page_num, cfg)
    pages = book_data["pages"]
    if page_num < 0 or page_num >= len(pages):
        return None
    bw = render_page_image(pages[page_num]["render_ops"], book_data["epub_path"], cfg)
    bitmap = pack_bitmap(bw)
    save_bitmap_cache(book_id, page_num, cfg, bitmap)
    return bitmap

# ============================================================
# Settings menu rendering — same Pillow pipeline as book pages, per spec addendum
# ============================================================
def render_settings_menu():
    state = settings_menu_state
    node_key = state["path"][-1]
    node = SETTINGS_MENU_TREE[node_key]
    canvas = Image.new("L", (PAGE_WIDTH, PAGE_HEIGHT), 255)
    draw = ImageDraw.Draw(canvas)
    font = load_font(11)
    title_font = load_font(13, bold=True)

    draw.text((4, 2), node["title"], font=title_font, fill=0)
    y = 22
    cfg = get_config()
    for i, item in enumerate(node["items"][:SETTINGS_MENU_VISIBLE_ROWS]):
        prefix = "> " if i == state["selected_idx"] else "  "
        label = item["label"]
        if item["type"] == "toggle":
            label += f" [{'ON' if cfg.get(item['key']) else 'OFF'}]"
        draw.text((4, y), prefix + label, font=font, fill=0)
        y += 14

    bw = canvas.convert("1")
    return pack_bitmap(bw)


def apply_settings_menu_action(event):
    state = settings_menu_state
    node = SETTINGS_MENU_TREE[state["path"][-1]]
    items = node["items"]

    if event == "up":
        state["selected_idx"] = max(0, state["selected_idx"] - 1)
    elif event == "down":
        state["selected_idx"] = min(len(items) - 1, state["selected_idx"] + 1)
    elif event == "select":
        item = items[state["selected_idx"]]
        if item["type"] == "submenu":
            state["path"].append(item["target"])
            state["selected_idx"] = 0
        elif item["type"] == "toggle":
            update_config(lambda cfg: cfg.__setitem__(item["key"], not cfg.get(item["key"], False)))
        elif item["type"] == "set_value":
            update_config(lambda cfg: cfg.__setitem__(item["key"], item["value"]))
        elif item["type"] == "adjust_value":
            update_config(lambda cfg: cfg.__setitem__(item["key"], cfg.get(item["key"], 0) + item["delta"]))
    elif event == "back":
        if len(state["path"]) > 1:
            state["path"].pop()
            state["selected_idx"] = 0


# ============================================================
# mDNS advertisement
# ============================================================
def advertise_mdns():
    zc = Zeroconf()
    hostname = socket.gethostname()
    local_ip = socket.gethostbyname(hostname)
    info = ServiceInfo(
        "_http._tcp.local.",
        f"{MDNS_SERVICE_NAME}._http._tcp.local.",
        addresses=[socket.inet_aton(local_ip)],
        port=FLASK_PORT,
        server=f"{MDNS_SERVICE_NAME}.local.",
    )
    zc.register_service(info)
    return zc


# ============================================================
# Flask routes — mirrors exactly what main.cpp calls
# ============================================================
@app.route("/books", methods=["GET"])
def list_books():
    if not os.path.isdir(BOOKS_DIR):
        return jsonify([])
    result = []
    for fname in sorted(os.listdir(BOOKS_DIR), key=str.lower):
        if not fname.endswith(".epub"):
            continue
        book_id = fname[:-len(".epub")]
        title, author = get_book_title_author(os.path.join(BOOKS_DIR, fname))
        result.append({"id": book_id, "title": title, "author": author})
    return jsonify(result)


@app.route("/last_read", methods=["GET"])
def last_read():
    cfg = get_config()
    lr = cfg.get("last_read")
    if not lr:
        return jsonify({"error": "no last read book"}), 404
    return jsonify(lr)


@app.route("/books/<book_id>/page/<int:page_num>", methods=["GET"])
def get_page(book_id, page_num):
    cfg = get_config()
    bitmap = render_book_page(book_id, page_num, cfg)
    if bitmap is None:
        return jsonify({"error": "page or book not found"}), 404
    update_config(lambda c: c.__setitem__("last_read", {"book_id": book_id, "page": page_num}))
    return Response(bitmap, mimetype="application/octet-stream")


@app.route("/books/<book_id>/page/<int:page_num>/next", methods=["GET"])
def get_next_page(book_id, page_num):
    return get_page(book_id, page_num + 1)


@app.route("/books/<book_id>/page/<int:page_num>/prev", methods=["GET"])
def get_prev_page(book_id, page_num):
    return get_page(book_id, max(0, page_num - 1))


@app.route("/books/<book_id>/bookmark", methods=["POST"])
def save_bookmark(book_id):
    data = request.get_json(force=True, silent=True) or {}
    update_config(lambda c: c["bookmarks"].__setitem__(book_id, data))
    return jsonify({"ok": True})


@app.route("/menu/settings", methods=["GET"])
def get_settings_menu():
    settings_menu_state["path"] = ["root"]
    settings_menu_state["selected_idx"] = 0
    return Response(render_settings_menu(), mimetype="application/octet-stream")


@app.route("/menu/settings/nav", methods=["POST"])
def settings_menu_nav():
    data = request.get_json(force=True, silent=True) or {}
    event = data.get("event")
    if event in ("up", "down", "select", "back"):
        apply_settings_menu_action(event)
    return Response(render_settings_menu(), mimetype="application/octet-stream")


@app.route("/settings/<key>", methods=["POST"])
def set_setting(key):
    data = request.get_json(force=True, silent=True) or {}
    value = data.get("value")
    update_config(lambda c: c.__setitem__(key, value))
    return jsonify({"ok": True})


@app.route("/settings/dither_threshold_delta", methods=["POST"])
def adjust_dither_threshold():
    data = request.get_json(force=True, silent=True) or {}
    step = data.get("value", 0) * DITHER_THRESHOLD_STEP
    update_config(lambda c: c.__setitem__("dither_threshold_offset", c.get("dither_threshold_offset", 0) + step))
    return jsonify({"ok": True})


@app.route("/settings/button_map", methods=["GET"])
def get_button_map():
    cfg = get_config()
    return jsonify(cfg["button_map"])

# Main
def main():
    os.makedirs(BOOKS_DIR, exist_ok=True)    
    os.makedirs(BITMAP_CACHE_DIR, exist_ok=True) 
    if not os.path.exists(CONFIG_PATH):
        save_config(DEFAULT_CONFIG)
    zc = advertise_mdns()
    try:
        app.run(host=FLASK_HOST, port=FLASK_PORT, threaded=True)
    finally:
        zc.close()

if __name__ == "__main__":
    main()
