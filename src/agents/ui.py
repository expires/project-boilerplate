from __future__ import annotations

import json
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from . import daemon
from .board import AGENTS_DIRNAME, Board, card_as_dict
from .config import load_config
from .llm import load_usage
from .logs import read_log
from .orchestrator import Orchestrator
from .runner import runner_state

COLUMNS = ("not_started", "in_progress", "review", "blocked", "closed")


def _paths(root: Path | str) -> tuple[Path, Path, Path]:
    agents = Path(root) / AGENTS_DIRNAME
    return agents / "ui.pid", agents / "ui.log", agents / "ui.json"


def read_pid(root: Path | str) -> int | None:
    pid_path, _, _ = _paths(root)
    if not pid_path.is_file():
        return None
    try:
        return int(pid_path.read_text().strip())
    except (ValueError, OSError):
        return None


def is_running(root: Path | str) -> bool:
    return daemon.alive(read_pid(root))


def ui_state(root: Path | str) -> dict[str, Any]:
    _, _, info_path = _paths(root)
    state: dict[str, Any] = {"running": False, "pid": None, "url": None, "host": None, "port": None}
    if info_path.is_file():
        try:
            info = json.loads(info_path.read_text())
            state.update({key: info.get(key) for key in ("host", "port", "url")})
        except json.JSONDecodeError:
            pass
    pid = read_pid(root)
    if daemon.alive(pid):
        state["running"] = True
        state["pid"] = pid
    return state


def build_board_payload(root: Path | str) -> dict[str, Any]:
    root = Path(root).resolve()
    config = load_config(root)
    usage = load_usage(root)
    board = Board(root)
    try:
        waves = Orchestrator(root, config).plan_waves()
    except Exception:  # noqa: BLE001 - a broken board should still render
        waves = []
    return {
        "columns": list(COLUMNS),
        "cards": [card_as_dict(card) for card in board.cards()],
        "runner": runner_state(root),
        "ui": ui_state(root),
        "waves": waves,
        "budget": {
            "spent_usd": round(float(usage.get("estimated_usd", 0.0)), 6),
            "monthly_usd": config.get("cost_controls", {}).get("monthly_budget_usd"),
        },
    }


def _handler_for(root: Path) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        project_root = root

        def log_message(self, *args: Any) -> None:
            return

        def _send(self, code: int, body: str, ctype: str) -> None:
            data = body.encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self) -> None:
            path = urlparse(self.path).path
            if path == "/":
                self._send(200, HTML, "text/html; charset=utf-8")
                return
            if path == "/api/board":
                try:
                    self._send(200, json.dumps(build_board_payload(self.project_root)), "application/json")
                except Exception as exc:  # noqa: BLE001
                    self._send(500, json.dumps({"error": str(exc)}), "application/json")
                return
            if path.startswith("/api/log/"):
                card_id = path.rsplit("/", 1)[-1]
                self._send(200, json.dumps({"id": card_id, "log": read_log(self.project_root, card_id)}), "application/json")
                return
            self._send(404, json.dumps({"error": "not found"}), "application/json")

    return Handler


def serve(root: Path | str, host: str = "127.0.0.1", port: int = 8765) -> None:
    root = Path(root).resolve()
    handler = _handler_for(root)
    try:
        server = ThreadingHTTPServer((host, port), handler)
    except OSError:
        server = ThreadingHTTPServer((host, 0), handler)
    actual_port = server.server_address[1]
    url = f"http://{host}:{actual_port}"
    _, _, info_path = _paths(root)
    info_path.parent.mkdir(parents=True, exist_ok=True)
    info_path.write_text(json.dumps({"host": host, "port": actual_port, "url": url}))
    print(url, flush=True)
    try:
        server.serve_forever()
    finally:
        server.server_close()
        info_path.unlink(missing_ok=True)


def start_detached(
    root: Path | str,
    host: str = "127.0.0.1",
    port: int = 8765,
    command: list[str] | None = None,
) -> str | None:
    root = Path(root).resolve()
    if is_running(root):
        return ui_state(root).get("url")
    pid_path, log_path, info_path = _paths(root)
    pid_path.parent.mkdir(parents=True, exist_ok=True)
    pid_path.unlink(missing_ok=True)
    info_path.unlink(missing_ok=True)
    custom = command is not None
    if command is None:
        command = [sys.executable, "-m", "agents.cli", "--root", str(root), "ui", "--host", host, "--port", str(port)]
    with log_path.open("a") as log:
        log.write(f"--- ui start {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())} ---\n")
    pid = daemon.spawn(command, str(root), log_path)
    pid_path.write_text(str(pid))
    if not custom:
        for _ in range(50):
            if info_path.is_file():
                break
            time.sleep(0.1)
    return ui_state(root).get("url")


def stop(root: Path | str, timeout: float = 5.0) -> bool:
    pid_path, _, info_path = _paths(root)
    stopped = daemon.terminate(read_pid(root), timeout)
    pid_path.unlink(missing_ok=True)
    info_path.unlink(missing_ok=True)
    return stopped


HTML = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>agent-board</title>
<style>
:root{color-scheme:dark}
*{box-sizing:border-box}
body{margin:0;font:14px/1.4 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;background:#0d1117;color:#e6edf3}
header{display:flex;align-items:center;gap:16px;padding:12px 16px;border-bottom:1px solid #21262d;position:sticky;top:0;background:#0d1117;z-index:2}
header h1{font-size:15px;margin:0;font-weight:600}
.pill{font-size:12px;padding:2px 8px;border-radius:999px;border:1px solid #30363d;color:#8b949e}
.dot{display:inline-block;width:8px;height:8px;border-radius:50%;margin-right:6px;background:#f85149}
.dot.on{background:#3fb950}
.board{display:grid;grid-template-columns:repeat(5,minmax(220px,1fr));gap:12px;padding:12px;align-items:start}
.col{background:#010409;border:1px solid #21262d;border-radius:10px;min-height:120px}
.col h2{font-size:12px;text-transform:uppercase;letter-spacing:.05em;color:#8b949e;margin:0;padding:10px 12px;border-bottom:1px solid #21262d;display:flex;justify-content:space-between}
.cards{padding:8px;display:flex;flex-direction:column;gap:8px}
.card{background:#161b22;border:1px solid #30363d;border-radius:8px;padding:8px 10px;cursor:pointer}
.card:hover{border-color:#58a6ff}
.card .id{font-size:11px;color:#8b949e}
.card .title{font-weight:600;margin:2px 0 6px}
.card .meta{display:flex;flex-wrap:wrap;gap:6px;font-size:11px;color:#8b949e}
.tag{border:1px solid #30363d;border-radius:6px;padding:0 6px}
.blocked .card{border-color:#6e2b2b}
.closed{opacity:.7}
aside{position:fixed;top:0;right:0;width:min(520px,90vw);height:100%;background:#0d1117;border-left:1px solid #30363d;transform:translateX(100%);transition:transform .18s ease;overflow:auto;z-index:3;padding:16px}
aside.open{transform:none}
aside h3{margin:0 0 8px}
pre{white-space:pre-wrap;word-break:break-word;background:#161b22;border:1px solid #30363d;border-radius:8px;padding:10px;font-size:12px}
button{background:#21262d;color:#e6edf3;border:1px solid #30363d;border-radius:6px;padding:4px 10px;cursor:pointer}
</style></head>
<body>
<header>
  <h1>agent-board</h1>
  <span class="pill" id="runner"><span class="dot"></span>runner</span>
  <span class="pill" id="budget">budget</span>
  <span class="pill" id="waves">waves</span>
</header>
<div class="board" id="board"></div>
<aside id="drawer"><button onclick="closeDrawer()">close</button><h3 id="drawer-title"></h3><pre id="drawer-body"></pre></aside>
<script>
const COLS=["not_started","in_progress","review","blocked","closed"];
const COLORS={blocked:"blocked",closed:"closed"};
function esc(s){return (s==null?"":String(s)).replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));}
function cardHtml(c){
  const tags=[];
  if(c.group) tags.push(`<span class="tag">${esc(c.group)}</span>`);
  else if(c.route) tags.push(`<span class="tag">${esc(c.route)}</span>`);
  tags.push(`<span class="tag">p${esc(c.priority)}</span>`);
  if(c.depends_on&&c.depends_on.length) tags.push(`<span class="tag">deps ${c.depends_on.map(esc).join(",")}</span>`);
  if(c.attempts) tags.push(`<span class="tag">att ${esc(c.attempts)}</span>`);
  if(c.review_cycles) tags.push(`<span class="tag">rev ${esc(c.review_cycles)}</span>`);
  return `<div class="card" onclick="openDrawer('${esc(c.id)}')"><div class="id">${esc(c.id)}</div><div class="title">${esc(c.title)}</div><div class="meta">${tags.join("")}</div></div>`;
}
function render(d){
  const board=document.getElementById("board");
  const by={}; (d.cards||[]).forEach(c=>{(by[c.status]=by[c.status]||[]).push(c)});
  board.innerHTML=(d.columns||COLS).map(col=>{
    const items=(by[col]||[]).sort((a,b)=>(a.priority-b.priority)||a.id.localeCompare(b.id));
    return `<section class="col ${COLORS[col]||""}"><h2><span>${col.replace("_"," ")}</span><span>${items.length}</span></h2><div class="cards">${items.map(cardHtml).join("")}</div></section>`;
  }).join("");
  const r=d.runner||{}; const el=document.getElementById("runner");
  el.innerHTML=`<span class="dot ${r.running?"on":""}"></span>runner ${r.running?"pid "+r.pid:"stopped"}`;
  const b=d.budget||{}; document.getElementById("budget").textContent=`$${(b.spent_usd||0).toFixed(4)} / $${b.monthly_usd}`;
  document.getElementById("waves").textContent=`waves ${(d.waves||[]).length}`;
}
async function openDrawer(id){
  const t=document.getElementById("drawer-title"); t.textContent=id;
  const body=document.getElementById("drawer-body"); body.textContent="loading…";
  document.getElementById("drawer").classList.add("open");
  try{const r=await fetch("/api/log/"+encodeURIComponent(id)); const d=await r.json(); body.textContent=d.log||"(no log)";}catch(e){body.textContent=String(e);}
}
function closeDrawer(){document.getElementById("drawer").classList.remove("open");}
async function tick(){try{const r=await fetch("/api/board");render(await r.json());}catch(e){}}
setInterval(tick,1000); tick();
</script>
</body></html>
"""
