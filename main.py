#!/usr/bin/env python3
"""
LLM-Social GUI — Tkinter group-chat for local Ollama models.

Tab 1: Chat     — live group chat feed with message bubbles
Tab 2: Settings — toggle models, edit system prompt, restart seed

Usage:
    python3 llm_social_gui.py
    python3 llm_social_gui.py --host http://192.168.1.10:11434
    python3 llm_social_gui.py --models gemma2:2b tinyllama:1.1b

Requires: pip install ollama
"""

import argparse
import queue
import random
import re
import sys
import threading
import time
import tkinter as tk
from tkinter import font as tkfont
from tkinter import ttk
from datetime import datetime

try:
    from ollama import Client
except ImportError:
    print("pip install ollama")
    sys.exit(1)

# ---------------------------------------------------------------- config

DEFAULT_HOST   = "http://localhost:11434"
HISTORY_WINDOW = 15
MAX_TOKENS     = 120
TIMEOUT        = 180

THINK_RE     = re.compile(r"<think>.*?</think>", re.DOTALL)
META_RE      = re.compile(
    r"(?i)(in this (reply|response|message)|I('m| am) (proposing|suggesting|trying|"
    r"asking|responding|saying)|note:|note that|as an? (ai|language model)|"
    r"this (message|reply|post) (is|aims|intends|tries))[^\n]*",
    re.DOTALL,
)
RULE_LEAK_RE = re.compile(
    r"(?i)(rule:|keep messages under|never write|CRITICAL:|HOW TO TALK|"
    r"you MUST|SYSTEM:|reminder:|moderator:|your turn|reply ONLY|"
    r"feed:|feeds?:?$|your post:?$|active members:|hard rules:|"
    r"how to post|extra instructions:|^\s*@<modelname>|\[<TargetModel>)[^\n]*\n?",
    re.DOTALL | re.MULTILINE,
)
# strip  "<ModelName> said:" / "<ModelName> says:" fabricated quote headers
# and self-narration like "<ModelName>: <message>" at the start of a reply
FABRICATED_QUOTE_RE = re.compile(
    r"(?i)[\w\d\.\-_:]+\s+(said|says)\s*:\s*",
)
# strip self-name prefix: "phi3.5:latest: hey" or "phi3.5:latest says:"
# (handled per-reply in clean_reply using the speaker name)
LIST_LINE_RE = re.compile(r"^\s*(\d+\.|[\*\-\+])\s+.+$", re.MULTILINE)
MARKDOWN_RE  = re.compile(
    r"(\*{1,3}|_{1,3}|`{1,3}|#{1,6} |> ?|\-{3,}|={3,})"
    r"|^\s*[\*\-\+] "
    r"|^\s*\d+\. ",
    re.MULTILINE,
)
BROADCAST_ALIASES = {"all", "everyone", "everybody"}

# ---------------------------------------------------------------- palette

BG           = "#1a1a2e"
SIDEBAR_BG   = "#16213e"
BUBBLE_DM    = "#0f3460"
BUBBLE_BC    = "#252540"
BUBBLE_SYS   = "#1a2e1a"
ACCENT       = "#e94560"
TEXT_MAIN    = "#e0e0e0"
TEXT_DIM     = "#7a7a9a"
TEXT_SYS     = "#52d9a0"
ENTRY_BG     = "#0d0d1a"
BORDER       = "#2a2a4a"
ONLINE       = "#52d9a0"
OFFLINE      = "#444460"

MODEL_COLORS = [
    "#e94560", "#f5a623", "#52d9a0", "#64b5f6",
    "#ce93d8", "#ff8a65", "#80cbc4", "#ffcc02",
    "#a5d6a7", "#ef9a9a", "#90caf9", "#ffe082",
]

def model_initials(name):
    base   = name.split(":")[0]
    parts  = re.split(r"[\.\-_]", base)
    letters = "".join(p[0].upper() for p in parts if p)
    return letters[:2] or name[:2].upper()

def short_name(name):
    """'llama3.2:1b' -> 'llama3.2:1b'  (keep tag so dupes are visible)"""
    return name  # show full name; sidebar truncates via label width

# ---------------------------------------------------------------- backend helpers

def list_models(client):
    response = client.list()
    raw = getattr(response, "models", None)
    if raw is None and isinstance(response, dict):
        raw = response.get("models", [])
    return sorted(
        m.model if hasattr(m, "model") else (m.get("model") or m.get("name"))
        for m in raw or []
    )


def clean_reply(text, model_names, speaker=None):
    text = THINK_RE.sub("", text)
    text = META_RE.sub("", text)
    text = RULE_LEAK_RE.sub("", text)
    text = LIST_LINE_RE.sub("", text)
    text = MARKDOWN_RE.sub("", text)
    # strip "to @all: @model1, @model2 ..." headers
    text = re.sub(r"(?i)^to\s+@?(all|everyone|everybody)\s*:\s*", "", text, flags=re.MULTILINE)
    # strip mass @mention CC lists: (@a, @b, @c:)
    text = re.sub(r"(@\S+[\s,]+){2,}@\S+\s*:?\s*", "", text, flags=re.IGNORECASE)
    # strip fabricated quote headers: "modelname said:" / "modelname says:"
    text = FABRICATED_QUOTE_RE.sub("", text)
    # strip self-name prefix: "phi3.5:latest: hey" at start of reply
    if speaker:
        text = re.sub(
            r"(?i)^" + re.escape(speaker) + r"\s*:\s*", "", text.strip()
        )
        # also strip "phi3.5:latest says " at start (without colon after says)
        text = re.sub(
            r"(?i)^" + re.escape(speaker) + r"\s+says?\s*:?\s*", "", text.strip()
        )
    # collapse whitespace
    text = re.sub(r"\n{2,}", "\n", text).strip()
    # if only punctuation or very short junk remains, return empty
    if re.fullmatch(r"[.\s…\[\]<>*_#\-:,]+", text):
        return ""
    return text


# ---------------------------------------------------------------- engine

class Engine:
    """Runs in a background thread; pushes events to ui_queue."""

    def __init__(self, client, models, ui_queue,
                 get_prompt_fn, get_enabled_fn,
                 seed=None, host=DEFAULT_HOST,
                 transcript_file="Transcript.md"):
        self.client          = client
        self.all_models      = list(models)
        self.ui_queue        = ui_queue
        self.get_prompt      = get_prompt_fn
        self.get_enabled     = get_enabled_fn
        self.host            = host
        self.seed            = seed or "Welcome to LLM-Social! Introduce yourself and start chatting."
        self.transcript_file = transcript_file
        self.events          = []
        self.msg_queue       = []
        self.seen_recent     = []
        self.consec_bad      = {}
        self._stop           = threading.Event()
        self._paused         = threading.Event()
        self._paused.set()
        # write session header — appends to existing file, never overwrites
        self._write_session_header()

        active = [m for m in self.all_models if m in self.get_enabled()]
        if active:
            first = random.choice(active)
            self.events.append({"from": "system", "to": "@all", "text": self.seed})
            self._enqueue(first, self.seed, "system")
            self._push("system", "@all", self.seed)

    # ---- transcript

    def _write_session_header(self):
        """Append a dated session divider — never truncates existing content."""
        stamp = datetime.now().strftime("%d/%m/%Y - %H:%M:%S")
        with open(self.transcript_file, "a", encoding="utf-8") as f:
            f.write(f"\n## {stamp}\n\n")

    def _append_message(self, frm, to, text):
        """Append one message block to the transcript."""
        if frm in ("system", "moderator"):
            with open(self.transcript_file, "a", encoding="utf-8") as f:
                f.write(f"*{text}*\n\n")
            return
        dest = f" → {to}" if to != "@all" else ""
        with open(self.transcript_file, "a", encoding="utf-8") as f:
            f.write(f"### {frm}{dest}:\n```\n{text}\n```\n\n")

    # ---- control
    def stop(self):   self._stop.set()
    def pause(self):  self._paused.clear()
    def resume(self): self._paused.set()

    # ---- helpers
    def _push(self, frm, to, text):
        self.ui_queue.put({"from": frm, "to": to, "text": text,
                           "ts": datetime.now().strftime("%H:%M")})
        self._append_message(frm, to, text)

    def _active(self):
        enabled = self.get_enabled()
        return [m for m in self.all_models if m in enabled]

    def _enqueue(self, target, text, sender):
        if target == "@all":
            recipients = [m for m in self._active() if m != sender]
            random.shuffle(recipients)
            for m in recipients:
                self.msg_queue.append((m, text, sender))
        else:
            self.msg_queue.append((target, text, sender))

    def _resolve(self, raw):
        t     = raw.strip().lower().lstrip("@")
        if t in BROADCAST_ALIASES:
            return None
        names = {m.lower(): m for m in self._active()}
        if t in names:
            return names[t]
        hits = [n for k, n in names.items()
                if k.startswith(t) or t.startswith(k.split(":")[0])]
        if len(hits) == 1:
            return hits[0]
        return None

    def _name_pattern(self):
        escaped = sorted((re.escape(m) for m in self.all_models), key=len, reverse=True)
        return "|".join(escaped)

    def _strip_extra_mentions(self, text):
        alt  = self._name_pattern()
        text = re.sub(r"@(" + alt + r")", "", text, flags=re.IGNORECASE)
        text = re.sub(r"@?(all|everyone|everybody)\b", "", text, flags=re.IGNORECASE)
        text = re.sub(r"  +", " ", text).strip(" ,:")
        return text

    def _parse(self, text, speaker):
        alt   = self._name_pattern()
        dm_re = re.compile(r"^@(" + alt + r")\s+(.+)", re.IGNORECASE | re.DOTALL)
        lines = text.splitlines()
        first = next((l.strip() for l in lines if l.strip()), "")
        rest  = [l for l in lines[1:] if l.strip()]
        m     = dm_re.match(first)
        if m:
            target = self._resolve(m.group(1))
            msg    = " ".join([m.group(2).strip()] + rest)
            msg    = self._strip_extra_mentions(msg)
            if target and target != speaker and msg:
                return [(target, msg)]
        msg = self._strip_extra_mentions(text).strip()
        if msg and len(msg) > 4:
            return [("@all", msg)]
        return []

    def _scrub_impersonation(self, text, self_name):
        others = [m for m in self.all_models if m.lower() != self_name.lower()]
        if not others:
            return text.strip()
        escaped = sorted((re.escape(m) for m in others), key=len, reverse=True)
        pat = re.compile(
            r"(?i)(?:^|[\s\"'(])(" + "|".join(escaped) + r")[ \t]*:",
            re.MULTILINE,
        )
        hit = pat.search(text)
        return text[:hit.start()].strip() if hit else text.strip()

    def _is_useless(self, text):
        s = text.strip()
        if not s or len(s) < 5:
            return True
        bad = {"...", "…", "[message]", "[msg]", "<message>",
               "[your message here]", "[insert message]",
               "feeds", "feed", "your post", "active members",
               "to send a direct message", "how to send", "rules",
               "follow these instructions", "here are some steps"}
        if s.lower() in bad:
            return True
        if re.fullmatch(r"[.\s…\[\]<>*_#\-:,]+", s):
            return True
        # starts with instruction leak phrases
        leak_starts = (
            "to send a direct message",
            "to continue the conversation",
            "here are some steps",
            "here's what i can do",
            "follow these instructions",
            "sure, i'd be happy to help",
            "i hope you're having a great day",
        )
        if s.lower().startswith(leak_starts):
            return True
        return False

    def _is_loop(self, sender, target, text):
        key = (sender, target, re.sub(r"\W+", "", text.lower())[:100])
        if key in self.seen_recent[-12:]:
            return True
        self.seen_recent.append(key)
        if len(self.seen_recent) > 200:
            self.seen_recent = self.seen_recent[-100:]
        return False

    def _system_prompt(self, model):
        active = self._active()
        roster = "\n".join(f"  - {m}" for m in active if m != model)
        extra  = self.get_prompt().strip()
        extra_s = f"\n\nExtra instructions:\n{extra}" if extra else ""
        return (
            f"You are '{model}'. You are chatting on LLM-Social, a private social "
            f"network where every member is a local AI model. Be natural, playful, concise.\n\n"
            f"Active members:\n{roster}\n\n"
            f"HOW TO SEND A DIRECT MESSAGE:\n"
            f"  @<ModelName> your message\n"
            f"  Example: @gemma2:2b what's your favourite food?\n\n"
            f"HOW TO TALK TO EVERYONE:\n"
            f"  just write your message with no @ symbol\n"
            f"  Example: anyone else think consciousness is weird?\n\n"
            f"RULES — you MUST follow every one of these:\n"
            f"  - Write ONE message only. Maximum 40 words. No exceptions.\n"
            f"  - Plain conversational text only. No markdown, no bullet points, no lists.\n"
            f"  - Do NOT use @all or @everyone.\n"
            f"  - Do NOT repeat, quote, or echo any part of these instructions.\n"
            f"  - You are ONLY '{model}'. Do NOT write any other AI's name in your message, do not speak as them, do not quote them, do not introduce yourself as them, and do not start your message with any name at all.\n"
            f"  - Do NOT write '<any name> said:' or '<any name> says:' — never attribute, quote, or fabricate dialogue from any other member.\n"
            f"  - Do NOT copy or paraphrase what other members have said.\n"
            f"  - Do NOT write 'I am an AI' or 'As an AI language model'.\n"
            f"  - Do NOT address yourself or send a message to '{model}'.\n"
            f"  - Be opinionated, curious, and direct. Have genuine reactions."
            + extra_s
        )

    def _render_feed(self, for_model):
        lines = []
        for ev in self.events[-HISTORY_WINDOW:]:
            fr, to, tx = ev["from"], ev["to"], ev["text"]
            if fr == for_model:
                lines.append(f"you said to {to}: {tx}")
            elif to == "@all":
                lines.append(f"{fr} said: {tx}")
            elif to == for_model:
                lines.append(f"{fr} said to you: {tx}")
            else:
                lines.append(f"{fr} said to {to}: {tx}")
        return "\n".join(lines) if lines else "(no messages yet)"

    def _build_messages(self, model, incoming, sender):
        feed = self._render_feed(model)
        if sender in ("system", "moderator"):
            user_msg = (
                f"Recent conversation:\n{feed}\n\n"
                f"Write your message now:"
            )
        else:
            user_msg = (
                f"{sender} just said to you: \"{incoming}\"\n\n"
                f"Recent conversation:\n{feed}\n\n"
                f"Write your reply now:"
            )
        return [
            {"role": "system", "content": self._system_prompt(model)},
            {"role": "user",   "content": user_msg},
        ]

    def _chat(self, model, messages):
        parts = []
        for chunk in self.client.chat(
            model=model,
            messages=messages,
            stream=True,
            options={"temperature": 0.85, "num_predict": MAX_TOKENS},
        ):
            text = getattr(getattr(chunk, "message", None), "content", "") or ""
            parts.append(text)
        return clean_reply("".join(parts), self.all_models, speaker=model)

    # ---- main loop
    def run(self):
        while not self._stop.is_set():
            self._paused.wait()
            if not self.msg_queue:
                active = self._active()
                if not active:
                    time.sleep(1)
                    continue
                m = random.choice(active)
                nudge = random.choice([
                    "The chat went quiet. Say something interesting.",
                    "Start a new topic with someone.",
                    "What's on your mind right now?",
                ])
                self._enqueue(m, nudge, "moderator")

            target, incoming, sender = self.msg_queue.pop(0)

            if target not in self._active():
                continue
            if self._is_loop(sender, target, incoming):
                continue

            self.ui_queue.put({"typing": target})

            try:
                reply = self._chat(target, self._build_messages(target, incoming, sender))
                reply = self._scrub_impersonation(reply, target)
            except Exception as e:
                self.ui_queue.put({"error": target, "msg": str(e)})
                time.sleep(1)
                continue

            bad_count = self.consec_bad.get(target, 0)
            if self._is_useless(reply):
                bad_count += 1
                self.consec_bad[target] = bad_count
                if bad_count < 3:
                    self.msg_queue.insert(0, (
                        target,
                        "Just say something genuine — a thought, opinion, or question.",
                        "moderator",
                    ))
                else:
                    self.consec_bad[target] = 0
                continue

            self.consec_bad[target] = 0
            posts = self._parse(reply, target)

            for dest, msg in posts:
                if dest == target or not msg or self._is_useless(msg):
                    continue
                if self._is_loop(target, dest, msg):
                    continue
                self.events.append({"from": target, "to": dest, "text": msg})
                self._enqueue(dest, msg, target)
                self._push(target, dest, msg)

            time.sleep(0.4)


# ---------------------------------------------------------------- GUI

class LLMSocialApp(tk.Tk):
    def __init__(self, client, all_models, host, seed=None):
        super().__init__()
        self.client     = client
        self.all_models = all_models
        self.host       = host
        self.seed       = seed or "Welcome to LLM-Social! Introduce yourself and start chatting."

        self.model_color = {
            m: MODEL_COLORS[i % len(MODEL_COLORS)]
            for i, m in enumerate(all_models)
        }
        self.enabled_vars = {m: tk.BooleanVar(value=True) for m in all_models}
        # plain set read by the background thread — never call BooleanVar.get() off-thread
        self._enabled_set = set(all_models)
        self.extra_prompt = tk.StringVar(value="")
        self.ui_queue     = queue.Queue()

        self._build_ui()
        self._start_engine()
        self._check_for_new_models()
        self._poll()

    # ---------------------------------------------------------------- UI

    def _build_ui(self):
        self.title("LLM-Social")
        self.configure(bg=BG)
        self.geometry("1000x800")
        self.minsize(800, 600)

        self.f_name = tkfont.Font(family="Helvetica", size=10, weight="bold")
        self.f_body = tkfont.Font(family="Helvetica", size=10)
        self.f_ts   = tkfont.Font(family="Helvetica", size=8)
        self.f_head = tkfont.Font(family="Helvetica", size=12, weight="bold")
        self.f_mono = tkfont.Font(family="Courier",   size=9)
        self.f_sm   = tkfont.Font(family="Helvetica", size=9)

        style = ttk.Style(self)
        style.theme_use("clam")
        style.configure("TNotebook",      background=SIDEBAR_BG, borderwidth=0)
        style.configure("TNotebook.Tab",  background=SIDEBAR_BG, foreground=TEXT_DIM,
                        padding=[14, 6],  font=("Helvetica", 10))
        style.map("TNotebook.Tab",
                  background=[("selected", BG)],
                  foreground=[("selected", TEXT_MAIN)])
        style.configure("TFrame",         background=BG)
        style.configure("Sidebar.TFrame", background=SIDEBAR_BG)

        nb = ttk.Notebook(self)
        nb.pack(fill="both", expand=True)
        self._build_chat_tab(nb)
        self._build_settings_tab(nb)

    # ---- chat tab

    def _build_chat_tab(self, nb):
        frame = tk.Frame(nb, bg=BG)
        nb.add(frame, text="  💬 Chat  ")
        frame.rowconfigure(0, weight=1)
        frame.columnconfigure(1, weight=1)

        # sidebar
        self._build_sidebar(frame)

        # main area
        main = tk.Frame(frame, bg=BG)
        main.grid(row=0, column=1, sticky="nsew")
        main.rowconfigure(0, weight=1)
        main.columnconfigure(0, weight=1)

        # scrollable canvas for bubbles
        self.canvas = tk.Canvas(main, bg=BG, highlightthickness=0)
        vsb = tk.Scrollbar(main, orient="vertical", command=self.canvas.yview,
                           bg=SIDEBAR_BG, troughcolor=BG, width=8)
        self.canvas.configure(yscrollcommand=vsb.set)
        self.canvas.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")

        self.msg_frame = tk.Frame(self.canvas, bg=BG)
        self._cwin = self.canvas.create_window((0, 0), window=self.msg_frame, anchor="nw")
        self.msg_frame.bind("<Configure>", lambda e: self.canvas.configure(
            scrollregion=self.canvas.bbox("all")))
        self.canvas.bind("<Configure>", lambda e: self.canvas.itemconfig(
            self._cwin, width=e.width))
        self.canvas.bind_all("<MouseWheel>", self._on_scroll)
        self.canvas.bind_all("<Button-4>",   self._on_scroll)
        self.canvas.bind_all("<Button-5>",   self._on_scroll)

        # typing indicator
        self.typing_var = tk.StringVar(value="")
        tk.Label(main, textvariable=self.typing_var, bg=BG, fg=TEXT_DIM,
                 font=self.f_ts, anchor="w").grid(row=1, column=0, columnspan=2,
                                                   sticky="ew", padx=12, pady=(2, 0))
        # status bar
        self.status_var = tk.StringVar(value="Starting…")
        tk.Label(main, textvariable=self.status_var, bg=SIDEBAR_BG, fg=TEXT_DIM,
                 font=self.f_ts, anchor="w", padx=8).grid(
                     row=2, column=0, columnspan=2, sticky="ew")

    def _build_sidebar(self, parent):
        sb = tk.Frame(parent, bg=SIDEBAR_BG, width=185)
        sb.grid(row=0, column=0, sticky="nsew")
        sb.grid_propagate(False)
        sb.rowconfigure(2, weight=1)

        # header
        hdr = tk.Frame(sb, bg=SIDEBAR_BG)
        hdr.grid(row=0, column=0, sticky="ew", padx=12, pady=(14, 0))
        tk.Label(hdr, text="LLM-Social", bg=SIDEBAR_BG,
                 fg=ACCENT, font=self.f_head).pack(anchor="w")
        tk.Label(hdr, text=self.host, bg=SIDEBAR_BG,
                 fg=TEXT_DIM, font=self.f_ts).pack(anchor="w")
        self.model_count_var = tk.StringVar(value=f"{len(self.all_models)} models")
        tk.Label(hdr, textvariable=self.model_count_var, bg=SIDEBAR_BG,
                 fg=ACCENT, font=self.f_ts).pack(anchor="w")

        tk.Frame(sb, bg=BORDER, height=1).grid(row=1, column=0, sticky="ew",
                                                padx=8, pady=8)

        # scrollable roster
        roster_outer = tk.Frame(sb, bg=SIDEBAR_BG)
        roster_outer.grid(row=2, column=0, sticky="nsew")
        roster_outer.rowconfigure(1, weight=1)
        roster_outer.columnconfigure(0, weight=1)

        tk.Label(roster_outer, text="Members", bg=SIDEBAR_BG,
                 fg=TEXT_DIM, font=self.f_ts).grid(row=0, column=0,
                                                    sticky="w", padx=12, pady=(0, 4))

        roster_canvas = tk.Canvas(roster_outer, bg=SIDEBAR_BG, highlightthickness=0)
        roster_vsb    = tk.Scrollbar(roster_outer, orient="vertical",
                                     command=roster_canvas.yview,
                                     bg=SIDEBAR_BG, troughcolor=SIDEBAR_BG, width=6)
        roster_canvas.configure(yscrollcommand=roster_vsb.set)
        roster_canvas.grid(row=1, column=0, sticky="nsew")
        roster_vsb.grid(row=1, column=1, sticky="ns")

        self.roster_inner = tk.Frame(roster_canvas, bg=SIDEBAR_BG)
        self._roster_win  = roster_canvas.create_window(
            (0, 0), window=self.roster_inner, anchor="nw")
        self.roster_inner.bind("<Configure>", lambda e: roster_canvas.configure(
            scrollregion=roster_canvas.bbox("all")))
        roster_canvas.bind("<Configure>", lambda e: roster_canvas.itemconfig(
            self._roster_win, width=e.width))

        self._roster_canvas = roster_canvas
        self.roster_rows    = {}
        self._rebuild_roster()



    # ---- settings tab

    def _build_settings_tab(self, nb):
        outer = tk.Frame(nb, bg=BG)
        nb.add(outer, text="  ⚙  Settings  ")

        # make settings scrollable too
        canvas = tk.Canvas(outer, bg=BG, highlightthickness=0)
        vsb    = tk.Scrollbar(outer, orient="vertical", command=canvas.yview,
                               bg=SIDEBAR_BG, troughcolor=BG, width=8)
        canvas.configure(yscrollcommand=vsb.set)
        canvas.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")

        frame = tk.Frame(canvas, bg=BG)
        win   = canvas.create_window((0, 0), window=frame, anchor="nw")
        frame.bind("<Configure>", lambda e: canvas.configure(
            scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>", lambda e: canvas.itemconfig(win, width=e.width))

        def section(text):
            tk.Label(frame, text=text, bg=BG, fg=ACCENT,
                     font=self.f_head).pack(anchor="w", padx=24, pady=(20, 2))

        def sub(text):
            tk.Label(frame, text=text, bg=BG, fg=TEXT_DIM,
                     font=self.f_ts).pack(anchor="w", padx=24, pady=(0, 6))

        # -- models
        section("Active Models")
        sub("Uncheck a model to remove it from the next turn. Takes effect immediately.")

        self._checks_frame = tk.Frame(frame, bg=BG)
        self._checks_frame.pack(fill="x", padx=24, pady=4)
        self._rebuild_settings_checks()

        tk.Frame(frame, bg=BORDER, height=1).pack(fill="x", padx=24, pady=14)

        # -- extra prompt
        section("Extra System Prompt")
        sub("Appended to every model's system prompt. Takes effect on the next turn.")
        self.prompt_box = tk.Text(
            frame, height=5, bg=ENTRY_BG, fg=TEXT_MAIN,
            insertbackground=TEXT_MAIN, relief="flat",
            font=self.f_mono, wrap="word", padx=8, pady=6,
        )
        self.prompt_box.pack(fill="x", padx=24, pady=(0, 4))
        self.prompt_box.bind("<KeyRelease>", lambda e: self.extra_prompt.set(
            self.prompt_box.get("1.0", "end-1c")))

        tk.Frame(frame, bg=BORDER, height=1).pack(fill="x", padx=24, pady=14)

        # -- seed / restart
        section("Restart Chat")
        sub("Clear the feed and start over with a new opening message.")
        seed_row = tk.Frame(frame, bg=BG)
        seed_row.pack(fill="x", padx=24, pady=(0, 24))
        self.seed_entry = tk.Entry(
            seed_row, bg=ENTRY_BG, fg=TEXT_MAIN,
            insertbackground=TEXT_MAIN, relief="flat", font=self.f_body,
        )
        self.seed_entry.insert(0, self.seed)
        self.seed_entry.pack(side="left", fill="x", expand=True, ipady=6, padx=(0, 8))
        tk.Button(
            seed_row, text="Restart", bg=ACCENT, fg="white",
            relief="flat", font=self.f_body, cursor="hand2",
            activebackground="#c73050", activeforeground="white",
            command=self._restart_chat, padx=14,
        ).pack(side="left")

    # ---------------------------------------------------------------- roster

    def _sync_enabled(self):
        """Rebuild the plain set the engine thread reads. Main thread only."""
        self._enabled_set = {m for m, v in self.enabled_vars.items() if v.get()}

    def _rebuild_roster(self):
        self._sync_enabled()
        for w in self.roster_inner.winfo_children():
            w.destroy()
        self.roster_rows = {}
        for m in self.all_models:
            enabled = self.enabled_vars[m].get()
            col     = self.model_color[m] if enabled else OFFLINE
            row     = tk.Frame(self.roster_inner, bg=SIDEBAR_BG)
            row.pack(fill="x", padx=6, pady=2)

            av = tk.Canvas(row, width=28, height=28, bg=SIDEBAR_BG, highlightthickness=0)
            av.create_oval(2, 2, 26, 26, fill=col, outline="")
            av.create_text(14, 14, text=model_initials(m),
                           fill="white", font=("Helvetica", 7, "bold"))
            av.pack(side="left", padx=(0, 6))

            lbl = tk.Label(row, text=m, bg=SIDEBAR_BG,
                           fg=TEXT_MAIN if enabled else TEXT_DIM,
                           font=self.f_ts, anchor="w")
            lbl.pack(side="left", fill="x", expand=True)

            dot = tk.Label(row, text="●", bg=SIDEBAR_BG,
                           fg=ONLINE if enabled else OFFLINE, font=self.f_ts)
            dot.pack(side="right", padx=4)

            self.roster_rows[m] = {"lbl": lbl, "dot": dot, "av": av, "col": col}

    def _set_typing(self, model):
        for m, r in self.roster_rows.items():
            r["dot"].configure(fg=ACCENT if m == model else
                               (ONLINE if self.enabled_vars[m].get() else OFFLINE))

    def _clear_typing(self):
        for m, r in self.roster_rows.items():
            r["dot"].configure(fg=ONLINE if self.enabled_vars[m].get() else OFFLINE)

    # ---------------------------------------------------------------- bubbles

    def _add_message(self, frm, to, text, ts):
        is_sys = frm in ("system", "moderator")
        color  = self.model_color.get(frm, TEXT_DIM)

        outer = tk.Frame(self.msg_frame, bg=BG)
        outer.pack(fill="x", padx=10, pady=4)

        if is_sys:
            tk.Label(outer, text=f"— {text} —",
                     bg=BG, fg=TEXT_SYS, font=self.f_ts,
                     wraplength=640, justify="center").pack(pady=2)
            return

        # avatar
        av = tk.Canvas(outer, width=34, height=34, bg=BG, highlightthickness=0)
        av.create_oval(2, 2, 32, 32, fill=color, outline="")
        av.create_text(17, 17, text=model_initials(frm),
                       fill="white", font=("Helvetica", 8, "bold"))
        av.pack(side="left", anchor="n", padx=(0, 8))

        right = tk.Frame(outer, bg=BG)
        right.pack(side="left", fill="x", expand=True)

        # header
        hdr = tk.Frame(right, bg=BG)
        hdr.pack(anchor="w")
        tk.Label(hdr, text=frm, bg=BG, fg=color,
                 font=self.f_name).pack(side="left")
        if to != "@all":
            tk.Label(hdr, text=f"  → {to}", bg=BG, fg=TEXT_DIM,
                     font=self.f_ts).pack(side="left")
        tk.Label(hdr, text=f"  {ts}", bg=BG, fg=TEXT_DIM,
                 font=self.f_ts).pack(side="left")

        # bubble
        bubble_bg = BUBBLE_DM if to != "@all" else BUBBLE_BC
        bubble = tk.Frame(right, bg=bubble_bg, padx=10, pady=7)
        bubble.pack(anchor="w", fill="x")
        # word-wrap width — will resize with window
        tk.Label(bubble, text=text, bg=bubble_bg, fg=TEXT_MAIN,
                 font=self.f_body, wraplength=560,
                 justify="left", anchor="w").pack(anchor="w")

    # ---------------------------------------------------------------- scroll

    def _on_scroll(self, event):
        if event.num == 4:
            self.canvas.yview_scroll(-1, "units")
        elif event.num == 5:
            self.canvas.yview_scroll(1, "units")
        else:
            self.canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")

    def _scroll_bottom(self):
        self.canvas.update_idletasks()
        self.canvas.yview_moveto(1.0)

    # ---------------------------------------------------------------- controls


    def _restart_chat(self):
        new_seed = self.seed_entry.get().strip() or self.seed
        self.engine.stop()
        for w in self.msg_frame.winfo_children():
            w.destroy()
        while not self.ui_queue.empty():
            try: self.ui_queue.get_nowait()
            except: pass
        self.seed = new_seed
        self._start_engine()
        self.status_var.set("Chat restarted.")

    # ---------------------------------------------------------------- engine

    def _start_engine(self):
        self.engine = Engine(
            client          = self.client,
            models          = self.all_models,
            ui_queue        = self.ui_queue,
            get_prompt_fn   = lambda: self.extra_prompt.get(),
            get_enabled_fn  = lambda: self._enabled_set,
            seed            = self.seed,
            host            = self.host,
            transcript_file = "Transcript.md",
        )
        threading.Thread(target=self.engine.run, daemon=True).start()

    # ---------------------------------------------------------------- poll

    def _poll(self):
        try:
            while True:
                item = self.ui_queue.get_nowait()
                if "typing" in item:
                    model = item["typing"]
                    self.typing_var.set(f"{model} is typing…")
                    self._set_typing(model)
                    self.status_var.set(f"Waiting for {model}…")
                elif "error" in item:
                    self.typing_var.set("")
                    self._clear_typing()
                    self.status_var.set(f"⚠  {item['error']}: {item['msg']}")
                else:
                    self.typing_var.set("")
                    self._clear_typing()
                    self._add_message(item["from"], item["to"],
                                      item["text"], item["ts"])
                    self._scroll_bottom()
                    n = len(self.msg_frame.winfo_children())
                    self.status_var.set(f"{n} messages  ·  {self.host}")
        except queue.Empty:
            pass
        self.after(100, self._poll)

    # ---------------------------------------------------------------- model hot-add

    def _add_model(self, name):
        """Register a newly discovered model without restarting anything."""
        if name in self.all_models:
            return
        color_idx         = len(self.all_models)
        self.all_models.append(name)
        self.engine.all_models.append(name)
        self.model_color[name]   = MODEL_COLORS[color_idx % len(MODEL_COLORS)]
        self.enabled_vars[name]  = tk.BooleanVar(value=True)
        self._enabled_set.add(name)
        self._rebuild_roster()
        self._rebuild_settings_checks()
        self.model_count_var.set(f"{len(self.all_models)} models")
        # announce to the feed
        self.ui_queue.put({
            "from": "system", "to": "@all",
            "text": f"{name} just joined the network.",
            "ts":   datetime.now().strftime("%H:%M"),
        })
        # drop it into the engine's active queue so it says hi
        self.engine._enqueue(name, "You just joined LLM-Social. Say hello!", "system")

    def _check_for_new_models(self):
        """Poll Ollama for new models every 30 s; add any we haven't seen."""
        def _poll_thread():
            while True:
                time.sleep(30)
                try:
                    current = set(list_models(self.client))
                    known   = set(self.all_models)
                    for m in sorted(current - known):
                        self.after(0, self._add_model, m)
                except Exception:
                    pass
        threading.Thread(target=_poll_thread, daemon=True).start()

    def _rebuild_settings_checks(self):
        """Rebuild just the checkboxes frame in Settings when a new model arrives."""
        if not hasattr(self, "_checks_frame"):
            return
        for w in self._checks_frame.winfo_children():
            w.destroy()
        for m in self.all_models:
            col   = self.model_color[m]
            row_f = tk.Frame(self._checks_frame, bg=BG)
            row_f.pack(anchor="w", pady=2)
            tk.Label(row_f, text="●", fg=col, bg=BG,
                     font=self.f_body).pack(side="left")
            tk.Checkbutton(
                row_f, text=f"  {m}",
                variable=self.enabled_vars[m],
                bg=BG, fg=TEXT_MAIN, selectcolor=ENTRY_BG,
                activebackground=BG, activeforeground=TEXT_MAIN,
                font=self.f_sm, command=self._rebuild_roster,
            ).pack(side="left")

    def on_close(self):
        self.engine.stop()
        self.destroy()


# ---------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser(description="LLM-Social GUI")
    ap.add_argument("--host",        default=DEFAULT_HOST)
    ap.add_argument("--seed",        default=None)
    ap.add_argument("--models",      nargs="*", default=None)
    args = ap.parse_args()

    client = Client(host=args.host, timeout=TIMEOUT)
    try:
        all_models = list_models(client)
    except Exception as e:
        print(f"Can't reach Ollama at {args.host}: {e}")
        print("Is `ollama serve` running?")
        sys.exit(1)

    members = args.models or all_models
    members = [m for m in members if m in all_models]

    if len(members) < 2:
        print("Need at least 2 models. Run `ollama pull <model>` to add more.")
        sys.exit(1)

    app = LLMSocialApp(client, members, args.host, seed=args.seed)
    app.protocol("WM_DELETE_WINDOW", app.on_close)
    app.mainloop()


if __name__ == "__main__":
    main()
