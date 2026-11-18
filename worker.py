#!/usr/bin/env python3
"""隔離コンテナ (ネットワークなし) で動くワーカー。

共有ボリューム /work/jobs/<id>/ を監視し、status が queued のジョブを処理する。
  meta.json   : 状態・進捗 (web が表示に使う)
  result.json : 実行結果 (完了時)
同じタグの直前の実行と比較し、新しい行 (タイムスタンプ or 内容が前回にないもの) に updated を付ける。
完了後は、そのタグの最新 2 件より古い実行を自動でゴミ箱に移す (meta.json の trashed。ファイルは残るので復元できる)。
"""
import json
import os
import shutil
import subprocess
import time

import run_sheet_c as eng
from diffview import norm_key

WORK = os.environ.get("WORK_DIR", "/work")
JOBS = os.path.join(WORK, "jobs")
EXPORT = os.environ.get("EXPORT_DIR", "/output")   # ホストに見える出力先 (compose で ./output に割り当て)


def read_json(path, default=None):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def write_json(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False)
    os.replace(tmp, path)  # web が書きかけを読まないようにする


def next_job():
    queued = []
    for jid in os.listdir(JOBS):
        meta = read_json(os.path.join(JOBS, jid, "meta.json"))
        if meta and meta.get("status") == "queued":
            queued.append((meta.get("created", ""), jid))
    return min(queued)[1] if queued else None


def previous_keys(tag, jid):
    """同じタグで直前に完了した実行の (job id, 行キー集合)。なければ (None, None)。"""
    prev = None
    for other in os.listdir(JOBS):
        if other < jid and (prev is None or other > prev):
            m = read_json(os.path.join(JOBS, other, "meta.json"))
            if m and m.get("status") == "done" and m.get("tag") == tag and not m.get("trashed"):
                prev = other
    if prev is None:
        return None, None
    results = read_json(os.path.join(JOBS, prev, "result.json"), [])
    return prev, {norm_key(r["row_key"]) for r in results}


def archive_old(tag):
    """同じタグの実行のうち、最新 2 件より古いものをゴミ箱に移す。復元されたもの (keep) は残す。"""
    metas = (read_json(os.path.join(JOBS, j, "meta.json")) for j in os.listdir(JOBS))
    runs = sorted((m for m in metas if m and m.get("tag") == tag and m.get("status") == "done"
                   and not m.get("trashed")), key=lambda m: m["id"], reverse=True)
    for m in runs[2:]:
        if not m.get("keep"):
            m.update(trashed=True, trashed_auto=True, trashed_at=time.strftime("%Y-%m-%d %H:%M:%S"))
            write_json(os.path.join(JOBS, m["id"], "meta.json"), m)


def compiler_version():
    gcc = shutil.which("gcc") or "gcc"
    out = subprocess.run([gcc, "--version"], capture_output=True, text=True).stdout
    return out.splitlines()[0] if out else "不明"


def export_job(jid, meta, results):
    """完了した実行を EXPORT/jobs/<タグ>/<id>/ に書き出す (meta.json / result.json / result.csv)。
    ボリュームの中身をホストから直接見られるようにするための控え。失敗しても実行結果には影響させない。"""
    try:
        tag = "".join(c if c.isalnum() or c in "-_." else "_" for c in meta.get("tag") or "") or "no-tag"
        d = os.path.join(EXPORT, "jobs", tag, jid)
        os.makedirs(d, exist_ok=True)
        write_json(os.path.join(d, "meta.json"), meta)
        write_json(os.path.join(d, "result.json"), results)
        eng.write_csv(os.path.join(d, "result.csv"), results)
    except OSError as e:
        print(f"出力先への書き出しに失敗: {e}", flush=True)


def process(jid):
    jdir = os.path.join(JOBS, jid)
    meta_path = os.path.join(jdir, "meta.json")
    meta = read_json(meta_path)
    meta.update(status="running", done=0, total=0, compiler=compiler_version())
    write_json(meta_path, meta)
    try:
        rows = eng.load_rows(os.path.join(jdir, meta["answers_file"]))
        if len(rows) < 2:
            raise ValueError("回答ファイルにデータがありません。")
        cases = eng.load_cases(os.path.join(jdir, meta["cases_file"]))
        meta["problems"] = [h.strip() for h in rows[0][eng.FIRST_ANS:] if h.strip()]   # 点数表の列 (誰も答えていない問題も含む)
        tasks = eng.collect_tasks(rows)
        if not tasks:
            raise ValueError("実行対象の回答がありません。E 列以降に回答があるか確認してください。")

        def progress(done, total):
            meta.update(done=done, total=total)
            write_json(meta_path, meta)

        results = eng.run_all(tasks, shutil.which("gcc") or "gcc", cases, [("", "")],
                              meta.get("timeout", 5), 4, progress)

        # 同じタグの前回の実行と比較して、新しい (更新された) 行を印付けする
        tag = meta.get("tag", "")
        prev_job, prev_keys = previous_keys(tag, jid) if tag else (None, None)
        for r in results:
            r["updated"] = prev_keys is not None and norm_key(r["row_key"]) not in prev_keys
        write_json(os.path.join(jdir, "result.json"), results)

        meta.update(status="done", total=len(results), done=len(results),
                    compared=prev_job is not None, prev_job=prev_job,
                    updated=sum(r["updated"] for r in results))
    except (Exception, SystemExit) as e:  # SystemExit: エンジンの sys.exit(メッセージ)
        code = getattr(e, "code", None)
        meta.update(status="error", message=code if isinstance(code, str) else f"{type(e).__name__}: {e}")
    write_json(meta_path, meta)
    if meta["status"] == "done":
        export_job(jid, meta, results)
    if meta["status"] == "done" and meta.get("tag"):
        archive_old(meta["tag"])


def main():
    os.makedirs(JOBS, exist_ok=True)
    for jid in os.listdir(JOBS):  # 再起動前に処理中だったジョブをやり直す
        p = os.path.join(JOBS, jid, "meta.json")
        meta = read_json(p)
        if meta and meta.get("status") == "running":
            meta["status"] = "queued"
            write_json(p, meta)
    print("worker started", flush=True)
    while True:
        jid = next_job()
        if jid:
            print("processing", jid, flush=True)
            process(jid)
        else:
            time.sleep(1)


if __name__ == "__main__":
    main()
