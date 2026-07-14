from __future__ import annotations

import json
import os
from dataclasses import fields
from datetime import datetime, timezone
import threading
import tkinter as tk
import sys
import webbrowser
from pathlib import Path
from tkinter import messagebox, ttk

from .dedupe import deterministic_idea_sort_key
from .digest import render_digest
from .models import ScoredIdea
from .runner import CrawlOptions, CrawlResult, _is_verified_real_time_item, run_crawl

BUNDLE_ROOT = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent))
if getattr(sys, "frozen", False):
    PROJECT_ROOT = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "ProductIdeaRadar"
else:
    PROJECT_ROOT = BUNDLE_ROOT
DEFAULT_CONFIG = BUNDLE_ROOT / "config" / "sources.json"
DEFAULT_OUT = PROJECT_ROOT / "runs" / "latest"
DEFAULT_DB = PROJECT_ROOT / "data" / "ideas.sqlite"
PROJECT_ROOT.mkdir(parents=True, exist_ok=True)
INSANE_SEARCH_CANDIDATES = [
    Path(os.environ.get("INSANE_SEARCH_DIR", "")) if os.environ.get("INSANE_SEARCH_DIR") else None,
    Path.home() / ".claude" / "skills" / "insane-search",
    Path.home() / ".gjc" / "skills" / "insane-search",
]

# ── Dark theme palette ──────────────────────────────────────────────
C = {
    "bg": "#0c0e14",
    "surface": "#141820",
    "surface2": "#1a1f2b",
    "border": "#252b3a",
    "border_hi": "#323a4f",
    "text": "#e8ecf4",
    "muted": "#7d869a",
    "accent": "#5b8def",
    "accent_hi": "#7aa4f5",
    "accent_dim": "#2a3f6e",
    "success": "#34d399",
    "warn": "#fbbf24",
    "danger": "#f87171",
    "log_bg": "#0a0c10",
}

FONT = "Segoe UI"
MONO = "Cascadia Mono"
SOURCE_LABELS = {
    "threads_social": "Threads Social (public web)",
    "threads": "Threads",
    "producthunt": "Product Hunt",
    "kickstarter": "Kickstarter",
    "reddit": "Reddit",
    "x_social": "X Social (API/public web)",
    "instagram_social": "Instagram Social (API/public web)",
    "x": "X / Twitter (legacy direct)",
    "instagram": "Instagram (legacy direct)",
}
DEFAULT_SOURCE_NAMES = ["threads_social", "producthunt", "kickstarter", "reddit", "x_social", "instagram_social", "x", "instagram"]
DEFAULT_ON_SOURCES = {"threads_social", "producthunt", "kickstarter", "reddit", "x_social", "instagram_social"}
GUI_DISPLAY_LIMIT = 30
COLLECTION_INTERVAL_MS = 60_000
EVIDENCE_RAW_FIELDS = [
    "physical_signal",
    "public_engagement",
    "purchase_evidence",
    "public_only",
    "discovery_method",
    "discovery_lane",
    "conformant",
    "is_live_real_time",
    "primary_purchase_evidence_id",
    "dedupe_key",
    "final_url",
    "resolver",
    "resolver_status",
    "provisional",
    "freshness_unknown",
    "evidence_type",
    "category_seed",
    "cache_source",
    "cache_item_id",
    "cache_reused_at",
    "fallback_reason",
]


def resolve_insane_search_dir() -> str | None:
    for candidate in INSANE_SEARCH_CANDIDATES:
        if candidate and (candidate / "engine" / "__main__.py").exists():
            return str(candidate)
    return None


def _source_label(item: ScoredIdea | object) -> str:
    raw_item = item.item if isinstance(item, ScoredIdea) else item
    platform = getattr(raw_item, "platform", "")
    source = getattr(raw_item, "source", "")
    platform_label = SOURCE_LABELS.get(platform, platform)
    source_label = SOURCE_LABELS.get(source, source)
    if source and source != platform:
        return source_label if source in SOURCE_LABELS else f"{platform_label} / {source}"
    return platform_label


def _evidence_lines(idea: ScoredIdea) -> list[str]:
    item = idea.item
    raw = item.raw or {}
    lines: list[str] = [
        f"Product name: {raw.get('product_name') or item.title}",
        f"Original post URL: {raw.get('original_post_url') or item.url}",
    ]
    image_url = raw.get("product_image_url") or (item.media_urls[0] if item.media_urls else "")
    if image_url:
        lines.append(f"Product image: {image_url}")
    purchase_url = _primary_purchase_url(raw)
    if purchase_url:
        lines.append(f"Purchase URL: {purchase_url}")
    metrics = dict(item.metrics or {})
    public_engagement = raw.get("public_engagement")
    if isinstance(public_engagement, dict):
        metrics.update(public_engagement)
    for key, label in [("views", "Views"), ("likes", "Likes"), ("replies", "Replies"), ("reposts", "Reposts")]:
        if metrics.get(key) is not None:
            lines.append(f"{label}: {metrics[key]}")
    affiliate = _primary_affiliate(raw)
    if affiliate:
        lines.append(f"Affiliate disclosure: {affiliate}")
    lines.append(f"Discovery lane: {raw.get('discovery_lane') or raw.get('discovery_method') or item.source}")
    lines.append(f"Conformance: {raw.get('conformant', idea.is_product_idea)}")
    for field in EVIDENCE_RAW_FIELDS:
        value = raw.get(field)
        if value is None or value == "":
            continue
        lines.append(f"{field}: {value}")
    lines.append(f"Resolver/dedupe evidence: resolver={raw.get('resolver') or raw.get('resolver_status')}; dedupe={raw.get('dedupe_key') or idea.duplicate_key}")
    return lines


def _primary_purchase_url(raw: dict) -> str:
    evidence = raw.get("purchase_evidence")
    if isinstance(evidence, list):
        for row in evidence:
            if isinstance(row, dict):
                resolver = row.get("resolver") if isinstance(row.get("resolver"), dict) else {}
                for key in ("final_url", "url", "raw_url", "value"):
                    value = resolver.get(key) if key == "final_url" else row.get(key)
                    if isinstance(value, str) and value:
                        return value
    value = raw.get("purchase_url") or raw.get("final_url")
    return value if isinstance(value, str) else ""


def _primary_affiliate(raw: dict) -> str:
    evidence = raw.get("purchase_evidence")
    if isinstance(evidence, list):
        for row in evidence:
            if isinstance(row, dict) and isinstance(row.get("affiliate"), dict):
                affiliate = row["affiliate"]
                disclosed = affiliate.get("disclosed")
                label = affiliate.get("label") or affiliate.get("network") or affiliate.get("source")
                return f"{label or 'affiliate'} (disclosed={disclosed})"
    affiliate = raw.get("affiliate")
    if isinstance(affiliate, dict):
        return str(affiliate)
    return str(affiliate) if affiliate else ""


def _parse_idea_time(idea: ScoredIdea) -> datetime:
    for value in (idea.item.published_at, idea.item.fetched_at):
        if not value:
            continue
        try:
            return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(timezone.utc)
        except ValueError:
            continue
    return datetime.min.replace(tzinfo=timezone.utc)
def _display_sort_key(idea: ScoredIdea) -> tuple[float, float, str, str]:
    timestamp, engagement, _score, stable_title, stable_id = deterministic_idea_sort_key(idea)
    return (timestamp, engagement, stable_title, stable_id)


def _uses_strict_schema(raw: dict) -> bool:
    strict_fields = {
        "is_live_real_time",
        "public_only",
        "conformant",
        "source_evidence_policy_passed",
        "purchase_evidence",
        "dedupe_key",
        "fixture",
        "is_fixture",
        "provisional",
        "degraded",
        "cache_source",
        "is_cache_fallback",
        "acquisition_mode",
        "evidence_type",
    }
    return any(field in raw for field in strict_fields)


def _is_display_eligible(idea: ScoredIdea) -> bool:
    raw = idea.item.raw if isinstance(idea.item.raw, dict) else {}
    if _uses_strict_schema(raw):
        return _is_verified_real_time_item(idea.item)
    return idea.is_product_idea


def _require_mapping(value: object, label: str) -> dict:
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be an object")
    return value


def _scored_idea_from_row(row: object, *, raw_fields: set[str], scored_fields: set[str]) -> ScoredIdea:
    from .models import RawItem

    row_data = _require_mapping(row, "idea row")
    if "item" in row_data:
        item_data = dict(_require_mapping(row_data.get("item"), "idea row.item"))
        scored_source = {key: value for key, value in row_data.items() if key != "item"}
    else:
        item_data = {key: value for key, value in row_data.items() if key in raw_fields}
        scored_source = row_data

    raw_payload = item_data.get("raw")
    if raw_payload is None:
        item_data["raw"] = {}
    elif not isinstance(raw_payload, dict):
        raise ValueError("idea row item.raw must be an object when present")

    item_defaults = {
        "id": str(item_data.get("id") or item_data.get("url") or item_data.get("title") or "unknown"),
        "source": str(item_data.get("source") or item_data.get("platform") or "unknown"),
        "platform": str(item_data.get("platform") or item_data.get("source") or "unknown"),
        "url": str(item_data.get("url") or ""),
        "title": str(item_data.get("title") or "Untitled"),
        "text": str(item_data.get("text") or scored_source.get("summary_ko") or ""),
    }
    item_defaults.update({key: value for key, value in item_data.items() if key in raw_fields and value is not None})
    if not isinstance(item_defaults.get("media_urls"), list):
        item_defaults["media_urls"] = []
    if not isinstance(item_defaults.get("metrics"), dict):
        item_defaults["metrics"] = {}
    if not isinstance(item_defaults.get("tags"), list):
        item_defaults["tags"] = []
    item = RawItem(**item_defaults)

    matched_terms = scored_source.get("matched_terms") or []
    scored_defaults = {
        "is_product_idea": bool(scored_source.get("is_product_idea", True)),
        "category": str(scored_source.get("category") or "unknown"),
        "summary_ko": str(scored_source.get("summary_ko") or item.text or item.title),
        "novelty_score": float(scored_source.get("novelty_score") or 0.0),
        "virality_score": float(scored_source.get("virality_score") or 0.0),
        "market_score": float(scored_source.get("market_score") or 0.0),
        "total_score": float(scored_source.get("total_score") or 0.0),
        "duplicate_key": str(scored_source.get("duplicate_key") or item.raw.get("dedupe_key") or item.url or item.id),
        "reason": str(scored_source.get("reason") or ""),
        "matched_terms": matched_terms if isinstance(matched_terms, list) else [str(matched_terms)],
    }
    return ScoredIdea(item=item, **scored_defaults)




def _latest_verified_ideas(ideas: list[ScoredIdea], *, limit: int = GUI_DISPLAY_LIMIT) -> list[ScoredIdea]:
    verified = [idea for idea in ideas if _is_display_eligible(idea)]
    return sorted(verified, key=_display_sort_key, reverse=True)[:limit]


def load_ideas_from_json(path: Path) -> list[ScoredIdea]:
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"{path} is not valid JSON: {exc}") from exc

    if isinstance(data, dict):
        if isinstance(data.get("ideas"), list):
            rows = data["ideas"]
        elif isinstance(data.get("items"), list):
            rows = data["items"]
        else:
            raise ValueError(f"{path} has unsupported JSON schema: expected a list, or an object with an 'ideas' or 'items' list")
    elif isinstance(data, list):
        rows = data
    else:
        raise ValueError(f"{path} has unsupported JSON schema: expected list/object, got {type(data).__name__}")

    ideas: list[ScoredIdea] = []
    from .models import RawItem

    raw_fields = {field.name for field in fields(RawItem)}
    scored_fields = {field.name for field in fields(ScoredIdea)}
    scored_fields.discard("item")
    for index, row in enumerate(rows, start=1):
        try:
            ideas.append(_scored_idea_from_row(row, raw_fields=raw_fields, scored_fields=scored_fields))
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{path} row {index} is unsupported: {exc}") from exc
    return ideas


def _score_tag(score: float) -> str:
    if score >= 5.0:
        return "score_high"
    if score >= 4.0:
        return "score_mid"
    return "score_low"


class IdeaRadarApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("Product Idea Radar")
        self.geometry("1240x800")
        self.minsize(1000, 680)
        self.configure(bg=C["bg"])

        self._ideas: list[ScoredIdea] = []
        self._displayed_ideas: list[ScoredIdea] = []
        self._crawl_thread: threading.Thread | None = None
        self._crawl_options: CrawlOptions | None = None
        self._repeat_after_id: str | None = None
        self._running = False
        self._cycle_active = False
        self._closing = False
        self._score_labels: list[tk.Label] = []

        self._apply_theme()
        self._build_ui()
        self._load_previous_results()
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    # ── Theme ───────────────────────────────────────────────────────

    def _apply_theme(self) -> None:
        style = ttk.Style(self)
        style.theme_use("clam")

        style.configure(".", background=C["bg"], foreground=C["text"], borderwidth=0)
        style.configure("TFrame", background=C["bg"])
        style.configure("Surface.TFrame", background=C["surface"])
        style.configure("TLabel", background=C["bg"], foreground=C["text"], font=(FONT, 10))
        style.configure("Muted.TLabel", background=C["bg"], foreground=C["muted"], font=(FONT, 9))
        style.configure("CardTitle.TLabel", background=C["surface"], foreground=C["muted"], font=(FONT, 9, "bold"))
        style.configure("Header.TLabel", background=C["bg"], foreground=C["text"], font=(FONT, 22, "bold"))
        style.configure("Sub.TLabel", background=C["bg"], foreground=C["muted"], font=(FONT, 10))

        style.configure(
            "TCheckbutton",
            background=C["surface"],
            foreground=C["text"],
            font=(FONT, 10),
            indicatorcolor=C["surface2"],
        )
        style.map(
            "TCheckbutton",
            background=[("active", C["surface"]), ("selected", C["surface"])],
            foreground=[("active", C["text"])],
        )

        style.configure(
            "TSpinbox",
            fieldbackground=C["surface2"],
            background=C["surface2"],
            foreground=C["text"],
            arrowcolor=C["muted"],
            bordercolor=C["border"],
            lightcolor=C["border"],
            darkcolor=C["border"],
            insertcolor=C["text"],
        )

        style.configure(
            "TEntry",
            fieldbackground=C["surface2"],
            foreground=C["text"],
            bordercolor=C["border"],
            lightcolor=C["border"],
            darkcolor=C["border"],
            insertcolor=C["text"],
        )

        style.configure(
            "Accent.TButton",
            background=C["accent"],
            foreground="#ffffff",
            font=(FONT, 10, "bold"),
            padding=(16, 10),
            borderwidth=0,
            focusthickness=0,
        )
        style.map(
            "Accent.TButton",
            background=[("active", C["accent_hi"]), ("disabled", C["border_hi"])],
            foreground=[("disabled", C["muted"])],
        )

        style.configure(
            "Ghost.TButton",
            background=C["surface2"],
            foreground=C["text"],
            font=(FONT, 9),
            padding=(12, 8),
            borderwidth=0,
        )
        style.map("Ghost.TButton", background=[("active", C["border_hi"])])

        style.configure(
            "Treeview",
            background=C["surface"],
            foreground=C["text"],
            fieldbackground=C["surface"],
            borderwidth=0,
            rowheight=32,
            font=(FONT, 10),
        )
        style.configure(
            "Treeview.Heading",
            background=C["surface2"],
            foreground=C["muted"],
            font=(FONT, 9, "bold"),
            borderwidth=0,
            relief="flat",
        )
        style.map(
            "Treeview",
            background=[("selected", C["accent_dim"])],
            foreground=[("selected", C["text"])],
        )

        style.configure(
            "Vertical.TScrollbar",
            background=C["surface2"],
            troughcolor=C["surface"],
            borderwidth=0,
            arrowcolor=C["muted"],
        )
        style.map("Vertical.TScrollbar", background=[("active", C["border_hi"])])

        style.configure("TPanedwindow", background=C["bg"])

    # ── UI helpers ──────────────────────────────────────────────────

    def _card(self, parent: tk.Widget, title: str, *, expand: bool = False) -> tk.Frame:
        outer = tk.Frame(parent, bg=C["border"], padx=1, pady=1)
        pack_kw: dict = {"fill": tk.X}
        if expand:
            pack_kw = {"fill": tk.BOTH, "expand": True}
        outer.pack(**pack_kw)

        inner = tk.Frame(outer, bg=C["surface"])
        inner.pack(fill=tk.BOTH, expand=True)

        header = tk.Frame(inner, bg=C["surface"])
        header.pack(fill=tk.X, padx=14, pady=(12, 0))
        tk.Label(
            header,
            text=title.upper(),
            bg=C["surface"],
            fg=C["muted"],
            font=(FONT, 8, "bold"),
        ).pack(anchor=tk.W)

        body = tk.Frame(inner, bg=C["surface"])
        body.pack(fill=tk.BOTH, expand=True, padx=14, pady=(8, 14))
        return body

    def _dark_text(self, parent: tk.Widget, *, height: int = 8, mono: bool = False) -> tk.Text:
        font = (MONO, 9) if mono else (FONT, 10)
        text = tk.Text(
            parent,
            height=height,
            wrap=tk.WORD,
            font=font,
            bg=C["log_bg"] if mono else C["surface2"],
            fg=C["text"],
            insertbackground=C["accent"],
            selectbackground=C["accent_dim"],
            selectforeground=C["text"],
            relief="flat",
            padx=10,
            pady=10,
            borderwidth=0,
            highlightthickness=1,
            highlightbackground=C["border"],
            highlightcolor=C["accent"],
        )
        return text

    def _add_spinbox(
        self,
        parent: tk.Widget,
        label: str,
        var: tk.Variable,
        from_: float,
        to: float,
        *,
        increment: float = 1.0,
    ) -> None:
        row = tk.Frame(parent, bg=C["surface"])
        row.pack(fill=tk.X, pady=3)
        tk.Label(row, text=label, bg=C["surface"], fg=C["muted"], font=(FONT, 9)).pack(side=tk.LEFT)
        ttk.Spinbox(row, from_=from_, to=to, increment=increment, textvariable=var, width=7).pack(side=tk.RIGHT)

    def _score_chip(self, parent: tk.Widget, label: str, color: str) -> tk.Label:
        chip = tk.Label(
            parent,
            text=f"{label}  —",
            bg=C["surface2"],
            fg=color,
            font=(FONT, 9, "bold"),
            padx=10,
            pady=4,
        )
        chip.pack(side=tk.LEFT, padx=(0, 6))
        self._score_labels.append(chip)
        return chip

    # ── Build UI ────────────────────────────────────────────────────

    def _build_ui(self) -> None:
        root = tk.Frame(self, bg=C["bg"])
        root.pack(fill=tk.BOTH, expand=True, padx=20, pady=16)

        # Header
        header = tk.Frame(root, bg=C["bg"])
        header.pack(fill=tk.X, pady=(0, 16))

        accent_bar = tk.Frame(header, bg=C["accent"], height=3)
        accent_bar.pack(fill=tk.X, pady=(0, 12))

        title_row = tk.Frame(header, bg=C["bg"])
        title_row.pack(fill=tk.X)
        tk.Label(
            title_row,
            text="Product Idea Radar",
            bg=C["bg"],
            fg=C["text"],
            font=(FONT, 22, "bold"),
        ).pack(side=tk.LEFT)
        self.status_dot = tk.Label(title_row, text="●", bg=C["bg"], fg=C["success"], font=(FONT, 10))
        self.status_dot.pack(side=tk.RIGHT, padx=(8, 0))
        self.status_var = tk.StringVar(value="준비됨")
        tk.Label(
            title_row,
            textvariable=self.status_var,
            bg=C["bg"],
            fg=C["muted"],
            font=(FONT, 10),
        ).pack(side=tk.RIGHT)

        tk.Label(
            header,
            text="공개 소스에서 제품 아이디어를 수집하고 점수화합니다",
            bg=C["bg"],
            fg=C["muted"],
            font=(FONT, 10),
        ).pack(anchor=tk.W)

        # Body
        body = ttk.Panedwindow(root, orient=tk.HORIZONTAL)
        body.pack(fill=tk.BOTH, expand=True)

        left = tk.Frame(body, bg=C["bg"], width=300)
        body.add(left, weight=0)

        # Sources card
        sources_body = self._card(left, "수집 소스")
        self.source_vars: dict[str, tk.BooleanVar] = {}
        for name in DEFAULT_SOURCE_NAMES:
            var = tk.BooleanVar(value=name in DEFAULT_ON_SOURCES)
            self.source_vars[name] = var
            row = tk.Frame(sources_body, bg=C["surface"])
            row.pack(fill=tk.X, pady=2)
            ttk.Checkbutton(row, text=SOURCE_LABELS.get(name, name), variable=var).pack(anchor=tk.W)

        # Settings card
        settings_body = self._card(left, "설정")
        self.limit_var = tk.IntVar(value=10)
        self.top_var = tk.IntVar(value=GUI_DISPLAY_LIMIT)
        self.threshold_var = tk.DoubleVar(value=4.7)
        self._add_spinbox(settings_body, "소스당 수집", self.limit_var, 1, 100)
        tk.Label(settings_body, text=f"검증 표시: 최신 {GUI_DISPLAY_LIMIT}건 고정", bg=C["surface"], fg=C["muted"], font=(FONT, 9)).pack(anchor=tk.W, pady=3)
        self._add_spinbox(settings_body, "점수 기준", self.threshold_var, 0.0, 10.0, increment=0.1)

        # Actions
        actions = tk.Frame(left, bg=C["bg"])
        actions.pack(fill=tk.X, pady=10)
        self.start_btn = ttk.Button(actions, text="▶  수집 시작", style="Accent.TButton", command=self._start_crawl)
        self.start_btn.pack(fill=tk.X)
        self.stop_btn = ttk.Button(actions, text="■  수집 중지", style="Ghost.TButton", command=self._stop_crawl, state=tk.DISABLED)
        self.stop_btn.pack(fill=tk.X, pady=(6, 0))
        btn_row = tk.Frame(actions, bg=C["bg"])
        btn_row.pack(fill=tk.X, pady=(6, 0))
        ttk.Button(btn_row, text="결과 폴더", style="Ghost.TButton", command=self._open_output_dir).pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 3))
        ttk.Button(btn_row, text="다이제스트", style="Ghost.TButton", command=self._save_digest).pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(3, 0))

        # Log card
        log_body = self._card(left, "진행 로그", expand=True)
        self.log_text = self._dark_text(log_body, height=10, mono=True)
        self.log_text.pack(fill=tk.BOTH, expand=True)
        self.log_text.configure(state=tk.DISABLED)

        # Right panel
        right = tk.Frame(body, bg=C["bg"])
        body.add(right, weight=1)

        # Table card
        table_body = self._card(right, "아이디어 목록", expand=True)
        table_inner = tk.Frame(table_body, bg=C["surface"])
        table_inner.pack(fill=tk.BOTH, expand=True)

        columns = ("rank", "score", "category", "platform", "source", "title")
        self.tree = ttk.Treeview(table_inner, columns=columns, show="headings")
        self.tree.heading("rank", text="#")
        self.tree.heading("score", text="점수")
        self.tree.heading("category", text="분류")
        self.tree.heading("platform", text="플랫폼")
        self.tree.heading("source", text="소스")
        self.tree.heading("title", text="제목")
        self.tree.column("rank", width=36, anchor=tk.CENTER, stretch=False)
        self.tree.column("score", width=56, anchor=tk.CENTER, stretch=False)
        self.tree.column("category", width=88, stretch=False)
        self.tree.column("platform", width=120, stretch=False)
        self.tree.column("source", width=128, stretch=False)
        self.tree.column("title", width=360, stretch=True)
        self.tree.tag_configure("score_high", foreground=C["success"])
        self.tree.tag_configure("score_mid", foreground=C["warn"])
        self.tree.tag_configure("score_low", foreground=C["muted"])
        self.tree.tag_configure("odd", background=C["surface"])
        self.tree.tag_configure("even", background=C["surface2"])
        self.tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self.tree.bind("<<TreeviewSelect>>", self._on_select)

        scroll = ttk.Scrollbar(table_inner, orient=tk.VERTICAL, command=self.tree.yview)
        scroll.pack(side=tk.RIGHT, fill=tk.Y)
        self.tree.configure(yscrollcommand=scroll.set)

        # Detail card
        detail_body = self._card(right, "상세 정보")
        self.detail_title = tk.Label(
            detail_body,
            text="목록에서 아이디어를 선택하세요",
            bg=C["surface"],
            fg=C["text"],
            font=(FONT, 12, "bold"),
            wraplength=700,
            justify=tk.LEFT,
        )
        self.detail_title.pack(anchor=tk.W)

        chips_row = tk.Frame(detail_body, bg=C["surface"])
        chips_row.pack(anchor=tk.W, pady=(8, 0))
        self._score_labels.clear()
        self._chip_total = self._score_chip(chips_row, "종합", C["accent_hi"])
        self._chip_novelty = self._score_chip(chips_row, "참신함", C["success"])
        self._chip_virality = self._score_chip(chips_row, "바이럴", C["warn"])
        self._chip_market = self._score_chip(chips_row, "시장성", C["accent_hi"])

        meta_row = tk.Frame(detail_body, bg=C["surface"])
        meta_row.pack(anchor=tk.W, pady=(6, 0))
        self.detail_category = tk.Label(meta_row, text="", bg=C["surface"], fg=C["muted"], font=(FONT, 9))
        self.detail_category.pack(side=tk.LEFT)
        self.detail_platform = tk.Label(
            meta_row,
            text="",
            bg=C["accent_dim"],
            fg=C["accent_hi"],
            font=(FONT, 8, "bold"),
            padx=8,
            pady=2,
        )
        self.detail_platform.pack(side=tk.LEFT, padx=(8, 0))

        self.detail_summary = self._dark_text(detail_body, height=4)
        self.detail_summary.pack(fill=tk.X, pady=(10, 0))
        self.detail_summary.configure(state=tk.DISABLED)

        link_row = tk.Frame(detail_body, bg=C["surface"])
        link_row.pack(fill=tk.X, pady=(8, 0))
        self.link_var = tk.StringVar()
        link_entry = ttk.Entry(link_row, textvariable=self.link_var)
        link_entry.pack(side=tk.LEFT, fill=tk.X, expand=True)
        ttk.Button(link_row, text="열기 →", style="Ghost.TButton", command=self._open_link).pack(side=tk.LEFT, padx=(6, 0))

    # ── Logic ─────────────────────────────────────────────────────────

    def _append_log(self, message: str) -> None:
        self.log_text.configure(state=tk.NORMAL)
        tag = None
        if message.startswith("[오류]") or message.startswith("[실패]"):
            tag = "err"
        elif message.startswith("[완료]"):
            tag = "ok"
        if tag == "err" and "err" not in self.log_text.tag_names():
            self.log_text.tag_configure("err", foreground=C["danger"])
        if tag == "ok" and "ok" not in self.log_text.tag_names():
            self.log_text.tag_configure("ok", foreground=C["success"])
        self.log_text.insert(tk.END, message + "\n", tag)
        self.log_text.see(tk.END)
        self.log_text.configure(state=tk.DISABLED)

    def _set_status(self, message: str, *, running: bool = False) -> None:
        self.status_var.set(message)
        self.status_dot.configure(fg=C["warn"] if running else C["success"])

    def _selected_sources(self) -> list[str]:
        return [name for name, var in self.source_vars.items() if var.get()]

    def _start_crawl(self) -> None:
        if self._running or self._cycle_active or self._closing:
            return
        sources = self._selected_sources()
        if not sources:
            messagebox.showwarning("소스 없음", "최소 1개 이상의 소스를 선택하세요.")
            return

        insane_dir = resolve_insane_search_dir()
        if insane_dir is None:
            messagebox.showerror(
                "insane-search 없음",
                "insane-search 스킬을 찾을 수 없습니다.\n"
                "INSANE_SEARCH_DIR 환경변수를 설정하거나\n"
                "~/.claude/skills/insane-search 를 설치하세요.",
            )
            return

        self._crawl_options = CrawlOptions(
            config=str(DEFAULT_CONFIG),
            sources=sources,
            limit_per_source=self.limit_var.get(),
            top=GUI_DISPLAY_LIMIT,
            threshold=self.threshold_var.get(),
            out=str(DEFAULT_OUT),
            db=str(DEFAULT_DB),
            insane_search_dir=insane_dir,
        )
        self._running = True
        self.start_btn.configure(state=tk.DISABLED, text="연속 수집 중...")
        self.stop_btn.configure(state=tk.NORMAL)
        self._append_log("─── 연속 수집 시작 ───")
        self._begin_crawl_cycle()

    def _begin_crawl_cycle(self) -> None:
        self._repeat_after_id = None
        if not self._running or self._cycle_active or self._closing:
            return
        assert self._crawl_options is not None
        options = self._crawl_options
        self._cycle_active = True
        self._set_status("수집 중...", running=True)

        def worker() -> None:
            try:
                result = run_crawl(options, on_progress=lambda msg: self.after(0, self._append_log, msg))
                self.after(0, self._on_crawl_done, result, None)
            except Exception as exc:
                self.after(0, self._on_crawl_done, None, exc)

        self._crawl_thread = threading.Thread(target=worker, daemon=True)
        self._crawl_thread.start()

    def _stop_crawl(self) -> None:
        if not self._running and not self._cycle_active:
            return
        self._running = False
        if self._repeat_after_id is not None:
            self.after_cancel(self._repeat_after_id)
            self._repeat_after_id = None
        self.stop_btn.configure(state=tk.DISABLED)
        if self._cycle_active:
            self._append_log("─── 중지 요청: 현재 수집 완료를 기다립니다 ───")
            self._set_status("중지 중 — 현재 수집 완료 대기", running=True)
        else:
            self._finish_stopped()

    def _finish_stopped(self) -> None:
        self._crawl_options = None
        self.start_btn.configure(state=tk.NORMAL, text="▶  수집 시작")
        self.stop_btn.configure(state=tk.DISABLED)
        self._set_status("수집 중지됨")

    def _schedule_next_cycle(self, *, failed: bool = False) -> None:
        if not self._running or self._closing:
            self._finish_stopped()
            return
        seconds = COLLECTION_INTERVAL_MS // 1_000
        if failed:
            self._set_status(f"오류 — {seconds}초 후 재시도", running=True)
        else:
            self._set_status(f"대기 중 — {seconds}초 후 다시 수집", running=True)
        self._repeat_after_id = self.after(COLLECTION_INTERVAL_MS, self._begin_crawl_cycle)

    def _on_crawl_done(self, result: CrawlResult | None, error: Exception | None) -> None:
        self._cycle_active = False
        self._crawl_thread = None
        if error is not None:
            self._append_log(f"[실패] {error}")
        else:
            assert result is not None
            self._ideas = result.ideas
            self._populate_tree(result.ideas, top=GUI_DISPLAY_LIMIT)
            self._append_log(f"─── 완료: 아이디어 {len(result.ideas)}건 ───")
            if result.errors:
                self._append_log(f"[오류] 소스: {', '.join(result.errors)}")

        if self._closing:
            self.destroy()
            return
        if not self._running:
            self._finish_stopped()
            return
        self._schedule_next_cycle(failed=error is not None)

    def _on_close(self) -> None:
        if self._closing:
            return
        self._closing = True
        self._running = False
        if self._repeat_after_id is not None:
            self.after_cancel(self._repeat_after_id)
            self._repeat_after_id = None
        self.start_btn.configure(state=tk.DISABLED)
        self.stop_btn.configure(state=tk.DISABLED)
        if self._cycle_active:
            self._append_log("─── 앱 종료 요청: 현재 수집 완료를 기다립니다 ───")
            self._set_status("종료 중 — 현재 수집 완료 대기", running=True)
            return
        self.destroy()
    def _populate_tree(self, ideas: list[ScoredIdea], *, top: int) -> None:
        self.tree.delete(*self.tree.get_children())
        self._displayed_ideas = _latest_verified_ideas(ideas, limit=min(top, GUI_DISPLAY_LIMIT))
        for rank, idea in enumerate(self._displayed_ideas, start=1):
            tag = _score_tag(idea.total_score)
            stripe = "even" if rank % 2 == 0 else "odd"
            self.tree.insert(
                "",
                tk.END,
                iid=str(rank - 1),
                values=(
                    rank,
                    f"{idea.total_score:.2f}",
                    idea.category,
                    _source_label(idea),
                    idea.item.source,
                    idea.item.title[:100],
                ),
                tags=(tag, stripe),
            )

    def _on_select(self, _event: object | None = None) -> None:
        selected = self.tree.selection()
        if not selected:
            return
        idx = int(selected[0])
        if idx >= len(self._displayed_ideas):
            return
        idea = self._displayed_ideas[idx]
        item = idea.item

        self.detail_title.configure(text=item.title)
        self._chip_total.configure(text=f"종합  {idea.total_score:.2f}")
        self._chip_novelty.configure(text=f"참신함  {idea.novelty_score:.2f}")
        self._chip_virality.configure(text=f"바이럴  {idea.virality_score:.2f}")
        self._chip_market.configure(text=f"시장성  {idea.market_score:.2f}")
        self.detail_category.configure(text=f"분류 · {idea.category}")
        self.detail_platform.configure(text=_source_label(item))

        self.detail_summary.configure(state=tk.NORMAL)
        self.detail_summary.delete("1.0", tk.END)
        self.detail_summary.insert(tk.END, f"{idea.summary_ko}\n\n")
        self.detail_summary.insert(tk.END, f"이유: {idea.reason}")
        evidence = _evidence_lines(idea)
        if evidence:
            self.detail_summary.insert(tk.END, "\n\n증거 상태:\n")
            self.detail_summary.insert(tk.END, "\n".join(f"- {line}" for line in evidence))
        self.detail_summary.configure(state=tk.DISABLED)
        self.link_var.set(item.url)

    def _open_link(self) -> None:
        url = self.link_var.get().strip()
        if url:
            webbrowser.open(url)

    def _open_output_dir(self) -> None:
        path = DEFAULT_OUT
        path.mkdir(parents=True, exist_ok=True)
        os.startfile(path)  # type: ignore[attr-defined]

    def _save_digest(self) -> None:
        if not self._ideas:
            messagebox.showinfo("저장", "표시할 아이디어가 없습니다.")
            return
        DEFAULT_OUT.mkdir(parents=True, exist_ok=True)
        digest_path = DEFAULT_OUT / "digest.md"
        digest_path.write_text(render_digest(_latest_verified_ideas(self._ideas), top=GUI_DISPLAY_LIMIT), encoding="utf-8")
        messagebox.showinfo("저장 완료", f"다이제스트를 저장했습니다.\n{digest_path}")

    def _load_previous_results(self) -> None:
        ideas_path = DEFAULT_OUT / "ideas.json"
        try:
            ideas = load_ideas_from_json(ideas_path)
        except ValueError as exc:
            self._append_log(f"[오류] 이전 결과 진단: {exc}")
            self._set_status("이전 결과 스키마 오류")
            return
        except OSError as exc:
            self._append_log(f"[오류] 이전 결과 읽기 실패: {exc}")
            self._set_status("이전 결과 읽기 오류")
            return
        if not ideas:
            return
        self._ideas = ideas
        self._populate_tree(ideas, top=GUI_DISPLAY_LIMIT)
        self._set_status(f"이전 결과 {len(ideas)}건")


def main() -> int:
    os.chdir(PROJECT_ROOT)
    app = IdeaRadarApp()
    app.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
