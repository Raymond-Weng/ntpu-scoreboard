"""練習賽計分板（北大 Judge Girl 專用）：抓 Judge 的提交紀錄，算 ICPC/CPE 式排名，賽後可以復盤。

執行：python3 scoreboard.py  然後開 http://localhost:8000
只用標準函式庫，不用裝套件。

環境變數（都可以不設）：
  SCOREBOARD_JUDGE         北大 Judge Girl 的網址，預設 http://120.126.151.220
  SCOREBOARD_PORT          網頁的 port，預設 8000
  SCOREBOARD_STATE         比賽存檔，預設 data/contest.json
  SCOREBOARD_AWAKE         設 0 就不讓 Mac 保持螢幕常亮
  SCOREBOARD_LAN_CONTROL   設 1 讓同網路的其他電腦也能操作（預設只有這台能按按鈕，別人只能看）
"""
import html
import json
import math
import os
import re
import subprocess
import sys
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
JUDGE = (os.environ.get("SCOREBOARD_JUDGE") or "http://120.126.151.220").rstrip("/")
STATE_FILE = Path(os.environ.get("SCOREBOARD_STATE") or HERE / "data" / "contest.json")
DATA_DIR = STATE_FILE.parent  # 存檔放這裡，復盤可以選這個資料夾裡的任何一場
PORT = int(os.environ.get("SCOREBOARD_PORT") or 8000)
LAN_CONTROL = os.environ.get("SCOREBOARD_LAN_CONTROL") == "1"

POLL_SECONDS = 5
MAX_PAGES = 100        # 每題每次最多往回翻幾頁（一頁約 20 筆）
STALE_MIN = 15         # 判題中的提交只追最近幾分鐘的，卡住的舊提交不再一直翻回去
KEEP_POLLING_MIN = 30  # 比賽結束後再抓幾分鐘，等還在判題的出結果

# Judge 的結果代碼（res）
CE, RE, TLE, WA, AC = 1, 4, 5, 6, 7
PENDING = {0, 8}
VERDICT = ['', 'CE', 'OLE', 'MLE', 'RE', 'TLE', 'WA', 'AC', 'Uploading', 'PE']
NET_ERRORS = (urllib.error.URLError, TimeoutError, ConnectionError)

lock = threading.Lock()
generation = 0  # 每次換起點 +1，讓還在跑的抓取知道資料已經作廢
state = {
    "title": "練習賽",
    "problems": [],          # [{"pid": 11, "label": "A", "title": "...", "cids": [0]}]
    "start": None,           # ms timestamp
    "start_sid": None,       # 回推：從這個 SID 開始算（含）
    "duration_min": 180,
    "penalty_min": 20,
    "freeze_min": 0,         # 最後幾分鐘封榜（0 = 不封）
    "subs": {},              # sid -> submission
    "pinned": [],            # 釘選的學號，排在榜的最上面
    "all_sid": None,         # 全站提交看到的最新 SID
}


# ---------- 存檔 ----------

def write_json(path, data):
    """先寫暫存檔再換名字，寫到一半當掉也不會把存檔弄壞。"""
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False), "utf-8")
    os.replace(tmp, path)


def load():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if not os.environ.get("SCOREBOARD_STATE"):
        # 舊版把存檔放在程式旁邊，搬進 data/
        for old in HERE.glob("contest*.json"):
            if not (DATA_DIR / old.name).exists():
                old.rename(DATA_DIR / old.name)
                print(f"搬移存檔 {old.name} -> {DATA_DIR}")
    if STATE_FILE.exists():
        state.update(json.loads(STATE_FILE.read_text("utf-8")))


def save():
    write_json(STATE_FILE, state)


# ---------- 抓 Judge ----------

def judge_get(path, timeout=10):
    with urllib.request.urlopen(JUDGE + path, timeout=timeout) as r:
        return r.read()


def fetch_subs(page=1, pid=None, cid=0):
    """Judge 的提交紀錄，每頁約 20 筆，新的在前。
    給 pid 就只抓那題；cid=0 是 Problem Set，其他是考試（/problem/<cid>/<pid>）。"""
    q = f"/api/submission?limit=20&page={page}"
    if pid is not None:
        q += f"&pid={pid}&cid={cid}"
    return json.loads(judge_get(q))


def sid_time(target):
    """找 SID >= target 裡最早那筆的時間（ms）。翻全站提交，
    先用 SID 差距估頁數直接跳過去，不用一頁頁翻。"""
    newest = fetch_subs(1)
    if not newest:
        raise ValueError("Judge 沒有回應")
    if newest[0]["sid"] < target:
        raise ValueError(f"SID {target} 還不存在（目前最新是 {newest[0]['sid']}）")
    page = max(1, (newest[0]["sid"] - target) // 20)
    best = None
    for _ in range(60):
        subs = fetch_subs(page)
        if not subs:
            page -= 1
            continue
        hit = [s for s in subs if s["sid"] >= target]
        if hit:
            best = min(hit, key=lambda s: s["sid"])
        oldest = min(s["sid"] for s in subs)
        if oldest < target:  # 這頁已經跨過 target，答案就在這頁或前一頁
            if best:
                return best["ts"], best["sid"]
            page -= 1
        else:
            page += max(1, (oldest - target) // 20)
    if best:
        return best["ts"], best["sid"]
    raise ValueError(f"找不到 SID {target}")


# ---------- 時間 ----------

def parse_hhmm(text):
    nums = re.findall(r"\d+", str(text))
    if len(nums) < 2:
        raise ValueError(f"時間格式要像 14:10：{text}")
    h, m = int(nums[0]), int(nums[1])
    if not (0 <= h < 24 and 0 <= m < 60):
        raise ValueError(f"時間不對：{text}")
    return h, m


def at_time(text, day=None):
    """'14:10' -> 那天（預設今天）14:10 的 ms。"""
    h, m = parse_hhmm(text)
    base = datetime.fromtimestamp(day / 1000) if day else datetime.now()
    return int(base.replace(hour=h, minute=m, second=0, microsecond=0).timestamp() * 1000)


def same_hhmm(ms, text):
    t = datetime.fromtimestamp(ms / 1000)
    return (t.hour, t.minute) == parse_hhmm(text)


def now_ms():
    return int(time.time() * 1000)


# ---------- 比賽狀態（下面這些都要在 lock 裡呼叫） ----------

def contest_end():
    return state["start"] + state["duration_min"] * 60000 if state["start"] else None


def set_start(start_ms, start_sid=None):
    """換起點就清空重抓，poller 會從新起點一路補回來。"""
    global generation
    generation += 1
    state["start"], state["start_sid"] = start_ms, start_sid
    state["subs"] = {}
    state["all_sid"] = None


def snapshot():
    """把目前這場另存一份，例如 data/contest-1029-1410.json；同名就加 -2、-3。"""
    if state["start"] is None or not state["subs"]:
        raise ValueError("目前這場沒有提交，沒東西可以存")
    stamp = datetime.fromtimestamp(state["start"] / 1000).strftime("%m%d-%H%M")
    path, n = DATA_DIR / f"contest-{stamp}.json", 1
    while path.exists():
        n += 1
        path = DATA_DIR / f"contest-{stamp}-{n}.json"
    write_json(path, state)
    return {"file": path.name}


def counts(s):
    start, sid = state["start"], state.get("start_sid")
    return s["ts"] >= start and (sid is None or s["sid"] >= sid)


def store(subs):
    """把提交存進 state（只留選定題目、比賽開始後的）。"""
    known = state["subs"]
    for s in subs:
        p = next((p for p in state["problems"] if p["pid"] == s["pid"]), None)
        if not p:
            continue
        if counts(s):
            known[str(s["sid"])] = s
        if not p.get("title"):
            p["title"] = s["ttl"]
        if s["cid"] not in p.setdefault("cids", [0]):
            # 有人從某場考試（/problem/<cid>/<pid>）交這題 -> 以後也盯那場
            p["cids"] = sorted(p["cids"] + [s["cid"]])
            print(f"found exam cid={s['cid']} for pid={s['pid']}")


def label(i):
    return chr(ord("A") + i) if i < 26 else f"P{i + 1}"


def relabel():
    for i, p in enumerate(state["problems"]):
        p["label"] = label(i)


# ---------- 輪詢 ----------

def sync_problem(pid, cid=0):
    """抓一題的提交。第 1 頁接不上舊資料（兩次輪詢間超過一頁）時就往後翻頁補，
    一直補到接上、或翻到比賽開始前。最近還在判題中的舊提交也會翻過去更新。"""
    for page in range(1, MAX_PAGES + 1):
        subs = fetch_subs(page, pid, cid)
        if not subs:
            return
        with lock:
            start = state["start"]
            if start is None:
                return
            overlap = any(str(s["sid"]) in state["subs"] for s in subs)
            store(subs)
            oldest = subs[-1]
            recent = now_ms() - STALE_MIN * 60000
            stale = any(s["pid"] == pid and s["cid"] == cid and s["res"] in PENDING
                        and s["sid"] < oldest["sid"] and s["ts"] > recent
                        for s in state["subs"].values())
        if oldest["ts"] < start:
            return  # 已經翻到比賽開始前
        if overlap and not stale:
            return  # 已經接上之前抓到的資料
        print(f"backfill cid={cid} pid={pid} -> page {page + 1}")


def sync_all():
    """全站提交：不管從哪場考試交都抓得到，順便找出考試的 cid。
    記住上次看到的最新 SID，翻到那裡就停；第一次會一路回推到比賽開始。"""
    with lock:
        gen = generation
    newest = None
    for page in range(1, 1001):
        subs = fetch_subs(page)
        if not subs:
            break
        newest = newest or subs[0]["sid"]
        with lock:
            if generation != gen:
                return  # 中途換了起點，這輪作廢
            start, last = state["start"], state.get("all_sid")
            store(subs)
        oldest = subs[-1]
        if oldest["ts"] < start or (last and oldest["sid"] <= last):
            break
        print(f"backfill all -> page {page + 1}")
    with lock:
        if newest and generation == gen:
            state["all_sid"] = max(newest, state.get("all_sid") or 0)


def poller():
    while True:
        with lock:
            end = contest_end()
            running = end is not None and now_ms() < end + KEEP_POLLING_MIN * 60000
        if running:
            try:
                sync_all()
            except Exception as e:
                print("fetch fail all", e)
            with lock:  # sync_all 可能找到新的考試 cid
                jobs = [(p["pid"], c) for p in state["problems"] for c in p.get("cids", [0])]
            for pid, cid in jobs:
                try:
                    sync_problem(pid, cid)
                except Exception as e:
                    print("fetch fail", cid, pid, e)
            with lock:
                save()
        time.sleep(POLL_SECONDS)


# ---------- 計分 ----------

def scoreboard():
    """ICPC 規則：解題數多的前面，再比罰時、最後 AC 時間。
    罰時 = 每題 AC 的分鐘 + AC 前每次錯誤 × penalty_min；CE 不罰，AC 之後的不算。
    replay.html 的 compute() 用一樣的規則，改這裡要一起改。"""
    with lock:
        st = json.loads(json.dumps(state))
    start = st["start"]
    end = start + st["duration_min"] * 60000 if start else None
    freeze_at = end - st["freeze_min"] * 60000 if end and st["freeze_min"] else None
    pids = [p["pid"] for p in st["problems"]]
    subs = sorted((s for s in st["subs"].values()
                   if start and s["pid"] in pids and s["ts"] <= end), key=lambda s: s["sid"])
    frozen = lambda s: freeze_at is not None and s["ts"] >= freeze_at

    teams = {}
    for s in subs:
        t = teams.setdefault(s["lgn"], {"name": s["lgn"], "cells": {}})
        c = t["cells"].setdefault(str(s["pid"]), {"tries": 0, "ac": None, "pending": 0, "frozen": 0})
        if c["ac"] is not None:
            continue
        if frozen(s):
            c["frozen"] += 1
        elif s["res"] in PENDING:
            c["pending"] += 1
        elif s["res"] == CE:
            continue
        else:
            c["tries"] += 1
            if s["res"] == AC:
                c["ac"] = (s["ts"] - start) // 60000

    # 首殺：那題最早 AC 的那一分鐘（同一分鐘就都算）
    first = {}
    for t in teams.values():
        for pid, c in t["cells"].items():
            if c["ac"] is not None and c["ac"] < first.get(pid, math.inf):
                first[pid] = c["ac"]
    rows = []
    for t in teams.values():
        solved = pen = last = 0
        for pid, c in t["cells"].items():
            if c["ac"] is not None:
                solved += 1
                pen += c["ac"] + (c["tries"] - 1) * st["penalty_min"]
                last = max(last, c["ac"])
                c["first"] = first[pid] == c["ac"]
        rows.append({**t, "solved": solved, "penalty": pen, "last": last})
    rows.sort(key=lambda r: (-r["solved"], r["penalty"], r["last"], r["name"]))
    for i, r in enumerate(rows):
        prev = rows[i - 1] if i else None
        same = prev and (prev["solved"], prev["penalty"]) == (r["solved"], r["penalty"])
        r["rank"] = prev["rank"] if same else i + 1

    # 每題結果統計（封榜後的只算進「封榜」）
    names = {AC: "AC", WA: "WA", TLE: "TLE", CE: "CE", RE: "RE"}
    stats = {str(pid): {"total": 0, "AC": 0, "WA": 0, "TLE": 0, "CE": 0, "RE": 0,
                        "other": 0, "pending": 0, "frozen": 0} for pid in pids}
    for s in subs:
        c = stats[str(s["pid"])]
        c["total"] += 1
        if frozen(s):
            c["frozen"] += 1
        elif s["res"] in PENDING:
            c["pending"] += 1
        else:
            c[names.get(s["res"], "other")] += 1
    for pid, c in stats.items():
        c["solvers"] = sum(1 for r in rows if (r["cells"].get(pid) or {}).get("ac") is not None)
        c["tried"] = sum(1 for r in rows if pid in r["cells"])

    def verdict(s):
        if frozen(s):
            return "?"
        return VERDICT[s["res"]] if 0 <= s["res"] < len(VERDICT) else str(s["res"])

    return {
        "title": st["title"], "problems": st["problems"], "start": start, "end": end,
        "freeze_at": freeze_at, "now": now_ms(), "rows": rows, "stats": stats,
        "recent": [{"lgn": s["lgn"], "pid": s["pid"], "ts": s["ts"], "v": verdict(s)}
                   for s in reversed(subs[-15:])],
        "start_sid": st.get("start_sid"),
        "pinned": st.get("pinned", []),
        "settings": {k: st[k] for k in ("duration_min", "penalty_min", "freeze_min", "title")},
    }


# ---------- 復盤用的存檔 ----------

def contest_files():
    """data/ 裡存過的比賽（*.json），給復盤選。第一個是現在這場，其他新的在前。"""
    with lock:
        out = [{"file": "", "title": state["title"], "start": state["start"],
                "subs": len(state["subs"]), "current": True}]
    saved = []
    for f in DATA_DIR.glob("*.json"):
        if f.resolve() == STATE_FILE.resolve():
            continue
        try:
            d = json.loads(f.read_text("utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(d, dict) and "subs" in d and "problems" in d:
            saved.append({"file": f.name, "title": d.get("title", f.stem), "start": d.get("start"),
                          "subs": len(d["subs"]), "judge_cid": d.get("judge_cid")})
    return out + sorted(saved, key=lambda c: -(c["start"] or 0))


def saved_file(name):
    """只接受 data/ 裡列得出來的存檔，不能用 ../ 之類的跑出去。"""
    if not name or name not in {c["file"] for c in contest_files()}:
        raise ValueError(f"沒有這個檔案：{name}")
    return DATA_DIR / name


def delete_contest(name):
    saved_file(name).unlink()
    return {"ok": True}


def replay(name=""):
    """復盤用：整場比賽的所有提交（不封榜），排名在瀏覽器算。"""
    if name:
        st = json.loads(saved_file(name).read_text("utf-8"))
    else:
        with lock:
            st = json.loads(json.dumps(state))
    pids = {p["pid"] for p in st["problems"]}
    subs = sorted((s for s in st["subs"].values() if s["pid"] in pids), key=lambda s: s["sid"])
    return {
        "title": st.get("title", ""), "problems": st["problems"], "start": st.get("start"),
        "duration_min": st.get("duration_min", 180), "penalty_min": st.get("penalty_min", 20),
        "freeze_min": st.get("freeze_min", 0), "pinned": st.get("pinned", []),
        # [sid, ts, 學號, pid, 結果]
        "subs": [[s["sid"], s["ts"], s["lgn"], s["pid"], s["res"]] for s in subs],
        "verdicts": VERDICT,
    }


# ---------- 匯入 Judge 上的考試 ----------

def judge_time(text):
    """'2024/04/16 10:10' -> ms"""
    return int(datetime.strptime(text.strip(), "%Y/%m/%d %H:%M").timestamp() * 1000)


def judge_contests():
    """Judge 的 /contest 頁面列出的所有考試（期中、期末…）。"""
    page = judge_get("/contest", timeout=15).decode("utf-8", "replace")
    rows = re.findall(r'<td>(\d+)</td>\s*<td>\s*<a href="/?contest/\d+">\s*(.*?)\s*</a>\s*</td>\s*'
                      r'<td>\s*(\d{4}/\d\d/\d\d \d\d:\d\d)\s*~\s*(\d{4}/\d\d/\d\d \d\d:\d\d)', page, re.S)
    done = {f.stem for f in DATA_DIR.glob("judge-*.json")}
    return [{"cid": int(c), "title": html.unescape(t), "start": judge_time(a), "end": judge_time(b),
             "file": f"judge-{c}.json", "imported": f"judge-{c}" in done} for c, t, a, b in rows]


def import_contest(cid):
    """把 Judge 上的一場考試整場抓下來存成 data/judge-<cid>.json。"""
    cid = int(cid)
    info = next((c for c in judge_contests() if c["cid"] == cid), None)
    if not info:
        raise ValueError(f"Judge 上找不到考試 {cid}")
    page = judge_get(f"/contest/{cid}", timeout=15).decode("utf-8", "replace")
    probs = re.findall(rf'href="/problem/{cid}/(\d+)"\s*>([^<]*)</a>', page)
    if not probs:
        raise ValueError(f"考試 {cid} 看不到題目（可能還沒開放）")
    start, end = info["start"], info["end"]
    subs = {}
    for pid, _ in probs:
        for n in range(1, 1001):  # 翻到考試開始前為止
            got = fetch_subs(n, int(pid), cid)
            if not got:
                break
            subs.update((str(x["sid"]), x) for x in got if start <= x["ts"] <= end)
            if got[-1]["ts"] < start:
                break
    write_json(DATA_DIR / info["file"], {
        "title": info["title"], "judge_cid": cid, "start": start,
        "duration_min": max(1, (end - start) // 60000), "penalty_min": 20, "freeze_min": 0,
        "problems": [{"pid": int(p), "label": label(i), "title": html.unescape(t.strip()), "cids": [cid]}
                     for i, (p, t) in enumerate(probs)],
        "subs": subs, "pinned": [],
    })
    return {"file": info["file"], "subs": len(subs)}


# ---------- 操作 ----------

def parse_problem(text):
    """接受 20、38/20 或整個網址 http://.../problem/38/20。
    一律抓 Problem Set（cid 0）；有寫考試編號就連那場考試一起抓。"""
    nums = [int(x) for x in re.findall(r"\d+", str(text).split("/problem/")[-1])]
    if not nums:
        raise ValueError("看不懂的題號")
    pid = nums[-1]
    cid = nums[-2] if len(nums) >= 2 else 0
    return pid, sorted({0, cid})


def lookup_title(pid, cids):
    """拿最近一筆提交的題名，抓不到就留空（比賽開始後會再補）。"""
    for cid in cids:
        try:
            subs = fetch_subs(1, pid, cid)
            if subs:
                return subs[0]["ttl"]
        except (*NET_ERRORS, ValueError):
            pass
    return ""


def parse_start(body):
    """開始／回推的起點：SID、時間（HH:MM，最近一次過去的那個時間）或現在。
    回傳 (start_ms, start_sid)。會連網路，不要在 lock 裡呼叫。"""
    if str(body.get("time", "")).strip():
        ms = at_time(body["time"])
        if ms > now_ms():
            ms -= 24 * 3600 * 1000  # 例如 00:10 回推到 23:30 -> 昨天
        return ms, None
    if str(body.get("sid", "")).strip():
        digits = re.findall(r"\d+", str(body["sid"]))
        if not digits:
            raise ValueError("SID 要是數字")
        target = int(digits[0])
        ts, _ = sid_time(target)
        return ts, target
    return now_ms(), None


def int_field(body, key, low=0):
    try:
        v = int(body[key])
    except (TypeError, ValueError):
        raise ValueError(f"{key} 要是整數")
    if v < low:
        raise ValueError(f"{key} 不能小於 {low}")
    return v


def apply_settings(body):
    """先把每個欄位都檢查完，最後才改 state，避免改到一半出錯。"""
    start = state["start"]
    new = {k: int_field(body, k, 1 if k == "duration_min" else 0)
           for k in ("duration_min", "penalty_min", "freeze_min") if k in body}
    if "title" in body:
        new["title"] = str(body["title"]).strip() or "練習賽"
    text = str(body.get("start_time", "")).strip()
    if text and not (start and same_hhmm(start, text)):
        # 只有真的改了開始時間才重抓；日期沿用原本那場（24 小時內），不然就是今天
        day = start if start and now_ms() - start < 24 * 3600 * 1000 else None
        start = at_time(text, day)
    if str(body.get("end_time", "")).strip():
        if start is None:
            raise ValueError("要先有開始時間")
        end = at_time(body["end_time"], start)
        if end <= start:
            end += 24 * 3600 * 1000  # 結束時間比開始早 -> 跨到隔天
        new["duration_min"] = max(1, round((end - start) / 60000))
    if start != state["start"]:
        set_start(start)
    state.update(new)


def stop():
    """結束比賽：把比賽長度改成到現在為止，榜單和復盤都留著。還沒開始的就直接取消。"""
    start = state["start"]
    if start is None:
        return
    if now_ms() < start:
        set_start(None)
    elif now_ms() < contest_end():
        state["duration_min"] = max(1, math.ceil((now_ms() - start) / 60000))


STATE_API = {"/api/start", "/api/stop", "/api/add", "/api/remove", "/api/pin", "/api/settings", "/api/snapshot"}


def handle_api(path, body):
    """會改比賽狀態的操作（path 一定在 STATE_API 裡）。"""
    if path == "/api/start":  # 回推要連網路找 SID，放在 lock 外面
        start_ms, start_sid = parse_start(body)
    if path == "/api/add":  # 先查題名，不要在 lock 裡面連網路
        pid, cids = parse_problem(body.get("pid", ""))
        title = str(body.get("title") or "").strip() or lookup_title(pid, cids)
    with lock:
        result = {"ok": True}
        if path == "/api/start":
            if state["start"] and state["subs"]:
                result["saved"] = snapshot()["file"]  # 開新的一場前，自動把上一場存起來
            set_start(start_ms, start_sid)
        elif path == "/api/stop":
            stop()
        elif path == "/api/add":
            old = next((p for p in state["problems"] if p["pid"] == pid), None)
            if old:
                old["cids"] = sorted(set(old.get("cids", [0])) | set(cids))
            else:
                state["problems"].append({"pid": pid, "cids": cids, "title": title})
                relabel()
        elif path == "/api/remove":
            pid = int_field(body, "pid")
            state["problems"] = [p for p in state["problems"] if p["pid"] != pid]
            relabel()
        elif path == "/api/pin":  # 切換釘選
            name = str(body.get("name", "")).strip()
            pins = state.setdefault("pinned", [])
            if name in pins:
                pins.remove(name)
            elif name:
                pins.append(name)
        elif path == "/api/settings":
            apply_settings(body)
        elif path == "/api/snapshot":
            result = snapshot()
        save()
    return result


# 不碰比賽狀態、只動 data/ 裡存檔的操作
FILE_API = {
    "/api/import": lambda body: import_contest(int_field(body, "cid")),
    "/api/delete": lambda body: delete_contest(str(body.get("file", ""))),
}


# ---------- 網頁伺服器 ----------

class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def send(self, code, data, ctype="application/json"):
        b = data if isinstance(data, bytes) else json.dumps(data, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype + "; charset=utf-8")
        self.end_headers()
        self.wfile.write(b)

    def can_control(self):
        """預設只有跑程式的這台電腦能按按鈕；同網路的人只能看。"""
        return LAN_CONTROL or self.client_address[0] in ("127.0.0.1", "::1", "::ffff:127.0.0.1")

    def run(self, fn):
        try:
            self.send(200, fn())
        except NET_ERRORS as e:
            self.send(502, {"error": f"連不上 Judge：{e}"})
        except (ValueError, KeyError, TypeError) as e:
            self.send(400, {"error": str(e)})
        except Exception as e:
            traceback.print_exc()
            self.send(500, {"error": f"伺服器出錯：{e}"})

    def do_GET(self):
        url = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(url.query)
        pages = {"/": "index.html", "/index.html": "index.html",
                 "/replay": "replay.html", "/replay.html": "replay.html"}
        api = {
            "/api/board": scoreboard,
            "/api/me": lambda: {"control": self.can_control()},
            "/api/contests": contest_files,
            "/api/judge_contests": judge_contests,
            "/api/replay": lambda: replay((q.get("f") or [""])[0]),
        }
        if url.path in pages:
            self.send(200, (HERE / pages[url.path]).read_bytes(), "text/html")
        elif url.path in api:
            self.run(api[url.path])
        else:
            self.send(404, {"error": "not found"})

    def do_POST(self):
        if not self.can_control():
            return self.send(403, {"error": "只有跑計分板的那台電腦可以操作（要開放請設 SCOREBOARD_LAN_CONTROL=1）"})
        # 擋掉別的網站偷偷送來的請求：一定要是 JSON，而且 Origin 要是自己
        origin = self.headers.get("Origin")
        if "application/json" not in (self.headers.get("Content-Type") or "") or (
                origin and urllib.parse.urlparse(origin).netloc != self.headers.get("Host")):
            return self.send(403, {"error": "不接受這個來源的請求"})
        try:
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(n) or b"{}")
        except ValueError:
            return self.send(400, {"error": "看不懂的 JSON"})
        if not isinstance(body, dict):
            return self.send(400, {"error": "JSON 要是物件"})

        if self.path in FILE_API:
            return self.run(lambda: FILE_API[self.path](body))
        if self.path not in STATE_API:
            return self.send(404, {"error": f"沒有這個 API：{self.path}"})
        self.run(lambda: handle_api(self.path, body))


def keep_display_awake():
    """Mac 上叫系統的 caffeinate 讓螢幕不睡、不進螢幕保護；-w 讓它跟著這個程式一起結束。"""
    if sys.platform != "darwin" or os.environ.get("SCOREBOARD_AWAKE") == "0":
        return
    try:
        subprocess.Popen(["caffeinate", "-d", "-i", "-w", str(os.getpid())])
        print("螢幕常亮：開（關掉這個程式就恢復）")
    except OSError as e:
        print("caffeinate 開不起來：", e)


if __name__ == "__main__":
    load()
    keep_display_awake()
    threading.Thread(target=poller, daemon=True).start()
    print(f"Scoreboard: http://localhost:{PORT}")
    if not LAN_CONTROL:
        print("同網路的人可以用你的 IP 看榜，但只有這台電腦能操作")
    ThreadingHTTPServer(("0.0.0.0", PORT), H).serve_forever()
