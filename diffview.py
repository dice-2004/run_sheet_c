"""同じタグの 2 つの実行 (今回 / ひとつ前) の結果を比べて、差分の一覧を作る。

行キー (学籍番号|タイムスタンプ) が前回になければ「新しい行」。
  追加 : 学籍番号 × 問題 の組が前回にもなかった
  更新 : 組は前回にもあるが、行 (提出) が新しい。前回の出力・コードと並べて見せる
  削除 : 前回にあった行が、今回は組ごとなくなっている
"""
import difflib
import re


def norm_key(key):
    """行キー (学籍番号|タイムスタンプ) を、秒より下の桁を捨ててそろえる。
    Excel や openpyxl で保存し直すと、小数点以下が .947000 -> .946999 のようにずれることがあるため。"""
    return re.sub(r"(\d{1,2}:\d{2}:\d{2})[.,]\d+$", r"\1", key)


def _side(res, i):
    """結果 res の i 番目のケースを {text, status, note, in} にする。なければ None。"""
    if res is None:
        return None
    cases = res.get("cases", [])
    if i == 0 and not cases and res.get("error"):
        return {"text": res["error"], "status": res["status"], "note": "", "in": ""}
    if i < len(cases):
        c = cases[i]
        return {"text": c["out"], "status": c["status"], "note": c["note"], "in": c["in"]}
    return None


def _rows(prev, cur):
    n = max(len(prev.get("cases", [])) if prev else 0, len(cur.get("cases", [])) if cur else 0, 1)
    rows = []
    for i in range(n):
        p, c = _side(prev, i), _side(cur, i)
        differ = bool(p and c and (p["text"], p["status"]) != (c["text"], c["status"]))
        rows.append({"i": i + 1, "in": (c or p or {}).get("in", ""), "prev": p, "cur": c, "differ": differ})
    return rows


def _code_diff(prev_code, cur_code):
    """行単位の差分を [(種類, 行), ...] で返す。種類: add / del / ctx / hunk"""
    out = []
    for line in difflib.unified_diff(prev_code.splitlines(), cur_code.splitlines(), lineterm="", n=2):
        if line.startswith(("---", "+++")):
            continue
        kind = {"+": "add", "-": "del", "@": "hunk"}.get(line[:1], "ctx")
        out.append((kind, line))
    return out


def build_diff(cur_results, prev_results):
    pk = lambda r: (r["sid"], r.get("pkey", r["prob"]))
    prev_keys = {norm_key(r["row_key"]) for r in prev_results}
    cur_keys = {norm_key(r["row_key"]) for r in cur_results}
    prev_by = {}
    for r in sorted(prev_results, key=lambda r: (r.get("ts", ""), r.get("row", 0))):
        prev_by[pk(r)] = r  # 同じ組に複数あれば、いちばん新しい提出を前回の代表とする
    cur_pairs = {pk(r) for r in cur_results}

    entries = []
    for r in cur_results:
        if norm_key(r["row_key"]) in prev_keys:
            continue
        p = prev_by.get(pk(r))
        rows = _rows(p, r)
        e = {"kind": "changed" if p else "added", "cur": r, "prev": p, "rows": rows,
             "out_changed": any(x["differ"] for x in rows), "code_diff": None,
             "code": r["code"]}
        if p:
            e["code_same"] = p["code"] == r["code"]
            e["code_diff"] = None if e["code_same"] else _code_diff(p["code"], r["code"])
        entries.append(e)
    for r in prev_results:
        if norm_key(r["row_key"]) not in cur_keys and pk(r) not in cur_pairs:
            entries.append({"kind": "removed", "cur": None, "prev": r, "rows": _rows(r, None),
                            "out_changed": False, "code_diff": None, "code": r["code"]})
    return entries


def summarize(entries):
    return {"added": sum(e["kind"] == "added" for e in entries),
            "changed": sum(e["kind"] == "changed" for e in entries),
            "changed_out": sum(e["kind"] == "changed" and e["out_changed"] for e in entries),
            "removed": sum(e["kind"] == "removed" for e in entries)}


def tidy(text):
    """比べる前に、改行コードをそろえて最後の改行だけを取り除く (点数の判定と同じ扱い。行末の空白の違いも色が付く)。"""
    return text.replace("\r\n", "\n").rstrip("\n")


def count(text):
    """「全 N 文字、M 行」の N と M (改行は文字に数えない)。"""
    return len(text.replace("\n", "")), (text.count("\n") + 1 if text else 0)


def char_diff(a, b, limit=20000):
    """a と b を文字単位で比べ、違う部分を <mark> で囲んだ HTML を (a 側, b 側) で返す。
    長すぎるときは比べずにそのまま返す (表示が重くならないように)。"""
    from markupsafe import Markup, escape   # web だけが使う (worker には入っていない)
    if len(a) + len(b) > limit:
        return escape(a), escape(b)
    ha, hb = [], []
    for op, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
        sa, sb = a[i1:i2], b[j1:j2]
        if op == "equal":
            ha.append(escape(sa)); hb.append(escape(sb))
            continue
        if sa:
            ha.append(Markup('<mark class="da">') + escape(sa) + Markup("</mark>"))
        if sb:
            hb.append(Markup('<mark class="db">') + escape(sb) + Markup("</mark>"))
    return Markup("").join(ha), Markup("").join(hb)
