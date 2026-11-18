"""点数づけと学生の並べ方。

点数 (コンペ.xlsx の凡例と同じ)
  1   : 全ケースで、出力が期待出力と完全に一致する
  0.5 : コンパイルはできたが、出力が違う (入力を促す文が付いている、空白が違う、出力なし・異常終了・時間切れも含む)
  0   : コンパイルエラー
  None: 期待出力がないので判定できない

「完全に一致」: 改行コード (CRLF / LF) と、出力の最後の改行の有無だけは区別しない。
  それ以外 (入力を促す文、行頭・行末・語の間の空白、空行、小数の桁数) はすべて区別する。
"""
import re
import unicodedata


def normalize(text):
    """比べる前の整形: 改行コードをそろえ、出力の最後の改行だけを取り除く。"""
    return text.replace("\r\n", "\n").rstrip("\n")


def output_matches(out, exp):
    """出力全体が期待出力と完全に一致するか。"""
    return normalize(out) == normalize(exp)


def judge(result):
    """1 つの回答の点数。"""
    if result.get("status") == "COMPILE_ERROR":
        return 0
    cases = [c for c in result.get("cases", []) if (c.get("expected") or "").strip()]
    if not cases:
        return None
    ok = all(c.get("status") == "OK" and output_matches(c.get("out", ""), c["expected"]) for c in cases)
    return 1 if ok else 0.5


def fmt(score):
    return "" if score is None else ("0.5" if score == 0.5 else str(int(score)))


def grade(sid):
    """学籍番号の学年 (入学年度の 2 桁)。例: 303026012 -> 26"""
    return sid[4:6] if len(sid) >= 6 else ""


def roster_order(sids):
    """いちばん新しい学年を先頭に、そのあとは古い学年から順に。各学年の中は学籍番号順。
    例: 26 → 21 → 22 → 23 → 24 → 25"""
    sids = sorted(set(sids))
    newest = max((grade(s) for s in sids), default="")
    return sorted(sids, key=lambda s: (grade(s) != newest, grade(s), s))


def prob_key(name):
    """'問題１' / '問題 1' / '1.0' などから問題番号の数字だけを取り出す (エンジンと同じ規則)。"""
    digits = re.findall(r"\d+", unicodedata.normalize("NFKC", str(name)))
    return str(int(digits[0])) if digits else str(name).strip()


def score_matrix(results, problems, students, score_of=None):
    """コピー用の点数表。
    results : 実行結果。同じ学生・同じ問題が複数あれば、提出時刻がいちばん新しいものを使う
    problems: 列にする問題 (回答ファイルの問題の列の順)
    students: 行の順 (1 人 1 行)
    戻り値  : (各行の点数のリスト。提出していない学生は None, 一覧にない提出者)"""
    latest = {}
    for r in sorted(results, key=lambda r: (r.get("ts", ""), r.get("row", 0))):
        latest[(r["sid"], r.get("pkey", prob_key(r["prob"])))] = r
    keys = [prob_key(p) for p in problems]
    submitted = {sid for sid, _ in latest}
    score_of = score_of or (lambda r: fmt(judge(r)))   # 手動で修正した点数を使うときは呼び出し側が渡す
    rows = [[score_of(latest[(sid, k)]) if (sid, k) in latest else "" for k in keys] if sid in submitted else None
            for sid in students]
    return rows, sorted(submitted - set(students))


# 点数をコピーするときの行の順 (コンペ.xlsx の学生番号の並び。110 人)。
# 学年 26 が先頭、そのあとは古い学年から (21 → 22 → … → 25)、各学年の中は学籍番号順。学生が変わったらここを書き換える
STUDENTS = (
    "303026001", "303026002", "303026003", "303026004", "303026005", "303026006", "303026007", "303026008",
    "303026009", "303026010", "303026011", "303026012", "303026013", "303026014", "303026015", "303026016",
    "303026017", "303026018", "303026019", "303026020", "303026021", "303026022", "303026023", "303026024",
    "303026025", "303026026", "303026027", "303026028", "303026029", "303026030", "303026031", "303026032",
    "303026033", "303026034", "303026035", "303026036", "303026037", "303026038", "303026039", "303026040",
    "303026041", "303026042", "303026043", "303026044", "303026045", "303026046", "303026047", "303026048",
    "303026049", "303026050", "303026051", "303026052", "303026053", "303026054", "303026055", "303026056",
    "303026057", "303026058", "303026059", "303026060", "303026061", "303026062", "303026063", "303026064",
    "303026065", "303026066", "303026067", "303026068", "303026069", "303026070", "303026071", "303026072",
    "303026073", "303026074", "303026075", "303026076", "303026077", "303026078", "303026079", "303026080",
    "303026081", "303026082", "303026083", "303026084", "303026085", "303026086", "303026087", "303026088",
    "303026089", "303026090", "303026091", "303026092", "303026093", "303026094", "303026095", "303026096",
    "303026097", "303026098", "303026099", "303026100", "303026101", "303026102", "303026103", "303026104",
    "303026105", "303026106", "303026107", "303025033", "303025059", "303025082",
)
